"""In-process state for the active escalation incident."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from escalation_mcp.models import (
    DEFAULT_SAFE_ACTION,
    ActionEvent,
    EscalationPacket,
    HumanDecision,
)

if TYPE_CHECKING:
    from escalation_mcp.call import CallController

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
    default_action: str = DEFAULT_SAFE_ACTION
    call: "CallController | None" = None
    last_decision: HumanDecision | None = None
    # Set by end_escalation while the call is still live (agent done, human watching).
    agent_outcome: tuple[str, str] | None = None


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
        self.active.last_decision = decision
        if decision.decision == "hangup":
            return  # call lifecycle signal, handled by the CallController
        if decision.decision == "stop":
            self.active.sticky_stop = decision
            self.active.stop_banner_from = decision.from_ or "human"
        await self.active.decision_queue.put(decision)

    @staticmethod
    def _call_info(incident: ActiveIncident) -> dict[str, Any]:
        call = incident.call
        if call is None:
            return {"call_state": None, "ends_in_s": None, "end_reason": None}
        info: dict[str, Any] = dict(call.snapshot())
        if call.finished:
            info["default_action"] = incident.default_action
            # Default move only when the human never made a real decision.
            info["run_default_action"] = not call.human_decided
        return info

    @staticmethod
    def _decision_dict(d: HumanDecision, incident: ActiveIncident) -> dict[str, Any]:
        return {
            "decision": d.decision,
            "text": d.text,
            "from": d.from_,
            "ts": d.ts,
            "pending_count": incident.decision_queue.qsize(),
        }

    def _ended_dict(self, incident: ActiveIncident) -> dict[str, Any]:
        return {
            "decision": "hangup",
            "text": None,
            "from": None,
            "ts": None,
            "pending_count": 0,
            **self._call_info(incident),
        }

    async def get_human_response(
        self, incident_id: str, wait_seconds: int = 0
    ) -> dict[str, Any]:
        """Oldest unconsumed decision. Order: sticky stop > queued decisions > call ended.

        Once the call has ended and the queue is drained, every call returns
        decision="hangup" with end_reason / run_default_action (sticky).
        """
        incident = self.require(incident_id)
        if incident.sticky_stop is not None:
            return {
                **self._decision_dict(incident.sticky_stop, incident),
                **self._call_info(incident),
            }

        decision: HumanDecision | None = None
        try:
            decision = incident.decision_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        call = incident.call
        if decision is None and call is not None and call.finished:
            return self._ended_dict(incident)

        wait = max(0, min(int(wait_seconds), 25))
        if decision is None and wait > 0:
            getter = asyncio.ensure_future(incident.decision_queue.get())
            waiters: set[asyncio.Future[Any]] = {getter}
            if call is not None:
                waiters.add(asyncio.ensure_future(call.wait_ended(wait)))
            done, pending = await asyncio.wait(
                waiters, timeout=wait, return_when=asyncio.FIRST_COMPLETED
            )
            for fut in pending:
                fut.cancel()
            if getter in done:
                decision = getter.result()
            elif call is not None and call.finished:
                return self._ended_dict(incident)

        if decision is None:
            return {
                "decision": "none",
                "text": None,
                "from": None,
                "ts": None,
                "pending_count": incident.decision_queue.qsize(),
                **self._call_info(incident),
            }

        if decision.decision == "stop":
            incident.sticky_stop = decision
            incident.stop_banner_from = decision.from_ or "human"

        return {**self._decision_dict(decision, incident), **self._call_info(incident)}


STATE = EscalationState()
