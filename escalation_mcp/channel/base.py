"""Channel interface for agent events and human decisions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

from escalation_mcp.models import ActionEvent, HumanDecision

DecisionHandler = Callable[[HumanDecision], Awaitable[None]]


class Channel(ABC):
    name: str = "base"

    @abstractmethod
    async def start(self, incident_id: str, on_decision: DecisionHandler) -> None:
        ...

    @abstractmethod
    async def publish_action(self, event: ActionEvent) -> None:
        ...

    @abstractmethod
    async def publish_raw(self, topic: str, payload: dict[str, Any]) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...
