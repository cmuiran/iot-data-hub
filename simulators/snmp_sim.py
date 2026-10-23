"""An SNMP agent for tests and local development (pysnmp's agent engine, UDP on 127.0.0.1).

Answers GET for the OIDs of the configured devices, with SNMPv2c (community) and/or SNMPv3
users (real USM authentication and encryption), so the hub's driver goes through the same
security checks as against a real UPS or switch. Unknown OIDs answer noSuchObject; an agent
with ``offline = True`` stays silent like an unreachable one.
"""

from __future__ import annotations

from typing import Any

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.proto import rfc1902
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchObject

from hub.codec import Value, to_raw
from hub.models import DataType, SnmpDevice, SnmpTag
from simulators.mqtt_sim import free_port
from simulators.s7_sim import animated

AUTH = {"md5": config.USM_AUTH_HMAC96_MD5, "sha": config.USM_AUTH_HMAC96_SHA,
        "sha224": config.USM_AUTH_HMAC128_SHA224, "sha256": config.USM_AUTH_HMAC192_SHA256,
        "sha384": config.USM_AUTH_HMAC256_SHA384, "sha512": config.USM_AUTH_HMAC384_SHA512}
PRIV = {"des": config.USM_PRIV_CBC56_DES, "aes128": config.USM_PRIV_CFB128_AES,
        "aes192": config.USM_PRIV_CFB192_AES, "aes256": config.USM_PRIV_CFB256_AES}


def _oid(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split("."))


class _Instrum:
    """What the command responder asks: the value of each requested OID."""

    def __init__(self, agent: SnmpSimAgent) -> None:
        self.agent = agent

    def read_variables(self, *var_binds: Any, **_: Any) -> list[tuple[Any, Any]]:
        return [(oid, self.agent.values.get(tuple(oid), NoSuchObject())) for oid, _ in var_binds]

    def read_next_variables(self, *var_binds: Any, **_: Any) -> list[tuple[Any, Any]]:
        keys = sorted(self.agent.values)
        out = []
        for oid, _ in var_binds:
            nxt = next((k for k in keys if k > tuple(oid)), None)
            out.append((rfc1902.ObjectName(nxt), self.agent.values[nxt]) if nxt
                       else (oid, EndOfMibView()))
        return out


class SnmpSimAgent:
    def __init__(self, port: int = 0, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = port or free_port()
        self.values: dict[tuple[int, ...], Any] = {}
        self.devices: list[SimulatedSnmpDevice] = []
        self.communities: dict[str, str] = {}             # community -> name
        self.users: list[tuple[str, str | None, str | None, str | None, str | None]] = []
        self._engine: engine.SnmpEngine | None = None

    def add_community(self, community: str) -> None:
        self.communities[community] = f"c{len(self.communities)}"

    def add_user(self, username: str, auth_protocol: str | None = None,
                 auth_key: str | None = None, priv_protocol: str | None = None,
                 priv_key: str | None = None) -> None:
        self.users.append((username, auth_protocol, auth_key, priv_protocol, priv_key))

    def start(self) -> None:
        eng = engine.SnmpEngine()
        config.add_transport(eng, udp.DOMAIN_NAME,
                             udp.UdpTransport().open_server_mode((self.host, self.port)))
        for community, name in self.communities.items():
            config.add_v1_system(eng, name, community)
            config.add_vacm_user(eng, 2, name, "noAuthNoPriv", (1, 3, 6), ())
        for username, auth_p, auth_k, priv_p, priv_k in self.users:
            config.add_v3_user(eng, username,
                               AUTH[auth_p] if auth_p else config.USM_AUTH_NONE, auth_k,
                               PRIV[priv_p] if priv_p else config.USM_PRIV_NONE, priv_k)
            level = "authPriv" if priv_p else "authNoPriv" if auth_p else "noAuthNoPriv"
            config.add_vacm_user(eng, 3, username, level, (1, 3, 6), ())
        ctx = context.SnmpContext(eng)
        ctx.context_names[b""] = _Instrum(self)
        cmdrsp.GetCommandResponder(eng, ctx)
        cmdrsp.NextCommandResponder(eng, ctx)
        self._engine = eng

    def stop(self) -> None:
        if self._engine is not None:
            self._engine.close_dispatcher()
            self._engine = None

    def __enter__(self) -> SnmpSimAgent:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _snmp_value(tag: SnmpTag, value: Value) -> Any:
    if tag.data_type is DataType.STRING:
        return rfc1902.OctetString(str(value))
    if tag.data_type is DataType.BOOL:
        return rfc1902.Integer32(1 if value else 2)           # TruthValue
    if tag.data_type in (DataType.FLOAT32, DataType.FLOAT64):
        return rfc1902.OctetString(f"{value:.2f}")            # floats travel as text
    raw = int(to_raw(tag, value))  # type: ignore[arg-type]
    if tag.data_type in (DataType.UINT64, DataType.INT64):
        return rfc1902.Counter64(raw)
    if tag.data_type is DataType.UINT32:
        return rfc1902.Counter32(raw)
    if tag.data_type is DataType.UINT16:
        return rfc1902.Gauge32(raw)
    return rfc1902.Integer32(raw)


class SimulatedSnmpDevice:
    def __init__(self, agent: SnmpSimAgent, device: SnmpDevice) -> None:
        self.agent = agent
        self.device = device
        self._tags = {t.name: t for t in device.tags}
        self._t = 0.0
        agent.devices.append(self)
        for tag in device.tags:
            self.set(tag.name, _initial(tag))

    def set(self, name: str, value: Value | None) -> None:
        tag = self._tags[name]
        if value is None:
            self.agent.values.pop(_oid(tag.oid), None)
        else:
            self.agent.values[_oid(tag.oid)] = _snmp_value(tag, value)

    def step(self, dt: float = 1.0) -> None:
        self._t += dt
        for tag in self.device.tags:
            if tag.data_type not in (DataType.BOOL, DataType.STRING):
                self.set(tag.name, animated(tag, self._t))


def _initial(tag: SnmpTag) -> Value:
    if tag.data_type is DataType.BOOL:
        return True
    if tag.data_type is DataType.STRING:
        return tag.name.upper()
    return animated(tag, 0.0)


__all__ = ["SimulatedSnmpDevice", "SnmpSimAgent"]
