"""Read runs/<run_id>/metrics.jsonl. Owner: Binoy."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_metrics(path: str | Path, last_n: int | None = None) -> list[dict[str, Any]]:
    """Return parsed metric rows; skips blank/partial lines (file may be mid-write)."""
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))  # json accepts NaN / Infinity
        except json.JSONDecodeError:
            continue
    return rows[-last_n:] if last_n else rows


class MetricsTail:
    """Incrementally read new rows from a growing metrics.jsonl."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._pos = 0
        self._buf = ""

    def new_rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open() as f:
            f.seek(self._pos)
            chunk = f.read()
            self._pos = f.tell()
        self._buf += chunk
        *lines, self._buf = self._buf.split("\n")
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
