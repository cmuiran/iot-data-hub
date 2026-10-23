"""OPC UA server simulating one or more devices from config (security None, anonymous).

Every tag's ``node_id`` is created exactly as configured, so the hub driver's NodeId handling
(``ns=``, bare ids with ``connection.namespace``) is exercised for real. Writable tags accept
writes; :meth:`SimulatedOpcUaServer.step` animates the read-only numeric ones.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from asyncua import Server, ua

from hub.codec import Value
from hub.drivers.opcua import VARIANT
from hub.models import Access, DataType, OpcUaDevice, OpcUaTag
from simulators.mqtt_sim import free_port

for _name in ("asyncua", "asyncua.server", "asyncua.server.uaprocessor",
              "asyncua.server.binary_server_asyncio", "asyncua.server.address_space"):
    logging.getLogger(_name).setLevel(logging.ERROR)


class SimulatedOpcUaServer:
    """``certificate``/``private_key`` switch the endpoint to Basic256Sha256 SignAndEncrypt;
    ``trusted_dir`` then only accepts client certificates found there."""

    def __init__(self, devices: list[OpcUaDevice], port: int | None = None, *,
                 certificate: Path | None = None, private_key: Path | None = None,
                 trusted_dir: Path | None = None) -> None:
        self.certificate = certificate
        self.private_key = private_key
        self.trusted_dir = trusted_dir
        self.port = port or free_port()
        self.endpoint = f"opc.tcp://127.0.0.1:{self.port}/hub-sim/"
        self.devices = devices
        self.server: Server | None = None
        self.nodes: dict[str, Any] = {}  # "<device_id>.<tag>" -> Node
        self._t = 0.0

    async def start(self) -> None:
        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name("Hub OPC UA simulator")
        if self.certificate and self.private_key:
            await server.load_certificate(str(self.certificate))
            await server.load_private_key(str(self.private_key))
            server.set_security_policy([ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt])
            if self.trusted_dir is not None:
                from hub.pki import trust_validator

                server.set_certificate_validator((await trust_validator(self.trusted_dir))
                                                 .validate)
        else:
            server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.server = server
        objects = server.nodes.objects
        for device in self.devices:
            ns_uri = device.connection.namespace or f"urn:hub:sim:{device.device_id}"
            uri_index = await server.register_namespace(ns_uri)
            folder = await objects.add_folder(uri_index, device.device_id)
            for tag in device.tags:
                assert isinstance(tag, OpcUaTag)
                node_id = await self._node_id(tag.node_id, uri_index)
                node = await folder.add_variable(node_id, tag.name, _initial(tag),
                                                 varianttype=VARIANT[tag.data_type])
                if tag.access is Access.READ_WRITE:
                    await node.set_writable()
                self.nodes[f"{device.device_id}.{tag.name}"] = node
        await server.start()

    async def _node_id(self, text: str, default_ns: int) -> ua.NodeId:
        assert self.server is not None
        if text.startswith("nsu="):
            uri, rest = text[4:].split(";", 1)
            idx = await self.server.register_namespace(uri)
            text = f"ns={idx};{rest}"
        elif not text.startswith("ns="):
            text = f"ns={default_ns};{text}"
        else:
            wanted = int(text[3:].split(";", 1)[0])
            while len(await self.server.get_namespace_array()) <= wanted:
                count = len(await self.server.get_namespace_array())
                await self.server.register_namespace(f"urn:hub:sim:extra:{count}")
        return ua.NodeId.from_string(text)

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()
            self.server = None

    async def __aenter__(self) -> SimulatedOpcUaServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def set(self, device_id: str, tag_name: str, value: Value) -> None:
        tag = self._tag(device_id, tag_name)
        await self.nodes[f"{device_id}.{tag_name}"].write_value(
            ua.Variant(value, VARIANT[tag.data_type]))

    async def get(self, device_id: str, tag_name: str) -> Value:
        return await self.nodes[f"{device_id}.{tag_name}"].read_value()

    async def set_bad(self, device_id: str, tag_name: str) -> None:
        node = self.nodes[f"{device_id}.{tag_name}"]
        tag = self._tag(device_id, tag_name)
        await node.write_value(ua.DataValue(ua.Variant(_initial(tag), VARIANT[tag.data_type]),
                                            StatusCode=ua.StatusCode(ua.StatusCodes.BadSensorFailure)))

    def _tag(self, device_id: str, name: str) -> OpcUaTag:
        for device in self.devices:
            if device.device_id == device_id:
                for tag in device.tags:
                    if tag.name == name:
                        return tag  # type: ignore[return-value]
        raise KeyError(f"{device_id}.{name}")

    async def step(self, dt: float = 1.0) -> None:
        self._t += dt
        for device in self.devices:
            for tag in device.tags:
                if tag.access is Access.READ and tag.data_type in (DataType.FLOAT32,
                                                                   DataType.FLOAT64):
                    lo = tag.min if tag.min is not None else 0.0
                    hi = tag.max if tag.max is not None else 100.0
                    value = (lo + hi) / 2 + (hi - lo) * 0.3 * math.sin(self._t / 10 + len(tag.name))
                    await self.set(device.device_id, tag.name, value)


def _initial(tag: OpcUaTag) -> Value:
    if tag.data_type is DataType.BOOL:
        return False
    if tag.data_type is DataType.STRING:
        return "ok"
    if tag.data_type in (DataType.FLOAT32, DataType.FLOAT64):
        return 0.0
    return 0
