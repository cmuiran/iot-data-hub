"""Where the program's data folders (db/migrations, frontend) are.

The hub is normally installed editable (``pip install -e .``), so they sit next to the packages.
A regular install puts the packages into site-packages without them; then ``HUB_HOME`` (the
program folder) or the working directory (the Windows service runs in the program folder) is
used instead.
"""

from __future__ import annotations

import os
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[1]


def program_path(*parts: str) -> Path:
    """The first of <source root>, $HUB_HOME, <cwd> that contains ``parts``."""
    roots = [_SOURCE_ROOT]
    if os.environ.get("HUB_HOME"):
        roots.append(Path(os.environ["HUB_HOME"]))
    roots.append(Path.cwd())
    for root in roots:
        candidate = root.joinpath(*parts)
        if candidate.exists():
            return candidate
    return _SOURCE_ROOT.joinpath(*parts)
