"""Protocol drivers. Use :func:`create_driver` to get the right one for a device."""

from __future__ import annotations

from hub.drivers.base import (
    AnyDevice,
    Clock,
    DeviceState,
    DeviceStatus,
    Driver,
    DriverError,
    SampleSink,
    StatusSink,
    WriteGrant,
    WriteNotPermitted,
    utcnow,
)
from hub.models import Broker, ModbusDevice, ModbusRtuDevice, MqttDevice, OpcUaDevice


def create_driver(
    device: AnyDevice,
    on_samples: SampleSink,
    on_status: StatusSink,
    *,
    brokers: dict[str, Broker] | None = None,
    clock: Clock = utcnow,
) -> Driver:
    if isinstance(device, ModbusDevice | ModbusRtuDevice):
        from hub.drivers.modbus_tcp import ModbusTcpDriver

        return ModbusTcpDriver(device, on_samples, on_status, clock=clock)  # type: ignore[arg-type]
    if isinstance(device, MqttDevice):
        from hub.drivers.mqtt_json import MqttJsonDriver

        broker = (brokers or {}).get(device.connection.broker_ref)
        if broker is None:
            raise DriverError(f"{device.device_id}: unknown broker {device.connection.broker_ref}")
        return MqttJsonDriver(device, on_samples, on_status, broker=broker, clock=clock)
    if isinstance(device, OpcUaDevice):
        from hub.drivers.opcua import OpcUaDriver

        return OpcUaDriver(device, on_samples, on_status, clock=clock)
    raise DriverError(f"no driver for protocol {device.protocol!r}")


__all__ = [
    "AnyDevice", "DeviceState", "DeviceStatus", "Driver", "DriverError", "WriteGrant",
    "WriteNotPermitted", "create_driver",
]
