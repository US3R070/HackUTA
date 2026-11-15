"""Sink interface for handing off escalation packets to the cloud agent."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from escalation_mcp.models import EscalationPacket, FinalReport


class Sink(ABC):
    name: str = "base"

    @abstractmethod
    async def send_packet(self, packet: EscalationPacket) -> list[str]:
        """Send the packet. Return warning strings (empty on success)."""

    @abstractmethod
    async def send_report(self, incident_id: str, report: FinalReport) -> list[str]:
        """Send the final report. Return warning strings."""

    async def send_event(self, incident_id: str, event: dict[str, Any]) -> list[str]:
        """Optional HTTP event mirror. Default: no-op."""
        return []

    def places_call(self, packet: EscalationPacket) -> bool:
        """Whether sending this packet results in a phone call we should track."""
        return True

    async def redial(self, packet: EscalationPacket, attempt: int) -> list[str]:
        """Nobody answered: place the call again. Default: no-op."""
        return []

    async def end_call(self, incident_id: str) -> list[str]:
        """Hang up the phone side of the call. Default: no-op."""
        return []
