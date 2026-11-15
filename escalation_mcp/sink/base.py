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
