"""Mock channel: outbox.jsonl for events, inbox.jsonl for decisions."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from escalation_mcp.channel.base import Channel, DecisionHandler, PresenceHandler
from escalation_mcp.models import ActionEvent, HumanDecision
from escalation_mcp.packet import incident_dir, redact_value

logger = logging.getLogger("escalation_mcp")


class MockChannel(Channel):
    name = "mock"

    def __init__(self) -> None:
        self._incident_id: str | None = None
        self._on_decision: DecisionHandler | None = None
        self._on_presence: PresenceHandler | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._inbox_offset = 0

    def _paths(self) -> tuple[Path, Path]:
        assert self._incident_id
        d = incident_dir(self._incident_id)
        outbox = d / "outbox.jsonl"
        inbox = d / "inbox.jsonl"
        outbox.touch(exist_ok=True)
        inbox.touch(exist_ok=True)
        return outbox, inbox

    async def start(
        self,
        incident_id: str,
        on_decision: DecisionHandler,
        on_presence: PresenceHandler | None = None,
    ) -> None:
        self._incident_id = incident_id
        self._on_decision = on_decision
        self._on_presence = on_presence
        self._stop.clear()
        _, inbox = self._paths()
        # Start from end so pre-existing lines are ignored unless newly appended
        self._inbox_offset = inbox.stat().st_size if inbox.exists() else 0
        self._task = asyncio.create_task(self._tail_inbox(), name="mock-inbox-tail")

    async def _tail_inbox(self) -> None:
        assert self._incident_id and self._on_decision
        while not self._stop.is_set():
            try:
                _, inbox = self._paths()
                size = inbox.stat().st_size
                if size > self._inbox_offset:
                    with inbox.open("r", encoding="utf-8") as fh:
                        fh.seek(self._inbox_offset)
                        chunk = fh.read()
                        self._inbox_offset = fh.tell()
                    for line in chunk.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        await self._handle_line(line)
                await asyncio.sleep(0.2)
            except Exception:  # noqa: BLE001
                logger.exception("mock inbox tail error")
                await asyncio.sleep(0.5)

    async def _handle_line(self, line: str) -> None:
        assert self._on_decision and self._incident_id
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("invalid inbox JSON: %s", line[:120])
            return
        msg_type = data.get("type")
        if msg_type == "presence":
            if data.get("incident_id") != self._incident_id:
                logger.warning("inbox presence incident_id mismatch")
                return
            event = data.get("event")
            if event in ("joined", "left") and self._on_presence is not None:
                await self._on_presence(event)
            return
        if msg_type != "decision":
            logger.info("ignoring inbox message type=%s", msg_type)
            return
        if data.get("incident_id") != self._incident_id:
            logger.warning("inbox decision incident_id mismatch")
            return
        try:
            decision = HumanDecision.model_validate(data)
        except Exception:  # noqa: BLE001
            logger.exception("invalid decision payload")
            return
        await self._on_decision(decision)

    async def publish_action(self, event: ActionEvent) -> None:
        await self.publish_raw(
            "agent_events", event.model_dump(by_alias=True)
        )

    async def publish_raw(self, topic: str, payload: dict[str, Any]) -> None:
        if not self._incident_id:
            return
        outbox, _ = self._paths()
        safe = redact_value(payload)
        record = {"topic": topic, **(safe if isinstance(safe, dict) else {"payload": safe})}
        with outbox.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
