#!/usr/bin/env python3
"""Hands-off escalate demo — works on macOS and Windows.

Usage (from repo root, venv optional if .venv exists):
  python demo/run_hands_off.py
  ./demo/run_hands_off.sh          # mac/linux
  demo\\run_hands_off.cmd          # windows
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Allow `python demo/run_hands_off.py` without installing the package.
_DEMO = Path(__file__).resolve().parent
if str(_DEMO) not in sys.path:
    sys.path.insert(0, str(_DEMO))

from platform_paths import (  # noqa: E402
    ensure_grok_on_path,
    export_hackuta_env,
    find_grok_bin,
    repo_root,
    venv_python,
)


def _reset_bad_lr_if_needed(root: Path) -> None:
    cfg = root / "demo" / "configs" / "bad_lr.yaml"
    if not cfg.is_file():
        return
    text = cfg.read_text(encoding="utf-8")
    if "3.0e-4" in text or "3e-4" in text:
        # Leave alone — user may have already reset; only nudge via stderr.
        if "1e-2" not in text and "0.01" not in text:
            print(
                "note: demo/configs/bad_lr.yaml looks already fixed; "
                "reset lr to 1e-2 if you want the NaN demo again.",
                file=sys.stderr,
            )


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    root = repo_root()
    os.chdir(root)

    py = venv_python(root)
    env_vars = export_hackuta_env(root, py)
    ensure_grok_on_path()

    grok = find_grok_bin()
    if grok is None:
        print(
            "grok CLI not found. Install Grok CLI, then ensure it's on PATH "
            "or at ~/.grok/bin (Windows: %USERPROFILE%\\.grok\\bin).",
            file=sys.stderr,
        )
        return 1

    prompt_path = root / "demo" / "hands_off_escalate_prompt.txt"
    if not prompt_path.is_file():
        print(f"missing prompt: {prompt_path}", file=sys.stderr)
        return 1
    prompt = prompt_path.read_text(encoding="utf-8")

    _reset_bad_lr_if_needed(root)

    print(f"repo:   {root}")
    print(f"python: {env_vars['HACKUTA_PYTHON']}")
    print(f"grok:   {grok}")
    print("Launching hands-off escalate (answer the phone; no TUI approve)…")

    # Interactive TUI — do not use -p. always-approve so phone is the only UI.
    args = ["--always-approve", "--fullscreen", prompt, *argv]
    if sys.platform == "win32" and grok.suffix.lower() in {".cmd", ".bat"}:
        cmd = ["cmd", "/c", str(grok), *args]
    else:
        cmd = [str(grok), *args]
    return subprocess.call(cmd, cwd=str(root), env=os.environ.copy())


if __name__ == "__main__":
    raise SystemExit(main())
