

from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional

import numpy as np
from livekit import api

from escalation_mcp.config import Settings

logger = logging.getLogger("escalation_mcp")

DataHandler = Callable[[bytes, Optional[str]], Awaitable[None]]
PresenceHandler = Callable[[str], Awaitable[None]]

SCREEN_IDENTITY = "agent"          # Chert's remote_participant_identity
BRIDGE_IDENTITY = "chert-bridge"   # identity we give Chert's bridge token

SR, CH = 48000, 1
SAMPLES = SR // 50                 # 20 ms frames
FRAME_BYTES = SAMPLES * 2 * CH     # int16
IDLE_TONE = os.getenv("AGENT_IDLE_TONE", "0") == "1"  # fallback if chime isn't enough


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
    ttl_hours: int = 1,
) -> str:
    grant = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
        can_publish_data=can_publish_data,
    )
    return (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grant)
        .with_ttl(timedelta(hours=ttl_hours))  # Chert requires <= 2h
        .to_jwt()
    )


def _tone(freq: float, seconds: float, amp: float = 0.2) -> bytes:
    n = np.arange(int(SR * seconds))
    return (amp * 32767 * np.sin(2 * np.pi * freq * n / SR)).astype(np.int16).tobytes()


CHIME = _tone(660, 0.25) + _tone(880, 0.35)
SILENCE = b"\x00\x00" * SAMPLES * CH
IDLE = _tone(440, SAMPLES / SR, amp=0.03)


class LiveKitRoomConnection:
    """Owns the agent's livekit.rtc.Room: audio track, data channel; screen backend adds video."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.room: Any | None = None
        self._handlers: list[DataHandler] = []
        self._presence_handlers: list[PresenceHandler] = []
        self._connected = False
        self._audio_source: Any | None = None
        self._audio_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._pump_task: asyncio.Task[None] | None = None
        self.bridge_joined = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected

    def add_data_handler(self, handler: DataHandler) -> None:
        if handler not in self._handlers:
            self._handlers.append(handler)

    def remove_data_handler(self, handler: DataHandler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    def add_presence_handler(self, handler: PresenceHandler) -> None:
        if handler not in self._presence_handlers:
            self._presence_handlers.append(handler)

    def remove_presence_handler(self, handler: PresenceHandler) -> None:
        if handler in self._presence_handlers:
            self._presence_handlers.remove(handler)

    def _fire_presence(self, event: str) -> None:
        for handler in list(self._presence_handlers):
            try:
                asyncio.create_task(handler(event))
            except Exception:  # noqa: BLE001
                logger.exception("presence handler error")

    # ---- audio ----
    def play_pcm(self, pcm: bytes) -> None:
        """Queue 48 kHz mono int16 PCM (e.g. TTS output) to speak into the call."""
        for i in range(0, len(pcm), FRAME_BYTES):
            chunk = pcm[i:i + FRAME_BYTES]
            if len(chunk) < FRAME_BYTES:
                chunk += b"\x00" * (FRAME_BYTES - len(chunk))
            self._audio_q.put_nowait(chunk)

    async def _pump_audio(self) -> None:
        from livekit import rtc

        while self._connected and self._audio_source is not None:
            try:
                chunk = self._audio_q.get_nowait()
            except asyncio.QueueEmpty:
                chunk = IDLE if IDLE_TONE else SILENCE
            try:
                await self._audio_source.capture_frame(rtc.AudioFrame(chunk, SR, CH, SAMPLES))
            except Exception:  # noqa: BLE001
                logger.exception("audio frame push failed")
                await asyncio.sleep(0.02)

    # ---- connection ----
    async def connect(self, room: str, identity: str = SCREEN_IDENTITY) -> None:
        from livekit import rtc

        token = mint_token(self.settings, identity=identity, room=room)
        self.room = rtc.Room()
        self.bridge_joined.clear()

        @self.room.on("data_received")
        def _on_data(data_packet: Any) -> None:
            raw = getattr(data_packet, "data", b"") or b""
            topic = getattr(data_packet, "topic", None)
            for handler in list(self._handlers):
                try:
                    asyncio.create_task(handler(bytes(raw), topic))
                except Exception:  # noqa: BLE001
                    logger.exception("data handler error")

        @self.room.on("participant_connected")
        def _on_participant(p: Any) -> None:
            if getattr(p, "identity", "") == BRIDGE_IDENTITY:
                logger.info("Chert bridge joined; playing chime to wake video")
                self.bridge_joined.set()
                self.play_pcm(CHIME)  # later: greeting TTS instead
                self._fire_presence("joined")

        @self.room.on("participant_disconnected")
        def _on_participant_left(p: Any) -> None:
            if getattr(p, "identity", "") == BRIDGE_IDENTITY:
                logger.info("Chert bridge left the room (caller hung up?)")
                self.bridge_joined.clear()
                self._fire_presence("left")

        await self.room.connect(self.settings.livekit_url, token)
        self._connected = True

        # agent audio track (required by Chert's bridge)
        self._audio_source = rtc.AudioSource(SR, CH)
        track = rtc.LocalAudioTrack.create_audio_track("agent-audio", self._audio_source)
        opts = rtc.TrackPublishOptions()
        opts.source = rtc.TrackSource.SOURCE_MICROPHONE
        await self.room.local_participant.publish_track(track, opts)
        self._pump_task = asyncio.create_task(self._pump_audio(), name="lk-audio-pump")

        logger.info("connected to LiveKit room %s as %s (audio published)", room, identity)

    async def publish_data(self, data: bytes, *, topic: str) -> None:
        if not self.room or not self._connected:
            logger.warning("publish_data skipped: not connected")
            return
        await self.room.local_participant.publish_data(data, reliable=True, topic=topic)

    async def disconnect(self) -> None:
        self._connected = False
        if self._pump_task:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None
        if self.room is not None:
            try:
                await self.room.disconnect()
            except Exception:  # noqa: BLE001
                logger.exception("error disconnecting LiveKit room")
        self.room = None
        self._audio_source = None
        self._handlers.clear()
        self._presence_handlers.clear()
