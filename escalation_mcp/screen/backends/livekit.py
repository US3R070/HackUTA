"""Publish screenshare frames into a LiveKit room."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from escalation_mcp.screen.backends.base import ScreenBackend

if TYPE_CHECKING:
    from escalation_mcp.livekit_room import LiveKitRoomConnection
    from escalation_mcp.screen.capture import CaptureLoop

logger = logging.getLogger("escalation_mcp")


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

    async def start(self, frame_source: "CaptureLoop") -> None:
        from livekit import rtc

        self._capture = frame_source
        # Ensure we have at least one frame for dimensions
        if self._capture.latest_rgba is None:
            self._capture.capture_once()
        assert self._capture.latest_rgba is not None
        w, h = self._capture.latest_rgba.size

        if not self._room_conn.room or not self._room_conn.connected:
            raise RuntimeError("LiveKit room not connected")

        self._source = rtc.VideoSource(w, h)
        self._track = rtc.LocalVideoTrack.create_video_track(
            "screenshare", self._source
        )
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_SCREENSHARE
        self._publication = await self._room_conn.room.local_participant.publish_track(
            self._track, options
        )
        self._stop.clear()
        self._task = asyncio.create_task(self._push_frames(), name="lk-screen-push")
        logger.info("published LiveKit screenshare track %sx%s", w, h)

    async def _push_frames(self) -> None:
        from livekit import rtc

        while not self._stop.is_set():
            try:
                if self._capture and self._capture.latest_rgba is not None and self._source:
                    img = self._capture.latest_rgba.convert("RGBA")
                    w, h = img.size
                    argb = bytearray(img.tobytes())
                    # Pillow RGBA -> LiveKit expects RGBA in VideoFrame
                    frame = rtc.VideoFrame(w, h, rtc.VideoBufferType.RGBA, argb)
                    self._source.capture_frame(frame)
            except Exception:  # noqa: BLE001
                logger.exception("LiveKit frame push failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.2)
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
                await self._room_conn.room.local_participant.unpublish_track(
                    self._publication.sid
                )
            except Exception:  # noqa: BLE001
                logger.exception("unpublish screenshare failed")
        self._publication = None
        self._track = None
        self._source = None
        self._capture = None
