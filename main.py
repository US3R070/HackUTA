"""Entrypoint: start watcher + webhook server + agent worker."""
from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()
import argparse
from datetime import datetime
from pathlib import Path
from typing import Callable
from chert.notify import notify
from chert.escalate import escalate, wait_forever
import yaml

from incident.builder import build_incident
from incident.models import Incident
from incident.store import IncidentStore
from triage.grok import grok_triage
from watcher.detectors import Detection, Detectors
from watcher.runner import Runner

# ---- hand-off: Ming registers chert notify/escalate here ----
HANDLERS: list[Callable[[Incident], None]] = []


def register(handler: Callable[[Incident], None]) -> None:
    HANDLERS.append(handler)

register(escalate)
register(notify)
def on_incident(inc: Incident) -> None:
    if not HANDLERS:
        print(f"\n=== INCIDENT {inc.id} ({inc.type}, {inc.triage.severity if inc.triage else '?'}) ===")
        print(inc.triage.model_dump_json(indent=2) if inc.triage else "(no triage)")
    for h in HANDLERS:
        h(inc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None, help="training config yaml (included in incident)")
    ap.add_argument("--runs-dir", default=None)
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="-- <training command>, {run_dir} is substituted")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path("config.yaml").read_text())
    runs_dir = Path(args.runs_dir or cfg["runs_dir"])
    run_dir = runs_dir / f"run_{datetime.now():%Y-%m-%d_%H%M%S}"
    cmd = [c.replace("{run_dir}", str(run_dir)) for c in args.cmd if c != "--"]
    if not cmd:
        ap.error("provide a training command after --")

    store = IncidentStore(runs_dir)
    d = cfg.get("detectors", {})
    detectors = Detectors(plateau_window=d.get("plateau_window_steps", 500),
                          plateau_min_improvement=d.get("plateau_min_improvement", 0.01))

    def handle(det: Detection, runner: Runner) -> None:
        inc = build_incident(det, runner.run_id, runner.log_tail, runner.metrics_path, args.config)
        inc.triage = grok_triage(inc)
        store.save(inc)
        on_incident(inc)

    code = Runner(cmd, run_dir, handle, detectors, log_tail_lines=cfg.get("log_tail_lines", 200)).run()
    print(f"[watcher] training exited with code {code}")

    if HANDLERS:
        print("[main] escalation active; Ctrl+C to end")
        wait_forever()


if __name__ == "__main__":
    main()
