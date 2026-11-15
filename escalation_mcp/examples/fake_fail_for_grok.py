"""Create a fake training failure, mint an incident_id, print the Grok prompt.

Usage (from repo root):
  .\\.venv\\Scripts\\python.exe -m escalation_mcp.examples.fake_fail_for_grok

This does NOT call Grok for you. It prepares files + a concrete incident_id,
then prints the prompt (with that id already filled in).
Use local `grok` in this repo — not the cloud Desktop /home/box bot.
"""

from __future__ import annotations

import json
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from escalation_mcp.config import repo_root
from escalation_mcp.packet import new_incident_id

RUN_ID = "run_demo_nan_20261114"


def _write_failure(run_dir: Path, incident_id: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "config.yaml").write_text(
        textwrap.dedent(
            """\
            # Intentionally bad LR for HackUTA escalation demo
            model: tiny_cnn
            dataset: cifar10_toy
            batch_size: 64
            epochs: 10
            optimizer:
              name: adam
              lr: 5.0          # BUG: should be ~3e-4
              weight_decay: 0.0
            scheduler: null
            checkpoint_every: 1000
            resume_from: null
            seed: 42
            """
        ),
        encoding="utf-8",
    )

    (run_dir / "train.log").write_text(
        textwrap.dedent(
            f"""\
            {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")} INFO  starting {RUN_ID}
            INFO  config=runs/{RUN_ID}/config.yaml
            INFO  device=cuda:0
            INFO  loaded tiny_cnn params=128453
            INFO  step=100  loss=1.8421  lr=5.0
            INFO  step=500  loss=1.2104  lr=5.0
            INFO  step=1000 loss=0.8842  lr=5.0  checkpoint=ckpt_1000.pt
            INFO  step=2000 loss=0.6120  lr=5.0
            INFO  step=3000 loss=0.4481  lr=5.0
            INFO  step=4000 loss=0.2917  lr=5.0  checkpoint=ckpt_4000.pt
            INFO  step=4100 loss=0.1882  lr=5.0
            INFO  step=4180 loss=0.0941  lr=5.0
            INFO  step=4190 loss=0.0512  lr=5.0
            WARN  step=4195 loss=2.441e+03  lr=5.0
            ERROR step=4200 loss=nan  lr=5.0
            ERROR Training halted: non-finite loss detected
            Traceback (most recent call last):
              File "demo/train.py", line 188, in train_loop
                loss.backward()
              File "demo/train.py", line 142, in step
                assert torch.isfinite(loss), f"non-finite loss at step {{step}}: {{loss}}"
            AssertionError: non-finite loss at step 4200: nan
            INFO  last_good_checkpoint=runs/{RUN_ID}/ckpt_4000.pt
            """
        ),
        encoding="utf-8",
    )

    metrics = [
        {"step": 100, "loss": 1.8421, "lr": 5.0, "grad_norm": 1.12},
        {"step": 500, "loss": 1.2104, "lr": 5.0, "grad_norm": 1.48},
        {"step": 1000, "loss": 0.8842, "lr": 5.0, "grad_norm": 1.91},
        {"step": 2000, "loss": 0.6120, "lr": 5.0, "grad_norm": 2.44},
        {"step": 3000, "loss": 0.4481, "lr": 5.0, "grad_norm": 3.10},
        {"step": 4000, "loss": 0.2917, "lr": 5.0, "grad_norm": 4.22},
        {"step": 4100, "loss": 0.1882, "lr": 5.0, "grad_norm": 6.81},
        {"step": 4180, "loss": 0.0941, "lr": 5.0, "grad_norm": 12.4},
        {"step": 4190, "loss": 0.0512, "lr": 5.0, "grad_norm": 28.7},
        {"step": 4195, "loss": 2441.0, "lr": 5.0, "grad_norm": 910.0},
        {"step": 4200, "loss": None, "lr": 5.0, "grad_norm": None, "event": "nan_detected"},
    ]
    (run_dir / "metrics.jsonl").write_text(
        "\n".join(json.dumps(row) for row in metrics) + "\n",
        encoding="utf-8",
    )

    (run_dir / "INCIDENT_BRIEF.md").write_text(
        textwrap.dedent(
            f"""\
            # Demo incident brief (for local Grok)

            **Incident ID:** `{incident_id}`
            **Run ID:** `{RUN_ID}`
            **Failure:** NaN loss at step 4200
            **Likely cause:** learning rate `5.0` is far too high
            **Proposed fix:** set `optimizer.lr` to `3.0e-4`, resume from `ckpt_4000.pt`
            **Severity:** HIGH

            ## Files to read
            - `runs/{RUN_ID}/train.log`
            - `runs/{RUN_ID}/metrics.jsonl`
            - `runs/{RUN_ID}/config.yaml`
            """
        ),
        encoding="utf-8",
    )


