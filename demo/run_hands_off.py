#!/usr/bin/env python3
"""Hands-off escalate demo — works on macOS and Windows.

Default flow (the 60s demo):
  1. Run demo/train.py on demo/configs/bad_lr.yaml under our watcher, output
     shown live. It trains normally for ~7s (warm-up), then diverges to NaN.
  2. The watcher catches it and saves an incident (log tail, metrics, config).
  3. Grok CLI starts on that incident and escalates right away (phone rings),
     then applies + verifies the fixes the caller approves. The diagnosis is
     pre-written in hands_off_escalate_prompt.txt for demo speed; --diagnose
     uses hands_off_diagnose_prompt.txt to make Grok find the bugs itself.

Usage (from repo root, venv optional if .venv exists):
  python demo/run_hands_off.py                 # train -> catch -> Grok
  python demo/run_hands_off.py --reset         # first restore buggy train.py + bad_lr.yaml
  python demo/run_hands_off.py --no-train      # skip training (Grok uses a canned incident)
  python demo/run_hands_off.py --diagnose      # Grok diagnoses the code live (slower: ~1-2 min to ring)
  python demo/run_hands_off.py --no-viewer     # don't open the live loss window
  python demo/run_hands_off.py --warmup-steps 30
  ./demo/run_hands_off.sh / demo\\run_hands_off.cmd   (same flags)
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Allow `python demo/run_hands_off.py` without installing the package.
_DEMO = Path(__file__).resolve().parent
for _p in (_DEMO, _DEMO.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from platform_paths import (  # noqa: E402
    ensure_grok_on_path,
    export_hackuta_env,
    find_grok_bin,
    repo_root,
    venv_python,
)

DEMO_FILES = ("demo/train.py", "demo/configs/bad_lr.yaml")
BAD_CONFIG = "demo/configs/bad_lr.yaml"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def _reset_demo_files(root: Path) -> None:
    """Restore the deliberately buggy train.py + bad_lr.yaml from HEAD."""
    res = _git(root, "checkout", "--", *DEMO_FILES)
    if res.returncode != 0:
        print(f"reset failed: {res.stderr.strip()}", file=sys.stderr)
    else:
        print(f"reset: restored {', '.join(DEMO_FILES)} from HEAD")


def _warn_if_demo_files_changed(root: Path) -> None:
    res = _git(root, "status", "--porcelain", "--", *DEMO_FILES)
    if res.stdout.strip():
        print(
            "warning: demo files differ from HEAD (a previous run's fix?):\n"
            f"{res.stdout.rstrip()}\n"
            "  run with --reset to restore the buggy versions for a clean demo.",
            file=sys.stderr,
        )


VIEWER_TITLE = "Mentor loss"
# Fraction of the screen width VS Code keeps on the left; the viewer takes the rest.
EDITOR_SPLIT = 0.64

_MAC_LAYOUT = """
on run argv
    set shellCmd to item 1 of argv
    set splitFrac to (item 2 of argv) as real
    tell application "Finder" to set sb to bounds of window of desktop
    set W to item 3 of sb
    set H to item 4 of sb
    set splitX to round (W * splitFrac)
    tell application "Terminal"
        set t to do script shellCmd
        set custom title of t to "%(title)s"
        set bounds of front window to {splitX, 25, W, H}
        activate
    end tell
    try
        tell application "System Events"
            tell (first process whose bundle identifier is "com.microsoft.VSCode")
                set position of window 1 to {0, 25}
                set size of window 1 to {splitX, H - 25}
            end tell
        end tell
        return "vscode:tiled"
    on error errMsg
        return "vscode:" & errMsg
    end try
