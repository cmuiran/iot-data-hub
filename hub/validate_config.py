"""Validate the hub configuration and print a per-device report.

Usage::

    python -m hub.validate_config [--config-dir config] [--strict]

Exit codes: 0 = no errors (warnings allowed unless --strict), 1 = errors found,
2 = the config directory does not exist.
"""

from __future__ import annotations

import argparse
import io
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from hub.config_loader import (
    ALARMS_FILE,
    BROKERS_FILE,
    BUILDING_FILE,
    CONTROL_FILE,
    DEVICES_DIR,
    NOTIFICATIONS_FILE,
    ConfigReport,
    Severity,
    load_config,
)

_FILE_LABELS = {BROKERS_FILE: "(brokers)", ALARMS_FILE: "(alarms)", CONTROL_FILE: "(control)",
                NOTIFICATIONS_FILE: "(notify)", BUILDING_FILE: "(building)"}


def _status(report: ConfigReport, source: str) -> str:
    severities = {i.severity for i in report.issues_for(source)}
    if Severity.ERROR in severities:
        return "ERROR"
    if Severity.WARNING in severities:
        return "WARNING"
    return "OK"


def print_report(report: ConfigReport, out: TextIO) -> None:
    # Files that were read, plus pseudo-sources such as a missing devices/ directory.
    sources = list(report.sources)
    sources += sorted({i.source for i in report.issues} - set(sources))
    labels = {s: report.sources.get(s) or _FILE_LABELS.get(s, "-") for s in sources}
    label_w = max((len(v) for v in labels.values()), default=1)
    source_w = max((len(s) for s in sources), default=1)

    print(f"Validating configuration in {report.config_dir}\n", file=out)
    device_status: Counter[str] = Counter()
    for source in sources:
        status = _status(report, source)
        if source in report.sources and source.startswith(f"{DEVICES_DIR}/"):
            device_status[status] += 1
        print(f"{status:<8} {labels[source]:<{label_w}}  {source:<{source_w}}".rstrip(), file=out)
        for issue in sorted(report.issues_for(source), key=lambda i: i.severity != "ERROR"):
            where = f"{issue.location}: " if issue.location else ""
            print(f"    {issue.severity:<8} {where}{issue.message}", file=out)

    errors = sum(i.severity is Severity.ERROR for i in report.issues)
    warnings = len(report.issues) - errors
    print(
        f"\nDevices: {device_status.total()} ({device_status['OK']} OK, "
        f"{device_status['WARNING']} WARNING, {device_status['ERROR']} ERROR). "
        f"Issues: {errors} error(s), {warnings} warning(s).",
        file=out,
    )


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(
        prog="python -m hub.validate_config",
        description="Validate the hub configuration directory.",
    )
    parser.add_argument("--config-dir", type=Path, default=Path("config"),
                        help="default: %(default)s")
    parser.add_argument("--strict", action="store_true", help="treat warnings as errors")
    args = parser.parse_args(argv)

    if not args.config_dir.is_dir():
        print(f"config directory not found: {args.config_dir}", file=sys.stderr)
        return 2

    report = load_config(args.config_dir)
    print_report(report, out)
    if report.has_errors or (args.strict and report.issues):
        return 1
    return 0


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")  # non-ASCII config values on legacy consoles
    sys.exit(main())
