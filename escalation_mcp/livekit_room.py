

from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional

import numpy as np
from livekit import api

from escalation_mcp.config import Settings
from escalation_mcp.voice.resample import resample_pcm16

logger = logging.getLogger("escalation_mcp")

DataHandler = Callable[[bytes, Optional[str]], Awaitable[None]]
UserAudioSink = Callable[[bytes], None]  # 16 kHz mono PCM16 chunks

SCREEN_IDENTITY = "agent"          # Chert's remote_participant_identity
BRIDGE_IDENTITY = "chert-bridge"   # identity we give Chert's bridge token

SR, CH = 48000, 1
SAMPLES = SR // 50                 # 20 ms frames
FRAME_BYTES = SAMPLES * 2 * CH     # int16
EL_SR = 16_000
# Chert/FaceTime often drops ~10s on pure silence and shows a gray placeholder.
# Default ON; set AGENT_IDLE_TONE=0 to force silence.
IDLE_TONE = os.getenv("AGENT_IDLE_TONE", "1") != "0"


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
# Very quiet keep-alive; louder values sound like "static" on FaceTime.
IDLE = _tone(220, SAMPLES / SR, amp=0.008)


class LiveKitRoomConnection:
    """Owns the agent's livekit.rtc.Room: audio track, data channel; screen backend adds video."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.room: Any | None = None
        self._handlers: list[DataHandler] = []
        self._connected = False
        self._audio_source: Any | None = None
        self._audio_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._pump_task: asyncio.Task[None] | None = None
        self._audio_stream_tasks: list[asyncio.Task[None]] = []
        self._user_audio_sink: UserAudioSink | None = None
        # Idle tone keeps FaceTime video alive between turns, but sounds like
        # static before the agent greets — leave off until first real speech.
        self._idle_enabled = False
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

    def set_user_audio_sink(self, sink: UserAudioSink | None) -> None:
        """Receive remote (phone) audio as 16 kHz mono PCM16 for ElevenLabs."""
        self._user_audio_sink = sink

    def clear_audio_queue(self) -> None:
        """Drop buffered outbound PCM (barge-in / interrupt)."""
        while True:
            try:
                self._audio_q.get_nowait()
            except asyncio.QueueEmpty:
                break

    def enable_idle_tone(self, enabled: bool = True) -> None:
        self._idle_enabled = enabled

    # ---- audio ----
    def play_pcm(self, pcm: bytes) -> None:
        """Queue 48 kHz mono int16 PCM (e.g. TTS output) to speak into the call."""
        for i in range(0, len(pcm), FRAME_BYTES):
            chunk = pcm[i:i + FRAME_BYTES]
            if len(chunk) < FRAME_BYTES:
                chunk += b"\x00" * (FRAME_BYTES - len(chunk))
            try:
                self._audio_q.put_nowait(chunk)
            except asyncio.QueueFull:
                break

    async def _pump_audio(self) -> None:
        from livekit import rtc

        while self._connected and self._audio_source is not None:
            try:
                chunk = self._audio_q.get_nowait()
            except asyncio.QueueEmpty:
                chunk = IDLE if (IDLE_TONE and self._idle_enabled) else SILENCE
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
                self.bridge_joined.set()
                # Skip chime when ElevenLabs Mentor will greet — chime + early
                # TTS overlaps the FaceTime gray window and sounds like static.
                if (os.getenv("ELEVENLABS_AGENT_ID") or "").strip():
                    logger.info("Chert bridge joined; waiting for Mentor voice")
                else:
                    logger.info("Chert bridge joined; playing chime to wake video")
                    self.play_pcm(CHIME)

        @self.room.on("track_subscribed")
        def _on_track_subscribed(track: Any, publication: Any, participant: Any) -> None:
            try:
                if track.kind != rtc.TrackKind.KIND_AUDIO:
                    return
            except Exception:  # noqa: BLE001
                return
            identity = getattr(participant, "identity", "")
            logger.info("subscribed remote audio from %s", identity)
            task = asyncio.create_task(
                self._consume_remote_audio(track),
                name=f"lk-remote-audio-{identity}",
            )
            self._audio_stream_tasks.append(task)

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

    async def _consume_remote_audio(self, track: Any) -> None:
        from livekit import rtc

        stream = rtc.AudioStream(track)
        try:
            async for event in stream:
                if not self._connected:
                    break
                sink = self._user_audio_sink
                if sink is None:
                    continue
                frame = event.frame
                try:
                    data = bytes(frame.data)
                    rate = int(frame.sample_rate)
                    channels = int(frame.num_channels)
                except Exception:  # noqa: BLE001
                    continue
                if channels > 1:
                    # downmix interleaved int16 → mono
                    samples = np.frombuffer(data, dtype=np.int16)
                    if samples.size >= channels:
                        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
                        data = samples.tobytes()
                pcm16k = resample_pcm16(data, rate, EL_SR) if rate != EL_SR else data
                try:
                    sink(pcm16k)
                except Exception:  # noqa: BLE001
                    logger.exception("user audio sink failed")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("remote audio stream ended with error")
        finally:
            for closer in ("aclose", "close"):
                fn = getattr(stream, closer, None)
                if fn is None:
                    continue
                try:
                    result = fn()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # noqa: BLE001
                    pass
                break

    async def publish_data(self, data: bytes, *, topic: str) -> None:
        if not self.room or not self._connected:
            logger.warning("publish_data skipped: not connected")
            return
        await self.room.local_participant.publish_data(data, reliable=True, topic=topic)

    async def disconnect(self) -> None:
        self._connected = False
        self._user_audio_sink = None
        for task in self._audio_stream_tasks:
            task.cancel()
        for task in self._audio_stream_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
        self._audio_stream_tasks.clear()
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
        self.clear_audio_queue()