end run
""" % {"title": VIEWER_TITLE}


def _open_loss_viewer(root: Path, py: Path, run_dir: Path) -> None:
    """Open demo/loss_view.py in its own window, tiled beside VS Code (stays up during the call)."""
    args = [str(py), "demo/loss_view.py", "--run-dir", str(run_dir.relative_to(root))]
    try:
        if sys.platform == "darwin":
            shell_cmd = f"cd {shlex.quote(str(root))} && clear && " + " ".join(shlex.quote(a) for a in args)
            res = subprocess.run(
                ["osascript", "-e", _MAC_LAYOUT, shell_cmd, str(EDITOR_SPLIT)],
                check=True, capture_output=True, text=True, timeout=15,
            )
            if "vscode:tiled" not in res.stdout:
                print(
                    "[viewer] couldn't move VS Code (needs Accessibility permission for this "
                    "terminal app: System Settings > Privacy & Security > Accessibility). "
                    f"Drag VS Code to the left ~{int(EDITOR_SPLIT * 100)}% of the screen so the "
                    "loss window on the right stays visible.",
                    file=sys.stderr,
                )
            return
        if sys.platform == "win32":
            subprocess.Popen(["cmd", "/c", "start", "Mentor loss", "cmd", "/k", *args], cwd=root)
            return
    except Exception as exc:  # noqa: BLE001
        print(f"[viewer] could not open a window ({exc})", file=sys.stderr)
    print(f"[viewer] run this in another terminal:\n  {' '.join(args)}", file=sys.stderr)


def _demo_is_broken(root: Path) -> bool:
    """True if train.py + bad_lr.yaml still contain the planted bugs the demo needs."""
    try:
        cfg = (root / BAD_CONFIG).read_text(encoding="utf-8")
        src = (root / "demo" / "train.py").read_text(encoding="utf-8")
    except OSError:
        return False
    return "lr: 20.0" in cfg and "np.logaddexp(np.float32(0.0), logits)" in src


def _train_until_incident(root: Path, py: Path, warmup_steps: int, run_dir: Path) -> tuple[Path, Path, list[str]] | None:
    """Run train.py under the watcher; return (incident_json, run_dir, cmd) on failure."""
    from incident.builder import build_incident
    from incident.store import IncidentStore
    from watcher.detectors import Detectors
    from watcher.runner import Runner

    runs_dir = run_dir.parent
    cmd = [
        str(py), "demo/train.py",
        "--config", BAD_CONFIG,
        "--run-dir", str(run_dir.relative_to(root)),
        "--warmup-steps", str(warmup_steps),
    ]
    store = IncidentStore(runs_dir)
    found: list[Path] = []

    def handle(det, runner) -> None:  # type: ignore[no-untyped-def]
        if found:
            return  # first failure is the incident; ignore follow-on detections
        if det.type not in {"nan", "crash", "oom"}:
            # Low-severity (overfit/plateau) can fire during warm-up noise;
            # the demo incident is the hard failure.
            print(f"[watcher] note: {det.type} at step {det.step} (low severity, ignored)", flush=True)
            return
        inc = build_incident(det, runner.run_id, runner.log_tail, runner.metrics_path, BAD_CONFIG, root)
        found.append(store.save(inc))
        print(f"\n[watcher] {det.type.upper()} at step {det.step} -> incident {inc.id}", flush=True)

    print(f"[watcher] starting: {' '.join(cmd[1:])}\n", flush=True)
    code = Runner(cmd, run_dir, handle, Detectors(), cwd=root, echo=True).run()
    print(f"[watcher] training exited with code {code}", flush=True)
    if not found:
        return None
    return found[0], run_dir, cmd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--no-train", action="store_true", help="skip training; escalate a canned incident")
    ap.add_argument("--reset", action="store_true", help="restore buggy demo files from HEAD first")
    ap.add_argument("--warmup-steps", type=int, default=60, help="normal-looking steps before divergence")
    ap.add_argument("--no-viewer", action="store_true", help="don't open the live loss window")
    ap.add_argument("--diagnose", action="store_true",
                    help="Grok diagnoses the code live instead of using the pre-written diagnosis (slower)")
    args, grok_args = ap.parse_known_args(argv if argv is not None else sys.argv[1:])

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

    prompt_name = "hands_off_diagnose_prompt.txt" if args.diagnose else "hands_off_escalate_prompt.txt"
    prompt_path = root / "demo" / prompt_name
    if not prompt_path.is_file():
        print(f"missing prompt: {prompt_path}", file=sys.stderr)
        return 1
    prompt = prompt_path.read_text(encoding="utf-8")

    if args.reset:
        _reset_demo_files(root)
    else:
        _warn_if_demo_files_changed(root)

    print(f"repo:   {root}")
    print(f"python: {env_vars['HACKUTA_PYTHON']}")
    print(f"grok:   {grok}")

    if not args.no_train and not _demo_is_broken(root):
        print(
            "demo files are already FIXED (a previous call's fix is still applied), so training "
            "would just succeed.\n  Restore them first:  python demo/run_hands_off.py --reset\n"
            "  (--reset restores demo/train.py + bad_lr.yaml from the last commit)",
            file=sys.stderr,
        )
        return 1

    if args.no_train:
        prompt += (
            "\n\nINCIDENT: none on disk (training was skipped). Assume training on "
            f"{BAD_CONFIG} diverged to NaN within ~20 steps; diagnose from the code.\n"
        )
    else:
        run_dir = root / "runs" / f"run_{datetime.now():%Y-%m-%d_%H%M%S}"
        if not args.no_viewer:
            _open_loss_viewer(root, py, run_dir)
        result = _train_until_incident(root, py, args.warmup_steps, run_dir)
        if result is None:
            print("training finished without an incident; nothing to escalate.", file=sys.stderr)
            return 1
        incident_path, run_dir, cmd = result
        verify_dir = run_dir.parent / f"{run_dir.name}_verify"
        prompt += (
            f"\n\nINCIDENT FILE: {incident_path.relative_to(root)}\n"
            f"FAILED RUN DIR: {run_dir.relative_to(root)}\n"
            f"VERIFY COMMAND (after fixes; fresh run dir each time, add a suffix if it exists):\n"
            f"  {env_vars['HACKUTA_PYTHON']} demo/train.py --config {BAD_CONFIG} "
            f"--run-dir {verify_dir.relative_to(root)} --max-steps 60 --step-delay 0\n"
            f"RERUN COMMAND (after the call ends, if fixes were applied; start it in the background):\n"
            f"  nohup {env_vars['HACKUTA_PYTHON']} demo/train.py --config {BAD_CONFIG} "
            f"--run-dir {(run_dir.parent / f'{run_dir.name}_rerun').relative_to(root)} --max-steps 500 "
            f"> /dev/null 2>&1 &\n"
        )

    print("Launching hands-off escalate (answer the phone; no TUI approve)…")
    # Interactive TUI — do not use -p. always-approve so phone is the only UI.
    cli = ["--always-approve", "--fullscreen", prompt, *grok_args]
    if sys.platform == "win32" and grok.suffix.lower() in {".cmd", ".bat"}:
        cmd = ["cmd", "/c", str(grok), *cli]
    else:
        cmd = [str(grok), *cli]
    return subprocess.call(cmd, cwd=str(root), env=os.environ.copy())


if __name__ == "__main__":
    raise SystemExit(main())
