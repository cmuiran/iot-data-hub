"""Program folders are found after a regular (non-editable) install too."""

from __future__ import annotations

from pathlib import Path

import pytest

from hub import paths


def test_program_path_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site = tmp_path / "site-packages"            # a regular install: no db/ next to the code
    site.mkdir()
    home = tmp_path / "hub"
    (home / "db" / "migrations").mkdir(parents=True)
    work = tmp_path / "work"
    (work / "frontend").mkdir(parents=True)
    monkeypatch.setattr(paths, "_SOURCE_ROOT", site)
    monkeypatch.chdir(work)
    monkeypatch.delenv("HUB_HOME", raising=False)
    assert paths.program_path("frontend") == work / "frontend"          # the service's cwd
    assert paths.program_path("db", "migrations") == site / "db" / "migrations"  # not found
    monkeypatch.setenv("HUB_HOME", str(home))
    assert paths.program_path("db", "migrations") == home / "db" / "migrations"


def test_source_checkout_wins() -> None:
    assert (paths.program_path("db", "migrations") / "0001_core.sql").is_file()
    assert (paths.program_path("frontend") / "index.html").is_file()
