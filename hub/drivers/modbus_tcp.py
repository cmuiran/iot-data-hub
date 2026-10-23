"""Modbus driver (pymodbus async client): Modbus TCP, RTU over TCP, and Modbus RTU on a serial
line (COM port, USB adapter, or an RFC 2217 serial server such as a Moxa NPort).

Polling: tags are grouped into blocks per register type (neighbours within ``MAX_GAP``
registers, at most ``max_registers_per_read``) so a meter with 40 values costs a few requests.
Some devices reject reads that touch undefined registers; when a block fails with exception 02
(illegal data address) it is split into one request per tag from then on.

Devices behind the same serial gateway (same host:port, different unit_id), and devices on the
same serial port, share one connection, and requests on it are serialized: gateways often accept
a single connection and an RS-485 line can only carry one request at a time.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from pymodbus import FramerType
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.pdu import ExceptionResponse

from hub.codec import (
    CodecError,
    Value,
    decode_bcd,
    decode_bit,
    decode_bit_field,
    decode_registers,
    encode_bcd,
    encode_registers,
)
from hub.drivers.base import (
    DeviceState,
    Driver,
    DriverError,
    sleep_until_next,
)
from hub.models import ModbusDevice, ModbusRtuDevice, ModbusTag, RegisterType, TagBase

log = logging.getLogger(__name__)
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)  # its own retries/log spam

MAX_GAP = 8  # registers skipped inside one block read
ILLEGAL_DATA_ADDRESS = 2


class ModbusDeviceException(DriverError):
    def __init__(self, code: int, what: str) -> None:
        super().__init__(f"Modbus exception {code} on {what}")
        self.code = code


# --------------------------------------------------------------------------------------------
# Shared connections
# --------------------------------------------------------------------------------------------


@dataclass
class _Connection:
    client: AsyncModbusTcpClient | AsyncModbusSerialClient
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


_connections: dict[tuple[str, ...], _Connection] = {}


def connection_key(device: ModbusDevice | ModbusRtuDevice) -> tuple[str, ...]:
    c = device.connection
    if isinstance(device, ModbusRtuDevice):
        return ("serial", c.port.lower())  # type: ignore[union-attr]
    return ("tcp", c.host.lower(), str(c.port), c.framer)  # type: ignore[union-attr]


def _make_client(device: ModbusDevice | ModbusRtuDevice
                 ) -> AsyncModbusTcpClient | AsyncModbusSerialClient:
    c = device.connection
    timeout_s, retries = c.timeout_ms / 1000, c.retries
    if isinstance(device, ModbusRtuDevice):
        rc = device.connection
        return AsyncModbusSerialClient(rc.port, framer=FramerType.RTU, baudrate=rc.baudrate,
                                       bytesize=rc.bytesize, parity=rc.parity,
                                       stopbits=rc.stopbits, timeout=timeout_s, retries=retries,
                                       reconnect_delay=0)
    tc = device.connection
    framer = FramerType.RTU if tc.framer == "rtu" else FramerType.SOCKET
    return AsyncModbusTcpClient(tc.host, port=tc.port, framer=framer, timeout=timeout_s,
                                retries=retries, reconnect_delay=0)


async def _acquire(device: ModbusDevice | ModbusRtuDevice) -> _Connection:
    key = connection_key(device)
    conn = _connections.get(key)
    if conn is None:
        conn = _connections[key] = _Connection(_make_client(device))
    conn.users += 1
    async with conn.lock:
        if not conn.client.connected and not await conn.client.connect():
            conn.users -= 1
            raise DriverError(f"cannot connect to {':'.join(key[1:3])}")
    return conn


def _release(key: tuple[str, ...]) -> None:
    conn = _connections.get(key)
    if conn is None:
        return
    conn.users -= 1
    if conn.users <= 0:
        conn.client.close()
        del _connections[key]


# --------------------------------------------------------------------------------------------
# Read plan
# --------------------------------------------------------------------------------------------


@dataclass
class Block:
    register_type: RegisterType
    start: int
    count: int
    tags: list[ModbusTag]
    split: bool = False  # read tag by tag (after an illegal-address exception)


def plan_blocks(tags: list[ModbusTag], max_registers: int, max_gap: int = MAX_GAP) -> list[Block]:
    blocks: list[Block] = []
    by_type: dict[RegisterType, list[ModbusTag]] = {}
    for tag in tags:
        by_type.setdefault(tag.register_type, []).append(tag)
    bit_area_limit = 2000
    for rtype, group in by_type.items():
        limit = bit_area_limit if rtype in (RegisterType.COIL, RegisterType.DISCRETE_INPUT) else (
            max_registers
        )
        group.sort(key=lambda t: (t.address, t.last_address))
        current: Block | None = None
        for tag in group:
            if current is not None:
                end = current.start + current.count - 1
                new_end = max(end, tag.last_address)
                if tag.address <= end + 1 + max_gap and new_end - current.start + 1 <= limit:
                    current.count = new_end - current.start + 1
                    current.tags.append(tag)
                    continue
                blocks.append(current)
            current = Block(rtype, tag.address, tag.register_count, [tag])
        if current is not None:
            blocks.append(current)
    return blocks


# --------------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------------


class ModbusTcpDriver(Driver[ModbusDevice]):
    """Modbus TCP, RTU over TCP and Modbus RTU (the transport comes from the device type)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        c = self.device.connection
        self._blocks = plan_blocks(list(self.device.tags), c.max_registers_per_read)
        self._conn: _Connection | None = None

    @property
    def _unit(self) -> int:
        return self.device.connection.unit_id

    async def _connect(self) -> None:
        self._conn = await _acquire(self.device)

    async def _disconnect(self) -> None:
        if self._conn is not None:
            self._conn = None
            _release(connection_key(self.device))

    async def _run_connected(self) -> None:
        period = self.device.poll_interval_ms / 1000
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            started = loop.time()
            await self.poll_once()
            await sleep_until_next(period, started, self._stopping)

    async def poll_once(self) -> None:
        """Read every block once and emit samples. Connection errors propagate (reconnect)."""
        samples = []
        failures = 0
        for block in self._blocks:
            try:
                values = await self._read_block(block)
            except ModbusDeviceException as exc:
                failures += 1
                if exc.code == ILLEGAL_DATA_ADDRESS and not block.split and len(block.tags) > 1:
                    block.split = True
                    log.info("%s: splitting %s block at %d after exception 2",
                             self.device.device_id, block.register_type, block.start)
                    try:
                        values = await self._read_block(block)
                        failures -= 1
                    except ModbusDeviceException as exc2:
                        values = {}
                        await self.mark_error(DeviceState.ERROR, str(exc2))
                else:
                    values = {}
                    await self.mark_error(DeviceState.ERROR, str(exc))
            for tag in block.tags:
                samples.append(self.make_sample(tag, values.get(tag.name)))
        await self.emit(samples)
        if failures == 0:
            await self.mark_seen()

    async def _read_block(self, block: Block) -> dict[str, Value | None]:
        if block.split:
            out: dict[str, Value | None] = {}
            for tag in block.tags:
                try:
                    out[tag.name] = await self._read_raw(tag)
                except ModbusDeviceException as exc:
                    if exc.code != ILLEGAL_DATA_ADDRESS:
                        raise
                    out[tag.name] = None
            if all(v is None for v in out.values()):
                raise ModbusDeviceException(ILLEGAL_DATA_ADDRESS, f"every tag of {block.start}")
            return out
        data = await self._read_area(block.register_type, block.start, block.count)
        out = {}
        for tag in block.tags:
            try:
                out[tag.name] = self._extract(tag, data, block.start)
            except CodecError as exc:   # e.g. a register that is not BCD: this tag only
                log.warning("%s: %s: %s", self.device.device_id, tag.name, exc)
                out[tag.name] = None
        return out

    def _extract(self, tag: ModbusTag, data: list[int], start: int) -> Value:
        offset = tag.address - start
        if tag.register_type in (RegisterType.COIL, RegisterType.DISCRETE_INPUT):
            return bool(data[offset])
        if tag.bit is not None:
            if tag.bit_count is not None:
                return decode_bit_field(data[offset], tag.bit, tag.bit_count)
            return decode_bit(data[offset], tag.bit)
        c = self.device.connection
        regs = data[offset : offset + tag.register_count]
        if tag.encoding == "bcd":
            return decode_bcd(regs, c.word_order)
        return decode_registers(regs, tag.data_type, c.byte_order, c.word_order)

    async def _read_area(self, rtype: RegisterType, address: int, count: int) -> list[int]:
        conn = self._require_conn()
        client = conn.client
        call = {
            RegisterType.COIL: client.read_coils,
            RegisterType.DISCRETE_INPUT: client.read_discrete_inputs,
            RegisterType.HOLDING: client.read_holding_registers,
            RegisterType.INPUT: client.read_input_registers,
        }[rtype]
        async with conn.lock:
            try:
                response = await call(address, count=count, device_id=self._unit)
            except ModbusException as exc:
                raise DriverError(f"{rtype} {address}+{count}: {exc}") from exc
        if isinstance(response, ExceptionResponse) or response.isError():
            code = getattr(response, "exception_code", 0)
            raise ModbusDeviceException(code, f"{rtype} {address}+{count}")
        if rtype in (RegisterType.COIL, RegisterType.DISCRETE_INPUT):
            return [int(b) for b in response.bits[:count]]
        return list(response.registers)

    def _require_conn(self) -> _Connection:
        if self._conn is None:
            raise DriverError(f"{self.device.device_id} is not connected")
        return self._conn

    async def _read_raw(self, tag: TagBase) -> Value:
        assert isinstance(tag, ModbusTag)
        data = await self._read_area(tag.register_type, tag.address, tag.register_count)
        return self._extract(tag, data, tag.address)

    async def _write_raw(self, tag: TagBase, raw: Value) -> None:
        assert isinstance(tag, ModbusTag)
        conn = self._require_conn()
        client = conn.client
        async with conn.lock:
            try:
                if tag.register_type is RegisterType.COIL:
                    response = await client.write_coil(tag.address, bool(raw), device_id=self._unit)
                elif tag.register_type is RegisterType.HOLDING and tag.bit is None:
                    c = self.device.connection
                    regs = (encode_bcd(raw, tag.register_count, c.word_order)
                            if tag.encoding == "bcd" else
                            encode_registers(raw, tag.data_type, c.byte_order, c.word_order,
                                             count=tag.count))
                    if len(regs) == 1:
                        response = await client.write_register(tag.address, regs[0],
                                                               device_id=self._unit)
                    else:
                        response = await client.write_registers(tag.address, regs,
                                                                device_id=self._unit)
                else:
                    raise DriverError(f"{tag.register_type} tag {tag.name} cannot be written")
            except ModbusException as exc:
                raise DriverError(f"write {tag.name}: {exc}") from exc
        if isinstance(response, ExceptionResponse) or response.isError():
            raise ModbusDeviceException(getattr(response, "exception_code", 0), f"write {tag.name}")


__all__ = ["Block", "ModbusTcpDriver", "plan_blocks"]
