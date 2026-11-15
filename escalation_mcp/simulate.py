"""Append a human decision to runs/<id>/inbox.jsonl for mock-mode testing.

Usage:
  python -m escalation_mcp.simulate <incident_id> approve|reject|stop|"free text"
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from escalation_mcp.config import runs_dir


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) < 2:
        print(
            "Usage: python -m escalation_mcp.simulate <incident_id> "
            "approve|reject|stop|\"free text\"",
            file=sys.stderr,
        )
        return 2

    incident_id = args[0]
    raw = " ".join(args[1:]).strip()
    known = {"approve", "reject", "stop"}
    if raw.lower() in known:
        decision = raw.lower()
        text = None
    else:
        decision = "instruction"
        text = raw

    inbox = runs_dir() / incident_id / "inbox.jsonl"
    if not inbox.parent.exists():
        print(f"incident dir not found: {inbox.parent}", file=sys.stderr)
        return 1
    inbox.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "type": "decision",
        "incident_id": incident_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "decision": decision,
        "text": text,
        "from": "simulate",
    }
    with inbox.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps({"ok": True, "wrote": record}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
