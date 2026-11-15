"""HTTP sink: POST packet/report/events to the cloud Chert agent."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from escalation_mcp.config import Settings
from escalation_mcp.models import EscalationPacket, FinalReport
from escalation_mcp.packet import save_packet
from escalation_mcp.sink.base import Sink

logger = logging.getLogger("escalation_mcp")


class HttpSink(Sink):
    name = "http"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.escalation_shared_secret}",
            "Content-Type": "application/json",
        }

    async def _post_with_retries(
        self, url: str, payload: dict[str, Any], *, retries: int = 2
    ) -> list[str]:
        warnings: list[str] = []
        delay = 0.5
        last_err = ""
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        url, json=payload, headers=self._headers()
                    )
                    if resp.status_code < 400:
                        return []
                    last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
            if attempt < retries:
                await asyncio.sleep(delay)
                delay *= 2
        warnings.append(f"sink POST {url} failed: {last_err}")
        logger.warning(warnings[-1])
        return warnings

    async def send_packet(self, packet: EscalationPacket) -> list[str]:
        save_packet(packet)  # always keep a local copy for resend
        url = f"{self.settings.chert_agent_url}/escalations"
        return await self._post_with_retries(
            url, packet.model_dump(by_alias=True)
        )

    async def send_report(self, incident_id: str, report: FinalReport) -> list[str]:
        url = f"{self.settings.chert_agent_url}/escalations/{incident_id}/report"
        return await self._post_with_retries(url, report.model_dump())

    async def send_event(self, incident_id: str, event: dict[str, Any]) -> list[str]:
        if not self.settings.escalation_http_events:
            return []
        url = f"{self.settings.chert_agent_url}/escalations/{incident_id}/events"
        return await self._post_with_retries(url, event, retries=1)

    async def redial(self, packet: EscalationPacket, attempt: int) -> list[str]:
        url = f"{self.settings.chert_agent_url}/escalations/{packet.incident_id}/redial"
        return await self._post_with_retries(url, {"attempt": attempt}, retries=1)

    async def end_call(self, incident_id: str) -> list[str]:
        url = f"{self.settings.chert_agent_url}/escalations/{incident_id}/hangup"
        return await self._post_with_retries(url, {}, retries=1)
