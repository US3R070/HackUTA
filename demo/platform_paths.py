"""Cross-platform path helpers for Mac + Windows demo runners."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def venv_python(root: Path | None = None) -> Path:
    """Prefer the project venv interpreter; fall back to current Python.

    Do not Path.resolve() the Unix venv launcher — resolving the symlink jumps
    to the base interpreter and drops site-packages from `.venv`.
    """
    root = (root or repo_root()).resolve()
    if sys.platform == "win32":
        candidates = [
            root / ".venv" / "Scripts" / "python.exe",
            root / ".venv" / "Scripts" / "python",
        ]
    else:
        candidates = [
            root / ".venv" / "bin" / "python",
            root / ".venv" / "bin" / "python3",
        ]
    for path in candidates:
        if path.exists():
            return path
    return Path(sys.executable)


def grok_home() -> Path:
    override = (os.environ.get("GROK_HOME") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".grok"


def find_grok_bin() -> Path | None:
    """Locate the grok CLI on macOS, Linux, or Windows."""
    which = shutil.which("grok")
    if which:
        return Path(which)

    home = grok_home()
    names = ("grok.exe", "grok.cmd", "grok.bat", "grok") if sys.platform == "win32" else ("grok",)
    for name in names:
        candidate = home / "bin" / name
        if candidate.is_file():
            return candidate
    return None


def ensure_grok_on_path() -> None:
    """Prepend ~/.grok/bin so child processes can find `grok`."""
    bin_dir = str(grok_home() / "bin")
    path = os.environ.get("PATH") or ""
    parts = path.split(os.pathsep) if path else []
    if bin_dir not in parts:
        os.environ["PATH"] = bin_dir + (os.pathsep + path if path else "")


def export_hackuta_env(root: Path | None = None, python: Path | None = None) -> dict[str, str]:
    """Set HACKUTA_ROOT / HACKUTA_PYTHON for portable .grok/config.toml expansion."""
    root = (root or repo_root()).resolve()
    python = python or venv_python(root)
    if not python.is_absolute():
        python = root / python
    python_s = os.path.normpath(str(python))

    os.environ["HACKUTA_ROOT"] = str(root)
    os.environ["HACKUTA_PYTHON"] = python_s
    # Windows + many CLIs behave better with UTF-8 stdio.
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    return {
        "HACKUTA_ROOT": str(root),
        "HACKUTA_PYTHON": python_s,
    }
