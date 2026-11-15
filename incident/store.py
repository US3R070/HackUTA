"""JSON persistence: runs/<run_id>/incidents/<id>.json. Owner: Binoy."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from incident.models import Incident


class IncidentStore:
    def __init__(self, runs_dir: str | Path = "runs"):
        self.runs_dir = Path(runs_dir)

    def _path(self, run_id: str, inc_id: str) -> Path:
        return self.runs_dir / run_id / "incidents" / f"{inc_id}.json"

    def save(self, inc: Incident) -> Path:
        p = self._path(inc.run_id, inc.id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(inc.model_dump_json(indent=2))
        return p

    def get(self, run_id: str, inc_id: str) -> Optional[Incident]:
        p = self._path(run_id, inc_id)
        return Incident.model_validate_json(p.read_text()) if p.exists() else None

    def latest(self) -> Optional[Incident]:
        files = sorted(self.runs_dir.glob("*/incidents/*.json"), key=lambda f: f.stat().st_mtime)
        return Incident.model_validate_json(files[-1].read_text()) if files else None
