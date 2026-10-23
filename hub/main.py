"""Collector only (no API): read every device and print the live values. For commissioning.

    hub --config-dir config                       # console only
    hub --config-dir config --database-url ...    # also store history and alarms

Ctrl+C stops it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path

from hub.config_loader import load_config
from hub.runtime import Hub, run
from hub.validate_config import print_report


def _render(hub: Hub) -> str:
    lines = []
    for device_id, status in sorted(hub.statuses.items()):
        err = f"  ({status.last_error})" if status.last_error and str(status.state) != "online" \
            else ""
        lines.append(f"[{status.state}] {device_id}{err}")
        device = hub.devices[device_id]
        for tag in device.tags:
            sample = hub.latest.get(device.tag_path(tag.name))
            if sample is None:
                value = "-"
            elif sample.value is None:
                value = "BAD"
            else:
                value = f"{sample.value:.4g}" if isinstance(sample.value, float) else \
                    str(sample.value)
            unit = f" {tag.unit}" if tag.unit and sample and sample.value is not None else ""
            flag = "" if sample is None or str(sample.quality) == "good" else \
                f"  [{sample.quality}]"
            lines.append(f"    {tag.name:<28} {value}{unit}{flag}")
    s = hub.stats
    lines.append(f"samples: {s.samples_received} received, {s.samples_written} stored, "
                 f"{s.buffered} buffered, {s.samples_dropped} dropped")
    return "\n".join(lines)


async def _main(config_dir: Path, database_url: str | None, interval: float) -> int:
    report = load_config(config_dir)
    if report.has_errors:
        print_report(report, sys.stderr)
        return 1
    store = None
    if database_url:
        from hub.storage.migrate import migrate
        from hub.storage.postgres import PostgresStore

        await asyncio.to_thread(migrate, database_url)
        store = PostgresStore(database_url)
        await store.open()
    hub = Hub(report, store=store)
    try:
        async with hub:
            while True:
                await asyncio.sleep(interval)
                print("\n" + _render(hub), flush=True)
    finally:
        if store is not None:
            await store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hub", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--database-url", default=os.environ.get("HUB_DATABASE_URL"))
    parser.add_argument("--interval", type=float, default=5.0, help="print every N seconds")
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        return run(_main(args.config_dir, args.database_url, args.interval))
    return 0


if __name__ == "__main__":
    sys.exit(main())
