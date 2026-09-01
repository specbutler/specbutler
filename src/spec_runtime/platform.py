"""Facts about the host platform kept out of workflow code."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath


def is_windows() -> bool:
    return os.name == "nt"


def is_unc_path(path: str | Path) -> bool:
    """Return whether a path names a Windows UNC/network location."""
    text = str(path)
    return text.startswith(("\\\\", "//")) or bool(PureWindowsPath(text).drive.startswith("\\\\"))

