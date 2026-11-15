"""In-process state for the active escalation incident."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from escalation_mcp.models import ActionEvent, EscalationPacket, HumanDecision

logger = logging.getLogger("escalation_mcp")

Outcome = Literal["resolved", "aborted", "handed_off"]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ActiveIncident:
    incident_id: str
    title: str
    severity: str
    packet: EscalationPacket
    room: str
    preview_url: str
    share: str
    created_at: str = field(default_factory=_utc_now_iso)
    seq: int = 0
    actions: list[ActionEvent] = field(default_factory=list)
    decision_queue: asyncio.Queue[HumanDecision] = field(
        default_factory=asyncio.Queue
    )
    sticky_stop: HumanDecision | None = None
    stop_banner_from: str | None = None
    overlay_actions: list[ActionEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    background_tasks: list[asyncio.Task[Any]] = field(default_factory=list)


class EscalationState:
    """One active incident at a time (MVP)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.active: ActiveIncident | None = None

    async def get_active(self) -> ActiveIncident | None:
        return self.active

    async def set_active(self, incident: ActiveIncident) -> None:
        async with self._lock:
            self.active = incident

    async def clear_active(self, incident_id: str) -> ActiveIncident | None:
        async with self._lock:
            if self.active and self.active.incident_id == incident_id:
                cleared = self.active
                self.active = None
                return cleared
            return None

    def require(self, incident_id: str) -> ActiveIncident:
        if not self.active or self.active.incident_id != incident_id:
            raise KeyError(f"no active escalation for {incident_id}")
        return self.active

    def next_seq(self, incident: ActiveIncident) -> int:
        incident.seq += 1
        return incident.seq

    def record_action(self, incident: ActiveIncident, event: ActionEvent) -> None:
        incident.actions.append(event)
        incident.overlay_actions = ([event] + incident.overlay_actions)[:3]

    async def enqueue_decision(self, decision: HumanDecision) -> None:
        if not self.active or self.active.incident_id != decision.incident_id:
            logger.warning(
                "ignoring decision for inactive incident %s", decision.incident_id
            )
            return
        if decision.decision == "stop":
            self.active.sticky_stop = decision
            self.active.stop_banner_from = decision.from_ or "human"
        await self.active.decision_queue.put(decision)

    async def get_human_response(
        self, incident_id: str, wait_seconds: int = 0
    ) -> dict[str, Any]:
        incident = self.require(incident_id)
        if incident.sticky_stop is not None:
            d = incident.sticky_stop
            return {
                "decision": "stop",
                "text": d.text,
                "from": d.from_,
                "ts": d.ts,
                "pending_count": incident.decision_queue.qsize(),
            }

        wait = max(0, min(int(wait_seconds), 25))
        try:
            if wait <= 0:
                decision = incident.decision_queue.get_nowait()
            else:
                decision = await asyncio.wait_for(
                    incident.decision_queue.get(), timeout=wait
                )
        except (asyncio.QueueEmpty, asyncio.TimeoutError):
            return {
                "decision": "none",
                "text": None,
                "from": None,
                "ts": None,
                "pending_count": incident.decision_queue.qsize(),
            }

        if decision.decision == "stop":
            incident.sticky_stop = decision
            incident.stop_banner_from = decision.from_ or "human"

        return {
            "decision": decision.decision,
            "text": decision.text,
            "from": decision.from_,
            "ts": decision.ts,
            "pending_count": incident.decision_queue.qsize(),
        }


STATE = EscalationState()
