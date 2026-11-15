from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from chert.client import place_call
from escalation_mcp.config import Settings
from escalation_mcp.livekit_room import BRIDGE_IDENTITY, SCREEN_IDENTITY, mint_token
from escalation_mcp.models import EscalationPacket, FinalReport
from escalation_mcp.packet import save_packet
from escalation_mcp.sink.base import Sink

logger = logging.getLogger("escalation_mcp")

_TERMINAL_STATUSES = {
    "ended", "completed", "failed", "canceled", "cancelled",
    "no_answer", "busy", "rejected", "declined", "missed", "expired",
}


def _is_terminal(call: dict[str, Any]) -> bool:
    """Chert call is over (unanswered/failed/ended). Unknown -> treat as active."""
    if call.get("terminal_reason"):
        return True
    return str(call.get("status", "")).lower() in _TERMINAL_STATUSES


class ChertSink(Sink):
    name = "chert"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.call_ids: dict[str, str] = {}

    def places_call(self, packet: EscalationPacket) -> bool:
        return packet.severity == "HIGH"  # LOW: iMessage only (later)

    async def _place(self, packet: EscalationPacket) -> list[str]:
        try:
            token = mint_token(self.settings, identity=BRIDGE_IDENTITY, room=packet.media.room)
            call = await asyncio.to_thread(
                place_call, os.environ["USER_PHONE"], token, SCREEN_IDENTITY, True
            )
            self.call_ids[packet.incident_id] = call["id"]
            logger.info("chert call %s placed for %s", call["id"], packet.incident_id)
            return []
        except Exception as exc:  # noqa: BLE001
            msg = f"chert call failed: {exc}"
            logger.warning(msg)
            return [msg]

    async def send_packet(self, packet: EscalationPacket) -> list[str]:
        save_packet(packet)
        if not self.places_call(packet):
            return []
        await asyncio.sleep(1.0)  # let agent audio+video start before Chert joins
        return await self._place(packet)

    async def redial(self, packet: EscalationPacket, attempt: int) -> list[str]:
        # Never redial over a call Chert still considers active (ringing or connected):
        # that would hang up a human who may be on the line.
        call_id = self.call_ids.get(packet.incident_id)
        if call_id:
            status = await self._call_status(call_id)
            if status is None or not _is_terminal(status):
                state = status.get("status") if status else "unknown"
                msg = f"not redialing: chert call {call_id} still {state}"
                logger.info(msg)
                return [msg]
            self.call_ids.pop(packet.incident_id, None)
        logger.info("chert redial attempt %d for %s", attempt, packet.incident_id)
        return await self._place(packet)

    async def _call_status(self, call_id: str) -> dict[str, Any] | None:
        try:
            from chert.client import _get

            return await asyncio.to_thread(_get, f"/calls/{call_id}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("chert status check failed for %s: %s", call_id, exc)
            return None

    async def end_call(self, incident_id: str) -> list[str]:
        call_id = self.call_ids.pop(incident_id, None)
        if not call_id:
            return []
        try:
            from chert.client import end_call

            await asyncio.to_thread(end_call, call_id)
            logger.info("chert call %s ended", call_id)
            return []
        except Exception as exc:  # noqa: BLE001
            return [f"end call failed: {exc}"]

    async def send_report(self, incident_id: str, report: FinalReport) -> list[str]:
        return await self.end_call(incident_id)

    async def send_event(self, incident_id: str, event: dict[str, Any]) -> list[str]:
        return []
