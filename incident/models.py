"""Pydantic Incident schema (see MVP.md section 4). Owner: Binoy."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

IncidentType = Literal["nan", "crash", "oom", "plateau", "overfit"]
Severity = Literal["LOW", "HIGH"]
Status = Literal["open", "notified", "calling", "fixing", "resolved"]


class Fix(BaseModel):
    kind: Literal["config", "code"] = "config"
    changes: dict[str, Any] = Field(default_factory=dict)
    patch: Optional[str] = None


class Triage(BaseModel):
    summary: str
    cause: str
    fix: Fix
    severity: Severity


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: f"inc_{uuid4().hex[:8]}")
    run_id: str
    type: IncidentType
    step: Optional[int] = None
    detected_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    traceback: Optional[str] = None
    log_tail: list[str] = Field(default_factory=list)
    metrics_tail: list[dict[str, Any]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    git_diff: str = ""
    triage: Optional[Triage] = None
    status: Status = "open"
    source: dict[str, str] = Field(default_factory=dict)

    def default_severity(self) -> Severity:
        return "HIGH" if self.type in ("nan", "crash", "oom") else "LOW"