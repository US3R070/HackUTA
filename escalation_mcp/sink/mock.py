"""Mock sink: packets/reports already saved under runs/<id>/; no network."""

from __future__ import annotations

import json
import logging

from escalation_mcp.models import EscalationPacket, FinalReport
from escalation_mcp.packet import incident_dir
from escalation_mcp.sink.base import Sink

logger = logging.getLogger("escalation_mcp")


class MockSink(Sink):
    name = "mock"

    async def send_packet(self, packet: EscalationPacket) -> list[str]:
        # packet.json is already written by packet.save_packet
        path = incident_dir(packet.incident_id) / "sink_ack.json"
        path.write_text(
            json.dumps({"ok": True, "sink": "mock", "incident_id": packet.incident_id}),
            encoding="utf-8",
        )
        logger.info("mock sink accepted packet %s", packet.incident_id)
        return []

    async def send_report(self, incident_id: str, report: FinalReport) -> list[str]:
        path = incident_dir(incident_id) / "final_report.json"
        path.write_text(
            json.dumps(report.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("mock sink saved final report for %s", incident_id)
        return []
