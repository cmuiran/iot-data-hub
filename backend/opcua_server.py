"""OPC UA server output: SCADA / MES read the hub's tags over OPC UA (item 6).

Address space (namespace ``urn:iot-data-hub``)::

    Objects/Hub/<site>/<area>/<device>/<tag>        NodeId ns=<n>;s=<site>.<area>.<device>.<tag>
    Objects/Hub/<site>/<area>/<device>/Status       NodeId ns=<n>;s=<site>.<area>.<device>._status

Values carry the hub's quality (Good / Uncertain / Bad) and the sample's source timestamp, and
follow the live data. Each tag has ``Unit`` / ``Description`` properties.

Security:

* Endpoints: Basic256Sha256 Sign and SignAndEncrypt. ``None`` only with
  HUB_OPCUA_SERVER_ALLOW_INSECURE=true (lab use). The server certificate is
  ``<pki>/hub-server.der/.pem`` (created on first start). If ``<pki>/trusted/`` contains client
  certificates, only those clients may connect.
* Users: the hub's own accounts (username + password), with ``data.read`` granted for *all*
  sites — OPC UA has no per-user view of the address space, so scoped users are refused. Use a
  dedicated service account for SCADA. Anonymous access is refused.
* Writes: off unless HUB_OPCUA_SERVER_WRITES=true. Then a write to a read_write tag goes through
  the control-safety layer as the logged-in user (RBAC, interlocks, audit...) and the OPC UA
  status code tells the client the outcome; the value in the address space changes only through
  the device read-back. Tags that need a second person answer GoodCompletesAsynchronously.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import Any

from asyncua import Server, ua
from asyncua.crypto.permission_rules import User, UserRole

from backend.security import verify_password
from hub.drivers.opcua import VARIANT
from hub.models import Access, DataType, Quality

log = logging.getLogger(__name__)
NAMESPACE = "urn:iot-data-hub"
USER_REFRESH_S = 30.0

_QUALITY = {Quality.GOOD: ua.StatusCodes.Good, Quality.UNCERTAIN: ua.StatusCodes.Uncertain,
            Quality.BAD: ua.StatusCodes.BadNotConnected}
_RESULT = {
    "applied": ua.StatusCodes.Good,
    "requested": ua.StatusCodes.GoodCompletesAsynchronously,
    "rejected_rbac": ua.StatusCodes.BadUserAccessDenied,
    "rejected_disabled": ua.StatusCodes.BadNotWritable,
    "rejected_range": ua.StatusCodes.BadOutOfRange,
    "rejected_rate": ua.StatusCodes.BadInvalidState,
    "rejected_interlock": ua.StatusCodes.BadInvalidState,
    "rejected_unconfirmed": ua.StatusCodes.BadInvalidState,
    "failed": ua.StatusCodes.BadCommunicationError,
    "timeout": ua.StatusCodes.BadTimeout,
    "readback_mismatch": ua.StatusCodes.UncertainLastUsableValue,
}


def _initial(data_type: DataType) -> Any:
    if data_type is DataType.BOOL:
        return False
    if data_type is DataType.STRING:
        return ""
    return 0.0 if data_type in (DataType.FLOAT32, DataType.FLOAT64) else 0


class HubUserManager:
    """asyncua calls get_user() synchronously; accounts are cached and refreshed every 30 s."""

    def __init__(self) -> None:
        self.users: dict[str, tuple[str, bool]] = {}   # username -> (hash, may_use_opcua)
        self.failures = 0

    def get_user(self, iserver: Any, username: str | None = None, password: str | None = None,
                 certificate: Any = None) -> User | None:
        if not username:
            return None  # anonymous is refused
        entry = self.users.get(username.lower())
        if entry is None or not verify_password(entry[0], password or "") or not entry[1]:
            self.failures += 1
            log.warning("OPC UA login refused for %r", username)
            return None
        return User(role=UserRole.User, name=username.lower())


class HubOpcUaServer:
    def __init__(self, services: Any) -> None:
        self.services = services
        self.settings = services.settings
        self.server: Server | None = None
        self.users = HubUserManager()
        self.ns = 0
        self.nodes: dict[str, Any] = {}           # tag path / "<device>._status" -> Node
        self._writable: dict[str, str] = {}       # NodeId string -> tag path
        self._queue: asyncio.Queue[dict[str, Any] | None] | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self.writes = {"accepted": 0, "refused": 0}

    @property
    def endpoint(self) -> str:
        return f"opc.tcp://{self.settings.opcua_server_host}:{self.settings.opcua_server_port}/hub/"

    # --- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        from hub.pki import make_certificate, trust_validator

        await self._refresh_users()
        server = Server(user_manager=self.users)  # type: ignore[arg-type]
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name("Industrial IoT Data Hub")
        await server.set_application_uri(f"{NAMESPACE}:server")
        pki = self.settings.pki_dir
        cert, key = await make_certificate(pki, "hub-server", "server",
                                           uri=f"{NAMESPACE}:server",
                                           hostname=self.settings.opcua_server_host)
        await server.load_certificate(str(cert))
        await server.load_private_key(str(key))
        policies = [ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
                    ua.SecurityPolicyType.Basic256Sha256_Sign]
        if self.settings.opcua_server_allow_insecure:
            policies.append(ua.SecurityPolicyType.NoSecurity)
        server.set_security_policy(policies)
        server.set_identity_tokens([ua.UserNameIdentityToken])
        trusted = pki / "trusted"
        if trusted.is_dir() and any(trusted.iterdir()):
            server.set_certificate_validator((await trust_validator(trusted)).validate)
        else:
            log.warning("OPC UA server: %s is empty, any client certificate is accepted "
                        "(users still need a password)", trusted)
        self.ns = await server.register_namespace(NAMESPACE)
        self.server = server
        await self._build(server)
        self._wrap_writes(server)
        await server.start()
        self._queue = self.services.hub.bus.subscribe()
        self._tasks = [asyncio.create_task(self._follow(), name="opcua-server:values"),
                       asyncio.create_task(self._users_loop(), name="opcua-server:users")]
        tags = sum(1 for k in self.nodes if not k.endswith("._status"))
        log.info("OPC UA server on %s (%d tags, writes %s)", self.endpoint, tags,
                 "on" if self.settings.opcua_server_writes else "off")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        if self._queue is not None:
            self.services.hub.bus.unsubscribe(self._queue)
            self._queue = None
        if self.server is not None:
            await self.server.stop()
            self.server = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    # --- users --------------------------------------------------------------------------------

    async def _refresh_users(self) -> None:
        store = self.services.store
        perms = await store.role_permissions()
        users: dict[str, tuple[str, bool]] = {}
        for u in await store.list_users():
            if not u.is_active or u.must_change_password:
                continue
            grants = await store.grants(u.user_id)
            global_read = any(g.site is None and "data.read" in perms.get(g.role_id, ())
                              for g in grants)
            users[u.username] = (u.password_hash, global_read)
        self.users.users = users

    async def _users_loop(self) -> None:
        while True:
            await asyncio.sleep(USER_REFRESH_S)
            try:
                await self._refresh_users()
            except Exception:  # noqa: BLE001
                log.exception("OPC UA user refresh failed")

    # --- address space ------------------------------------------------------------------------

    async def _build(self, server: Server) -> None:
        hub = self.services.hub
        def qn(name: str) -> ua.QualifiedName:
            return ua.QualifiedName(name, self.ns)

        root = await server.nodes.objects.add_folder(ua.NodeId("Hub", self.ns), qn("Hub"))
        folders: dict[tuple[str, ...], Any] = {}
        writes_on = self.settings.opcua_server_writes
        for device_id, device in sorted(hub.devices.items()):
            parent = root
            for depth, name in enumerate((device.site, device.area, device_id), start=1):
                key = (device.site, device.area, device_id)[:depth]
                if key not in folders:
                    folders[key] = await parent.add_folder(
                        ua.NodeId(".".join(key) + "._folder", self.ns), qn(name))
                parent = folders[key]
            status = hub.statuses.get(device_id)
            node = await parent.add_variable(
                ua.NodeId(f"{device.site}.{device.area}.{device_id}._status", self.ns),
                qn("Status"), str(status.state) if status else "unknown",
                varianttype=ua.VariantType.String)
            self.nodes[f"{device_id}._status"] = node
            for tag in device.tags:
                path = device.tag_path(tag.name)
                sample = hub.latest.get(path)
                value = sample.value if sample and sample.value is not None else _initial(
                    tag.data_type)
                node = await parent.add_variable(ua.NodeId(path, self.ns), qn(tag.name),
                                                 ua.Variant(value, VARIANT[tag.data_type]))
                if tag.unit:
                    await node.add_property(self.ns, "Unit", tag.unit)
                if tag.description:
                    await node.add_property(self.ns, "Description", tag.description)
                if writes_on and tag.access is Access.READ_WRITE:
                    await node.set_writable()
                    self._writable[node.nodeid.to_string()] = path
                self.nodes[path] = node
                if sample is not None:
                    await self._set(path, sample.value, sample.quality, sample.timestamp)

    async def _set(self, key: str, value: Any, quality: Quality | str,
                   ts: datetime | None) -> None:
        """Update a tag node directly in the address space (not via a session)."""
        node = self.nodes.get(key)
        found = self.services.hub.tags.get(key)
        if node is None or found is None or self.server is None:
            return
        data_type = found[1].data_type
        variant = ua.Variant(_initial(data_type) if value is None else value, VARIANT[data_type])
        dv = ua.DataValue(variant, StatusCode=ua.StatusCode(_QUALITY[Quality(quality)]),
                          SourceTimestamp=ts, ServerTimestamp=datetime.now(UTC))
        await self.server.write_attribute_value(node.nodeid, dv)

    async def _follow(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            if event is None:  # dropped by the bus: resubscribe and resync
                self._queue = self.services.hub.bus.subscribe()
                for path, s in list(self.services.hub.latest.items()):
                    await self._set(path, s.value, s.quality, s.timestamp)
                continue
            try:
                if event.get("type") == "samples":
                    for s in event["samples"]:
                        await self._set(s["tag"], s["value"], s["quality"],
                                        datetime.fromisoformat(s["ts"]))
                elif event.get("type") == "status":
                    node = self.nodes.get(f"{event['device_id']}._status")
                    if node is not None and self.server is not None:
                        await self.server.write_attribute_value(
                            node.nodeid, ua.DataValue(ua.Variant(event["state"],
                                                                 ua.VariantType.String)))
            except Exception:  # noqa: BLE001
                log.exception("OPC UA server value update failed")

    # --- writes from OPC UA clients -----------------------------------------------------------

    def _wrap_writes(self, server: Server) -> None:
        service = server.iserver.attribute_service
        original = service.write

        # same default as asyncua's AttributeService.write (its internal session)
        async def write(params: ua.WriteParameters,
                        user: User = User(role=UserRole.Admin),  # noqa: B008
                        ) -> list[ua.StatusCode]:
            if user.role is UserRole.Admin:  # the server's internal session
                return await original(params, user=user)
            results: list[ua.StatusCode] = []
            for wv in params.NodesToWrite:
                path = self._writable.get(wv.NodeId.to_string())
                if path is None or wv.AttributeId != ua.AttributeIds.Value:
                    results.append(ua.StatusCode(ua.StatusCodes.BadNotWritable))
                    self.writes["refused"] += 1
                    continue
                results.append(await self._control_write(user, path, wv.Value))
            return results

        service.write = write  # type: ignore[method-assign]

    async def _control_write(self, user: User, path: str, dv: ua.DataValue) -> ua.StatusCode:
        from backend.rbac import Principal

        store = self.services.store
        account = await store.get_user_by_username(user.name or "")
        if account is None or not account.is_active:
            self.writes["refused"] += 1
            return ua.StatusCode(ua.StatusCodes.BadUserAccessDenied)
        principal = Principal(user=account, session_id=None,
                              grants=tuple(await store.grants(account.user_id)),
                              role_permissions=await store.role_permissions(),
                              site_orgs=self.services.auth.site_orgs())
        value = dv.Value.Value if dv.Value is not None else None
        if isinstance(value, float) and value.is_integer() and self.services.hub.tags[path][
                1].data_type not in (DataType.FLOAT32, DataType.FLOAT64):
            value = int(value)
        started = time.monotonic()
        result = await self.services.control.request_write(principal, path, value,
                                                           "OPC UA write", None)
        self.writes["accepted" if result.status in ("applied", "pending_confirmation")
                    else "refused"] += 1
        log.info("OPC UA write %s=%r by %s -> %s (%.0f ms)", path, value, user.name,
                 result.result, (time.monotonic() - started) * 1000)
        return ua.StatusCode(_RESULT.get(result.result, ua.StatusCodes.BadInvalidState))
