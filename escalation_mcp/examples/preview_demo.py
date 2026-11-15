"""Seed is already on disk; start a mock escalation so you can open the preview.

Usage (repo root, venv active):
  python -m escalation_mcp.examples.preview_demo

Then open the printed preview_url in a browser. In another terminal:
  python -m escalation_mcp.simulate <incident_id> approve

Ctrl+C ends the escalation.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from escalation_mcp.config import Settings, repo_root
from escalation_mcp import session as sess
from escalation_mcp.channel.mock import MockChannel
from escalation_mcp.sink.mock import MockSink

RUN_ID = "run_demo_nan_20261114"


def _load_details() -> dict:
    root = repo_root() / "runs" / RUN_ID
    log_lines = (root / "train.log").read_text(encoding="utf-8").splitlines()
    metrics = [
        json.loads(line)
        for line in (root / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    config_text = (root / "config.yaml").read_text(encoding="utf-8")
    return {
        "traceback": "\n".join(log_lines[-8:]),
        "log_tail": log_lines[-20:],
        "metrics_tail": metrics[-8:],
        "config": {"raw": config_text, "lr": 5.0},
        "files": [
            f"runs/{RUN_ID}/train.log",
            f"runs/{RUN_ID}/metrics.jsonl",
            f"runs/{RUN_ID}/config.yaml",
        ],
        "git_diff": (
            "--- a/runs/run_demo_nan_20261114/config.yaml\n"
            "+++ b/runs/run_demo_nan_20261114/config.yaml\n"
            "@@\n-  lr: 5.0\n+  lr: 3.0e-4\n"
        ),
    }


async def _run() -> int:
    run_dir = repo_root() / "runs" / RUN_ID
    if not run_dir.exists():
        print(f"missing demo run dir: {run_dir}", file=sys.stderr)
        return 1

    sess.configure_runtime(
        settings=Settings(mode="mock", preview_port=8765, screen_fps=5.0),
        sink=MockSink(),
        channel=MockChannel(),
        fake_frames=None,  # real screen capture on this laptop
    )

    result = await sess.start_escalation(
        title="Training run crashed: NaN loss at step 4200",
        summary=(
            "Loss became NaN at step 4200. Learning rate 5.0 looks too high. "
            "Propose lowering lr to 3e-4 and resuming from ckpt_4000."
        ),
        failure_type="nan",
        severity="HIGH",
        details=_load_details(),
        proposed_fix="Lower optimizer.lr from 5.0 to 3e-4; resume from ckpt_4000.pt",
        run_id=RUN_ID,
        share="screen",
    )
    print(json.dumps(result, indent=2))
    incident_id = result["incident_id"]
    preview = result.get("preview_url") or "http://127.0.0.1:8765"
    print()
    print("=== Escalation preview running ===")
    print(f"Open in browser:  {preview}")
    print(f"Packet:           runs/{incident_id}/packet.json")
    print(f"Simulate approve: python -m escalation_mcp.simulate {incident_id} approve")
    print(f"Simulate stop:    python -m escalation_mcp.simulate {incident_id} stop")
    print("Ctrl+C to end_escalation and exit.")
    print()

    # Narrate a few actions so the overlay updates
    await sess.report_action(incident_id, "read_file", f"runs/{RUN_ID}/train.log", "done")
    await asyncio.sleep(0.3)
    await sess.report_action(incident_id, "read_file", f"runs/{RUN_ID}/config.yaml", "done")
    await asyncio.sleep(0.3)
    await sess.report_action(
        incident_id, "diagnose", "LR 5.0 too high → propose 3e-4", "started"
    )

    from escalation_mcp.state import STATE

    try:
        while True:
            decision = await STATE.get_human_response(incident_id, wait_seconds=2)
            if decision["decision"] == "none":
                continue
            print("human decision:", json.dumps(decision))
            if decision["decision"] == "stop":
                await sess.end_escalation(incident_id, "aborted", "stopped during preview demo")
                return 0
            if decision["decision"] in ("approve", "reject", "instruction"):
                await sess.report_action(
                    incident_id,
                    "human_decision",
                    f"{decision['decision']}: {decision.get('text')}",
                    "done",
                )
                await sess.end_escalation(
                    incident_id,
                    "resolved" if decision["decision"] == "approve" else "aborted",
                    f"preview demo finished with {decision['decision']}",
                )
                return 0
    except KeyboardInterrupt:
        print("\nending escalation...")
        await sess.end_escalation(incident_id, "aborted", "preview demo interrupted")
        return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
