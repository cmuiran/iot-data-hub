"""Run the same checks as CI on this machine, before a commit or a release.

    python scripts/check.py                # lint, security scan, dependency audit, all tests
    python scripts/check.py --quick        # lint + security scan + tests without slow suites
    python scripts/check.py --offline      # skip the dependency audit (it needs the internet)
    python scripts/check.py --load         # also a 100-device load test (about 2 minutes)

Exit code 0 when every step passed. Steps: ruff (lint), bandit (static security analysis),
requirements.lock up to date, pip-audit (known vulnerabilities in the locked runtime
dependencies), python -m hub.validate_config on the repository configuration, pytest, and
optionally hub-loadtest.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
CODE = ["hub", "backend", "simulators", "tests", "scripts"]


def steps(args: argparse.Namespace) -> list[tuple[str, list[str]]]:
    out = [
        ("lint (ruff)", [PY, "-m", "ruff", "check", *CODE]),
        ("security scan (bandit)", [PY, "-m", "bandit", "-q", "-c", "pyproject.toml", "-r",
                                    "hub", "backend", "simulators"]),
    ]
    out.append(("lock file current", [PY, "scripts/lock.py", "--check"]))
    if not args.offline:
        # exactly what ships (requirements.lock), not the tools of the build machine
        out.append(("dependency audit (pip-audit)",
                    [PY, "-m", "pip_audit", "--progress-spinner", "off", "--no-deps",
                     "--disable-pip", "-r", "requirements.lock"]))
    out.append(("configuration", [PY, "-m", "hub.validate_config", "--config-dir", "config"]))
    tests = [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if args.quick:
        tests += ["-m", "not integration"]
    out.append(("tests (pytest)", tests))
    if args.load:
        out.append(("load test (100 devices)",
                    [PY, "-m", "simulators.loadtest", "--devices", "100", "--tags", "20",
                     "--seconds", "60", "--warmup", "20", "--out", ".load/check",
                     "--embedded-db", ".load/check_db", "--json", ".load/check.json"]))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--quick", action="store_true", help="unit tests only (no simulators/db)")
    p.add_argument("--offline", action="store_true", help="skip pip-audit")
    p.add_argument("--load", action="store_true", help="add a short load test")
    args = p.parse_args(argv)
    results: list[tuple[str, bool, float]] = []
    for name, cmd in steps(args):
        print(f"\n=== {name}: {' '.join(Path(c).name if c == PY else c for c in cmd)}",
              flush=True)
        t0 = time.monotonic()
        ok = subprocess.run(cmd, cwd=ROOT).returncode == 0  # noqa: S603 - fixed commands
        results.append((name, ok, time.monotonic() - t0))
    print("\n=== summary")
    for name, ok, seconds in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} ({seconds:.0f} s)")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
