"""Simulate the phone side in mock mode by appending to runs/<id>/inbox.jsonl.

Usage:
  python -m escalation_mcp.simulate <incident_id> approve|reject|stop|hangup|"free text"
  python -m escalation_mcp.simulate <incident_id> join|leave

  approve / reject / stop  -> human decision
  "free text"              -> instruction (your custom suggestion)
  hangup                   -> you say "that's all, hang up" on the call
  join / leave             -> you pick up / hang up the phone (caller presence)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from escalation_mcp.config import runs_dir

DECISIONS = {"approve", "reject", "stop", "hangup"}
PRESENCE = {"join": "joined", "leave": "left"}


def build_record(incident_id: str, raw: str) -> dict[str, object]:
    ts = datetime.now(timezone.utc).isoformat()
    word = raw.strip().lower()
    if word in PRESENCE:
        return {"type": "presence", "incident_id": incident_id, "ts": ts,
                "event": PRESENCE[word]}
    if word in DECISIONS:
        decision, text = word, None
    else:
        decision, text = "instruction", raw.strip()
    return {
        "type": "decision",
        "incident_id": incident_id,
        "ts": ts,
        "decision": decision,
        "text": text,
        "from": "simulate",
    }


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    incident_id = args[0]
    record = build_record(incident_id, " ".join(args[1:]))

    inbox = runs_dir() / incident_id / "inbox.jsonl"
    if not inbox.parent.exists():
        print(f"incident dir not found: {inbox.parent}", file=sys.stderr)
        return 1

    with inbox.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps({"ok": True, "wrote": record}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
