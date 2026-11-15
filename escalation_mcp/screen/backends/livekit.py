from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from escalation_mcp.screen.backends.base import ScreenBackend

if TYPE_CHECKING:
    from escalation_mcp.livekit_room import LiveKitRoomConnection
    from escalation_mcp.screen.capture import CaptureLoop

logger = logging.getLogger("escalation_mcp")

W, H, FPS = 1280, 720, 20  # higher fps → keyframes more often (less gray on join)


class LiveKitScreenBackend(ScreenBackend):
    name = "livekit"

    def __init__(self, room_conn: "LiveKitRoomConnection") -> None:
        self._room_conn = room_conn
        self._capture: CaptureLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._source: Any | None = None
        self._track: Any | None = None
        self._publication: Any | None = None
        room_conn.add_bridge_joined_hook(self._on_subscriber_likely)
        room_conn.add_call_answered_hook(self._on_subscriber_likely)

    def _on_subscriber_likely(self) -> None:
        # FaceTime often stays gray until the next H.264 keyframe. Mute/unmute
        # nudges the encoder when Chert / the phone starts watching.
        asyncio.create_task(self.refresh_for_subscriber(), name="lk-video-refresh")

    async def refresh_for_subscriber(self) -> None:
        track = self._track
        if track is None:
            return
        try:
            logger.info("refreshing video track for new subscriber (keyframe nudge)")
            track.mute()
            await asyncio.sleep(0.08)
            track.unmute()
            # Push a burst of frames immediately after unmute.
            for _ in range(8):
                await self._push_one_frame()
                await asyncio.sleep(0.03)
        except Exception:  # noqa: BLE001
            logger.exception("video refresh for subscriber failed")

    async def start(self, frame_source: "CaptureLoop") -> None:
        from livekit import rtc

        self._capture = frame_source
        if self._capture.latest_rgba is None:
            self._capture.capture_once()
        if not self._room_conn.room or not self._room_conn.connected:
            raise RuntimeError("LiveKit room not connected")

        self._source = rtc.VideoSource(W, H)
        self._track = rtc.LocalVideoTrack.create_video_track("agent-video", self._source)
        options = rtc.TrackPublishOptions()
        # Chert's bridge has historically ignored screenshare-source tracks
        # (black video); camera source is the known-good default. Override with
        # AGENT_VIDEO_SOURCE=screenshare to A/B test.
        source_name = (os.getenv("AGENT_VIDEO_SOURCE") or "camera").strip().lower()
        options.source = (
            rtc.TrackSource.SOURCE_SCREENSHARE
            if source_name == "screenshare"
            else rtc.TrackSource.SOURCE_CAMERA
        )
        options.video_codec = rtc.VideoCodec.H264
        options.simulcast = False
        self._publication = await self._room_conn.room.local_participant.publish_track(
            self._track, options
        )
        self._stop.clear()
        self._task = asyncio.create_task(self._push_frames(), name="lk-screen-push")
        logger.info(
            "published agent video track %sx%s (H.264, source=%s)", W, H, source_name
        )

    async def _push_one_frame(self) -> None:
        import numpy as np
        from livekit import rtc

        if not self._capture or self._capture.latest_rgba is None or not self._source:
            return
        img = self._capture.latest_rgba.convert("RGBA").resize((W, H))
        rgba = np.asarray(img, dtype=np.uint8)
        bgra = np.ascontiguousarray(rgba[:, :, [2, 1, 0, 3]])
        bgra[:, :, 3] = 255
        self._source.capture_frame(
            rtc.VideoFrame(W, H, rtc.VideoBufferType.BGRA, bgra.tobytes())
        )

    async def _push_frames(self) -> None:
        while not self._stop.is_set():
            try:
                await self._push_one_frame()
            except Exception:  # noqa: BLE001
                logger.exception("LiveKit frame push failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1 / FPS)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._publication and self._room_conn.room:
            try:
                await self._room_conn.room.local_participant.unpublish_track(self._publication.sid)
            except Exception:  # noqa: BLE001
                logger.exception("unpublish agent video failed")
        self._publication = None
        self._track = None
        self._source = None
        self._capture = None
