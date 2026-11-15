#!/usr/bin/env python3
"""Live loss viewer for the demo: runs in its own terminal window.

Follows <run-dir>/metrics.jsonl as training writes it (healthy -> explosion),
then waits for Grok's verification runs (<run-dir>_verify*/) and plays their
real logged losses back so the recovery is visible on screen.

  python demo/loss_view.py --run-dir runs/run_2026-11-15_120000
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

RED, GREEN, YELLOW, DIM, BOLD, RESET = (
    "\033[91m", "\033[92m", "\033[93m", "\033[2m", "\033[1m", "\033[0m"
)
BAR_W = 34


def _bar(loss: float | None) -> str:
    """Log-scale bar: 0.001 -> empty, 1e36 -> full."""
    if loss is None or not math.isfinite(loss):
        return "█" * BAR_W
    lg = math.log10(max(loss, 1e-3))
    frac = min(1.0, max(0.0, (lg + 3) / 39))
    return "█" * max(1, round(frac * BAR_W))


def _fmt_loss(loss: float | None) -> str:
    if loss is None or not math.isfinite(loss):
        return "     inf"
    return f"{loss:8.3g}" if loss < 1e4 else f"{loss:8.1e}"


def _row_line(row: dict, color: str, warmup: bool = False) -> str:
    loss = row.get("loss")
    bad = row.get("event") == "non_finite" or loss is None or (loss is not None and loss > 1e3)
    c = RED if bad else color
    lr = row.get("lr")
    lr_s = f"{DIM}lr {lr:g} (warm-up){RESET}" if warmup else f"{DIM}lr {lr:g}{RESET}"
    return (
        f"  step {row.get('step', '?'):>5}   loss {c}{_fmt_loss(loss)}{RESET}  "
        f"{c}{_bar(loss)}{RESET}  {lr_s}"
    )


def _run_meta(run: Path) -> tuple[int, float | None]:
    """(warmup_steps, configured lr) from train.py's config-<attempt>.json, if present."""
    for meta in sorted(run.glob("config-*.json")):
        try:
            d = json.loads(meta.read_text())
            return int(d.get("warmup_steps") or 0), (d.get("config") or {}).get("lr")
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return 0, None


def _read_rows(path: Path, start: int) -> tuple[list[dict], int]:
    if not path.exists():
        return [], start
    with path.open(encoding="utf-8") as fh:
        fh.seek(start)
        data = fh.read()
    complete, _, _partial = data.rpartition("\n")
    if not complete:
        return [], start
    rows = []
    for line in complete.splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows, start + len(complete.encode("utf-8")) + 1


def _run_finished(run: Path) -> bool:
    try:
        status = json.loads((run / "status.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return status.get("status") in {"completed", "paused", "failed", "interrupted"}


def _header(title: str, color: str) -> None:
    print(f"\n{color}{BOLD}━━ {title} {'━' * max(0, 58 - len(title))}{RESET}", flush=True)


def follow_training(run: Path, title: str | None = None, color: str = YELLOW) -> None:
    """Print rows live as training writes them, until NaN or the run finishes."""
    _header(title or f"TRAINING  {run.name}", color)
    print(f"{DIM}  waiting for metrics…{RESET}", flush=True)
    offset = 0
    first = last = None
    warmup, cfg_lr = 0, None
    announced = False
    while True:
        rows, offset = _read_rows(run / "metrics.jsonl", offset)
        if rows and not warmup:
            warmup, cfg_lr = _run_meta(run)
        for row in rows:
            step = row.get("step") or 0
            in_warmup = bool(warmup) and step <= warmup
            if warmup and not in_warmup and not announced:
                announced = True
                lr_txt = f"{cfg_lr:g}" if isinstance(cfg_lr, (int, float)) else "configured value"
                print(f"  {DIM}── warm-up over: lr → {lr_txt} ──{RESET}", flush=True)
            if row.get("event") == "non_finite":
                print(_row_line(row, color), flush=True)
                print(f"\n  {RED}{BOLD}✗ LOSS EXPLODED → NaN at step {row.get('step')}{RESET}", flush=True)
                return
            print(_row_line(row, color, warmup=in_warmup), flush=True)
            if isinstance(row.get("loss"), (int, float)):
                first = row["loss"] if first is None else first
                last = (row["loss"], row.get("step"))
        if not rows and _run_finished(run):
            if color == GREEN and first is not None and last is not None:
                print(f"\n  {GREEN}{BOLD}✓ trained to step {last[1]}: loss {first:.3g} → {last[0]:.3g}{RESET}", flush=True)
            return
        time.sleep(0.1)


VIEWER_TITLE = "Mentor loss"  # set by run_hands_off when it opens this window


def _raise_window() -> None:
    """macOS: bring this Terminal window to the front (VS Code may be covering it)."""
    if sys.platform != "darwin":
        return
    script = f'''
    tell application "Terminal"
        repeat with w in windows
            try
                if custom title of selected tab of w is "{VIEWER_TITLE}" then
                    set index of w to 1
                    activate
                end if
            end try
        end repeat
    end tell'''
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:  # noqa: BLE001
        pass


def replay_verify(run: Path, delay: float) -> None:
    _raise_window()
    rows, _ = _read_rows(run / "metrics.jsonl", 0)
    rows = [r for r in rows if r.get("event") in (None, "metric", "non_finite")]
    _header("VERIFY RUN (after fix)", GREEN)
    for row in rows:
        print(_row_line(row, GREEN), flush=True)
        time.sleep(delay)
    finite = [r["loss"] for r in rows if isinstance(r.get("loss"), (int, float))]
    if any(r.get("event") == "non_finite" for r in rows):
        print(f"\n  {RED}{BOLD}✗ still diverging{RESET}", flush=True)
    elif len(finite) >= 2:
        print(
            f"\n  {GREEN}{BOLD}✓ stable: loss {finite[0]:.3g} → {finite[-1]:.3g} "
            f"in {rows[-1].get('step')} steps{RESET}",
            flush=True,
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--replay-delay", type=float, default=0.35, help="seconds per verify row on screen")
    args = ap.parse_args()
    run: Path = args.run_dir

    print(f"{BOLD}Mentor · live training monitor{RESET}", flush=True)
    follow_training(run)

    seen: set[Path] = set()
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    i = 0
    print(f"\n{DIM}  calling you… waiting for the fix{RESET}", flush=True)
    while True:
        for vdir in sorted(run.parent.glob(f"{run.name}_verify*")):
            if vdir in seen or not _run_finished(vdir):
                continue
            seen.add(vdir)
            sys.stdout.write("\r" + " " * 40 + "\r")
            replay_verify(vdir, args.replay_delay)
            print(f"\n{DIM}  watching for more changes…{RESET}", flush=True)
        # Grok starts the full training run on the fixed code after the call.
        for rdir in sorted(run.parent.glob(f"{run.name}_rerun*")):
            if rdir in seen:
                continue
            seen.add(rdir)
            sys.stdout.write("\r" + " " * 40 + "\r")
            _raise_window()
            follow_training(rdir, title="FULL TRAINING (fixed code)", color=GREEN)
            print(f"\n{DIM}  watching for more changes…{RESET}", flush=True)
        sys.stdout.write(f"\r  {DIM}{spinner[i % len(spinner)]}{RESET} ")
        sys.stdout.flush()
        i += 1
        time.sleep(0.15)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
