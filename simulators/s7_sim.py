"""A Siemens S7 PLC for tests and local development, built on python-snap7's S7 server.

The server holds the data blocks, merkers (M), inputs (I) and outputs (Q) that the device
configs use; :class:`SimulatedS7Device` puts the tags' values there and animates the read-only
ones. One server simulates one PLC (one host:port in the config).

python-snap7's server treats a single-bit write as a byte write; a real CPU changes only that
bit. :class:`S7SimServer` fixes this, so the hub's bit writes are checked the way a PLC
answers them.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Iterable
from typing import Any

from snap7.datatypes import S7WordLen
from snap7.server import Server
from snap7.type import SrvArea

from hub.codec import Value, to_raw
from hub.drivers.s7 import decode_s7, encode_s7
from hub.models import Access, DataType, S7Area, S7Device, S7Tag

log = logging.getLogger(__name__)

_SRV_AREAS = {S7Area.DB: SrvArea.DB, S7Area.MERKER: SrvArea.MK, S7Area.INPUT: SrvArea.PE,
              S7Area.OUTPUT: SrvArea.PA}


class _BitAwareServer(Server):
    """Single-bit writes (transport size BIT) change only the addressed bit."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._bit = threading.local()

    def _parse_address_specification(self, addr_spec: bytes) -> dict[str, Any]:
        parsed = super()._parse_address_specification(addr_spec)
        if parsed and parsed.get("word_len") == S7WordLen.BIT and len(addr_spec) >= 12:
            parsed["bit"] = int.from_bytes(addr_spec[9:12], "big") % 8
        return parsed

    def _parse_write_address(self, request: dict[str, Any]) -> Any:
        spec = request.get("parameters", {}).get("address_spec", {}) or {}
        self._bit.value = spec.get("bit")
        return super()._parse_write_address(request)

    def _write_to_memory_area(self, area: Any, db_number: int, start: int,
                              write_data: bytearray) -> bool:
        bit = getattr(self._bit, "value", None)
        self._bit.value = None
        if bit is None:
            return bool(super()._write_to_memory_area(area, db_number, start, write_data))
        key = (area, db_number)
        if key not in self.memory_areas or start >= len(self.memory_areas[key]):
            return False
        with self.area_locks[key]:
            mem = self.memory_areas[key]
            if write_data and write_data[0] & 1:
                mem[start] |= 1 << bit
            else:
                mem[start] &= ~(1 << bit) & 0xFF
        return True


class S7SimServer:
    """One simulated PLC. Areas are sized from the tags registered with :meth:`ensure`."""

    def __init__(self, port: int = 0, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = port
        self.memory: dict[tuple[S7Area, int], bytearray] = {}
        self._server: _BitAwareServer | None = None

    def ensure(self, tags: Iterable[S7Tag]) -> None:
        sizes: dict[tuple[S7Area, int], int] = {}
        for tag in tags:
            a = tag.parsed
            key = (a.area, a.db)
            sizes[key] = max(sizes.get(key, 0), a.byte + tag.byte_length)
        for key, size in sizes.items():
            mem = self.memory.setdefault(key, bytearray())
            if len(mem) < size + 16:
                mem.extend(bytes(size + 16 - len(mem)))

    def start(self) -> None:
        if self.port == 0:
            from simulators.mqtt_sim import free_port

            self.port = free_port()
        server = _BitAwareServer(log=False)
        for (area, db), mem in self.memory.items():
            server.register_area(_SRV_AREAS[area], db, mem)
        server.start(tcp_port=self.port)
        self._server = server

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.stop()
                self._server.destroy()
            except Exception:  # noqa: BLE001 - already stopped
                pass
            self._server = None

    def __enter__(self) -> S7SimServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class SimulatedS7Device:
    """The tags of one S7 device config inside an :class:`S7SimServer`."""

    def __init__(self, server: S7SimServer, device: S7Device) -> None:
        self.server = server
        self.device = device
        self._tags = {t.name: t for t in device.tags}
        self._t = 0.0
        server.ensure(device.tags)
        for tag in device.tags:
            self.set(tag.name, _initial(tag))

    def _mem(self, tag: S7Tag) -> bytearray:
        a = tag.parsed
        return self.server.memory[(a.area, a.db)]

    def set(self, name: str, value: Value) -> None:
        """Store an engineering value."""
        tag = self._tags[name]
        a = tag.parsed
        mem = self._mem(tag)
        if tag.data_type is DataType.BOOL:
            if value:
                mem[a.byte] |= 1 << (a.bit or 0)
            else:
                mem[a.byte] &= ~(1 << (a.bit or 0)) & 0xFF
            return
        raw = value if tag.data_type is DataType.STRING else to_raw(tag, value)
        data = encode_s7(tag, raw)
        mem[a.byte : a.byte + len(data)] = data

    def get_raw(self, name: str) -> Value:
        tag = self._tags[name]
        a = tag.parsed
        return decode_s7(tag, self._mem(tag)[a.byte : a.byte + tag.byte_length])

    def step(self, dt: float = 1.0) -> None:
        """Read-only numeric tags move; setpoints and outputs keep what was written."""
        self._t += dt
        for tag in self.device.tags:
            if tag.access is Access.READ_WRITE or tag.data_type in (DataType.BOOL,
                                                                     DataType.STRING):
                continue
            self.set(tag.name, animated(tag, self._t))


def _initial(tag: S7Tag) -> Value:
    if tag.data_type is DataType.BOOL:
        return False
    if tag.data_type is DataType.STRING:
        return tag.name.upper()[: tag.string_length or 1]
    return animated(tag, 0.0)


def animated(tag: Any, t: float) -> float | int:
    """A plausible moving value inside the tag's range (shared by the S7/HTTP/SNMP sims)."""
    if tag.states:
        keys = sorted(tag.states)
        return keys[int(t // 30) % len(keys)]
    lo = tag.min if tag.min is not None else 0.0
    hi = tag.max if tag.max is not None else 100.0
    integer = tag.data_type in (DataType.INT16, DataType.UINT16, DataType.INT32,
                                DataType.UINT32, DataType.INT64, DataType.UINT64)
    if tag.data_type in (DataType.UINT32, DataType.UINT64, DataType.INT64) and tag.min is None:
        return round(1000 + t * 10)  # counters (energy, run hours, octets)
    value = (lo + hi) / 2 + (hi - lo) * 0.3 * math.sin(t / 10 + len(tag.name))
    if integer:
        return round(value / tag.scale) * tag.scale if tag.scale != 1.0 else round(value)
    return value


__all__ = ["S7SimServer", "SimulatedS7Device", "animated"]