def _prompt(incident_id: str) -> str:
    return textwrap.dedent(
        f"""\
        A local training run failed. Work ONLY with the local escalation MCP on this laptop
        (tools like escalation__escalate_failure). Do NOT use any cloud /home/box copy.

        Pre-minted incident_id (use this exact value everywhere): {incident_id}

        1. Read:
           - runs/{RUN_ID}/INCIDENT_BRIEF.md
           - runs/{RUN_ID}/train.log
           - runs/{RUN_ID}/metrics.jsonl
           - runs/{RUN_ID}/config.yaml
        2. Call escalate_failure once with:
           - incident_id="{incident_id}"
           - failure_type="nan"
           - severity="HIGH"
           - run_id="{RUN_ID}"
           - share="screen"
           - proposed_fix="Lower optimizer.lr from 5.0 to 3e-4; resume from ckpt_4000.pt"
           - put log/metrics/config excerpts into details
        3. Confirm the tool returned incident_id="{incident_id}" and print preview_url.
        4. Narrate with report_action(incident_id="{incident_id}", ...) while you diagnose.
        5. Between steps call get_human_response(incident_id="{incident_id}", wait_seconds=0).
           Before any file edit / resume, long-poll
           get_human_response(incident_id="{incident_id}", wait_seconds=25)
           until approve or reject. Never treat silence as approval.
           The call stays live after a decision so the human can keep
           watching; keep polling. decision="hangup" means the call ended
           (if run_default_action is true, do default_action).
        6. Preview URL is always http://127.0.0.1:8765.
           After approve you may draft on branch agent/fix-{incident_id}
           (do not push/merge). Finish with end_escalation; it keeps the
           monitor up for a short grace if the human is still watching.
        """
    ).strip()


def main() -> int:
    root = repo_root()
    run_dir = root / "runs" / RUN_ID
    incident_id = new_incident_id()
    _write_failure(run_dir, incident_id)

    prompt = _prompt(incident_id)
    prompt_path = run_dir / "GROK_PROMPT.txt"
    prompt_path.write_text(prompt + "\n", encoding="utf-8")
    (run_dir / "incident_id.txt").write_text(incident_id + "\n", encoding="utf-8")

    print("=" * 60)
    print("Fake failure ready.")
    print(f"Run dir:      {run_dir}")
    print(f"incident_id:  {incident_id}")
    print("=" * 60)
    print()
    print("Preview is locked to http://127.0.0.1:8765")
    print("After simulate approve, the preview keeps streaming (call_state=watching).")
    print("End the call with: simulate <id> hangup  (or leave / wait for the idle timer).")
    print("Manual cleanup tool if stuck: reset_escalation")
    print()
    print("STEP 1 — start local Grok in this repo:")
    print(f"  cd {root}")
    print("  grok mcp doctor escalation")
    print("  grok")
    print()
    print("STEP 2 — paste this prompt into Grok:")
    print("-" * 60)
    print(prompt)
    print("-" * 60)
    print()
    print(f"(Also saved to {prompt_path})")
    print()
    print("STEP 3 — open the preview_url Grok returns (usually http://127.0.0.1:8765)")
    print()
    print("STEP 4 — in another terminal, simulate your phone reply:")
    print(
        f"  .\\.venv\\Scripts\\python.exe -m escalation_mcp.simulate {incident_id} approve"
    )
    print("  # or:")
    print(
        f"  .\\.venv\\Scripts\\python.exe -m escalation_mcp.simulate {incident_id} reject"
    )
    print(
        f"  .\\.venv\\Scripts\\python.exe -m escalation_mcp.simulate {incident_id} stop"
    )
    print(
        f'  .\\.venv\\Scripts\\python.exe -m escalation_mcp.simulate {incident_id} "lower the LR and resume"'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
