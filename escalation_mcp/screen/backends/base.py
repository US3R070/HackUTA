"""Common screen backend interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from escalation_mcp.screen.capture import CaptureLoop


class ScreenBackend(ABC):
    name: str = "base"

    @abstractmethod
    async def start(self, frame_source: "CaptureLoop") -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...
