"""Triage prompt construction. Owner: Binoy."""
from __future__ import annotations

import json
from pathlib import Path

from incident.models import Incident

SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "watcher.md"


def system_prompt() -> str:
    skill = SKILL_PATH.read_text() if SKILL_PATH.exists() else ""
    return (skill + "\n\nRespond with ONLY the JSON object described in Output. No prose, no code fences.").strip()


def user_prompt(inc: Incident) -> str:
    payload = inc.model_dump(mode="json", exclude={"triage", "status"})
    payload["log_tail"] = payload["log_tail"][-80:]
    payload["metrics_tail"] = payload["metrics_tail"][-20:]
    return "Incident:\n" + json.dumps(payload, indent=2, default=str)
