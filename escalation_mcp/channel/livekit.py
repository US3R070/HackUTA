"""LiveKit data-channel transport for agent_events / human_decisions."""

from __future__ import annotations

import json
import logging
from typing import Any

from escalation_mcp.channel.base import Channel, DecisionHandler, PresenceHandler
from escalation_mcp.models import ActionEvent, HumanDecision
from escalation_mcp.packet import redact_value

logger = logging.getLogger("escalation_mcp")

EVENTS_TOPIC = "agent_events"
DECISIONS_TOPIC = "human_decisions"


class LiveKitChannel(Channel):
    name = "livekit"

    def __init__(self, room_conn: Any) -> None:
        """room_conn is an escalation_mcp.livekit_room.LiveKitRoomConnection."""
        self._room_conn = room_conn
        self._incident_id: str | None = None
        self._on_decision: DecisionHandler | None = None
        self._on_presence: PresenceHandler | None = None

    async def start(
        self,
        incident_id: str,
        on_decision: DecisionHandler,
        on_presence: PresenceHandler | None = None,
    ) -> None:
        self._incident_id = incident_id
        self._on_decision = on_decision
        self._on_presence = on_presence
        self._room_conn.add_data_handler(self._on_data)
        if on_presence is not None:
            self._room_conn.add_presence_handler(on_presence)
            if self._room_conn.bridge_joined.is_set():
                await on_presence("joined")  # bridge was faster than us

    async def _on_data(self, data: bytes, topic: str | None) -> None:
        if topic and topic != DECISIONS_TOPIC:
            return
        if not self._on_decision or not self._incident_id:
            return
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:  # noqa: BLE001
            logger.warning("invalid data-channel JSON")
            return
        if payload.get("type") != "decision":
            logger.info("ignoring data message type=%s", payload.get("type"))
            return
        if payload.get("incident_id") != self._incident_id:
            logger.warning("decision incident_id mismatch")
            return
        try:
            decision = HumanDecision.model_validate(payload)
        except Exception:  # noqa: BLE001
            logger.exception("invalid decision payload")
            return
        await self._on_decision(decision)

    async def publish_action(self, event: ActionEvent) -> None:
        await self.publish_raw(EVENTS_TOPIC, event.model_dump(by_alias=True))

    async def publish_raw(self, topic: str, payload: dict[str, Any]) -> None:
        safe = redact_value(payload)
        assert isinstance(safe, dict)
        body = json.dumps(safe, ensure_ascii=False).encode("utf-8")
        await self._room_conn.publish_data(body, topic=topic)

    async def stop(self) -> None:
        self._room_conn.remove_data_handler(self._on_data)
        if self._on_presence is not None:
            self._room_conn.remove_presence_handler(self._on_presence)
        self._on_decision = None
        self._on_presence = None
