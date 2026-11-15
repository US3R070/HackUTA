"""LiveKit room naming, token minting, and a shared Room connection."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional

from livekit import api

from escalation_mcp.config import Settings

logger = logging.getLogger("escalation_mcp")

DataHandler = Callable[[bytes, Optional[str]], Awaitable[None]]

SCREEN_IDENTITY = "local-screen"


def room_name_for(incident_id: str) -> str:
    return f"esc-{incident_id}"


def mint_token(
    settings: Settings,
    *,
    identity: str,
    room: str,
    can_publish: bool = True,
    can_subscribe: bool = True,
    can_publish_data: bool = True,
    ttl_hours: int = 6,
) -> str:
    grant = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
        can_publish_data=can_publish_data,
    )
    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grant)
        .with_ttl(timedelta(hours=ttl_hours))
    )
    return token.to_jwt()


class LiveKitRoomConnection:
    """Owns a single livekit.rtc.Room for screen + data channel."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.room: Any | None = None
        self._handlers: list[DataHandler] = []
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def add_data_handler(self, handler: DataHandler) -> None:
        if handler not in self._handlers:
            self._handlers.append(handler)

    def remove_data_handler(self, handler: DataHandler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    async def connect(self, room: str, identity: str = SCREEN_IDENTITY) -> None:
        from livekit import rtc

        token = mint_token(
            self.settings,
            identity=identity,
            room=room,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
        )
        self.room = rtc.Room()

        @self.room.on("data_received")
        def _on_data(data_packet: Any) -> None:
            raw = getattr(data_packet, "data", b"") or b""
            topic = getattr(data_packet, "topic", None)
            for handler in list(self._handlers):
                try:
                    # Schedule on the room's event loop
                    import asyncio

                    asyncio.create_task(handler(bytes(raw), topic))
                except Exception:  # noqa: BLE001
                    logger.exception("data handler error")

        await self.room.connect(self.settings.livekit_url, token)
        self._connected = True
        logger.info("connected to LiveKit room %s as %s", room, identity)

    async def publish_data(self, data: bytes, *, topic: str) -> None:
        if not self.room or not self._connected:
            logger.warning("publish_data skipped: not connected")
            return
        from livekit import rtc

        await self.room.local_participant.publish_data(
            data,
            reliable=True,
            topic=topic,
        )

    async def disconnect(self) -> None:
        if self.room is not None:
            try:
                await self.room.disconnect()
            except Exception:  # noqa: BLE001
                logger.exception("error disconnecting LiveKit room")
        self.room = None
        self._connected = False
        self._handlers.clear()
