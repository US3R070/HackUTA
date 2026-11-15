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


class ChertSink(Sink):
    name = "chert"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.call_ids: dict[str, str] = {}

    async def send_packet(self, packet: EscalationPacket) -> list[str]:
        save_packet(packet)
        if packet.severity != "HIGH":
            return []  # LOW: iMessage only (later)
        try:
            await asyncio.sleep(1.0)  # let agent audio+video start before Chert joins
            token = mint_token(self.settings, identity=BRIDGE_IDENTITY, room=packet.media.room)
            call = await asyncio.to_thread(
                place_call, os.environ["USER_PHONE"], token, SCREEN_IDENTITY, True
            )
            call_id = call.get("id") if isinstance(call, dict) else None
            if not call_id:
                raise RuntimeError(f"Chert place_call returned no id: {call!r}")
            self.call_ids[packet.incident_id] = call_id
            logger.info("chert call %s placed for %s", call_id, packet.incident_id)
            return []
        except Exception as exc:  # noqa: BLE001
            msg = f"chert call failed: {exc}"
            logger.warning(msg)
            return [msg]

    async def send_report(self, incident_id: str, report: FinalReport) -> list[str]:
        call_id = self.call_ids.pop(incident_id, None)
        if call_id:
            try:
                from chert.client import end_call
                await asyncio.to_thread(end_call, call_id)
                logger.info("chert call %s ended", call_id)
            except Exception as exc:  # noqa: BLE001
                return [f"end call failed: {exc}"]
        return []

    async def send_event(self, incident_id: str, event: dict[str, Any]) -> list[str]:
        return []