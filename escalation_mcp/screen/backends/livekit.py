from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from escalation_mcp.screen.backends.base import ScreenBackend

if TYPE_CHECKING:
    from escalation_mcp.livekit_room import LiveKitRoomConnection
    from escalation_mcp.screen.capture import CaptureLoop

logger = logging.getLogger("escalation_mcp")

W, H, FPS = 1280, 720, 15  # match screen_test.py; Chert is happier at ≥10–15 fps


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
        if self._capture.latest_rgba is None:
            self._capture.capture_once()
        if not self._room_conn.room or not self._room_conn.connected:
            raise RuntimeError("LiveKit room not connected")

        self._source = rtc.VideoSource(W, H)
        self._track = rtc.LocalVideoTrack.create_video_track("agent-video", self._source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_CAMERA
        options.video_codec = rtc.VideoCodec.H264
        options.simulcast = False
        self._publication = await self._room_conn.room.local_participant.publish_track(
            self._track, options
        )
        self._stop.clear()
        self._task = asyncio.create_task(self._push_frames(), name="lk-screen-push")
        logger.info("published agent camera track %sx%s (H.264)", W, H)

    async def _push_frames(self) -> None:
        import numpy as np
        from livekit import rtc

        while not self._stop.is_set():
            try:
                if self._capture and self._capture.latest_rgba is not None and self._source:
                    # Match screen_test.py: Chert/FaceTime expects BGRA + opaque alpha.
                    # RGBA here shows as gray/wrong colors on the phone even when
                    # the local MJPEG preview (JPEG) looks fine.
                    img = self._capture.latest_rgba.convert("RGBA").resize((W, H))
                    rgba = np.asarray(img, dtype=np.uint8)
                    bgra = np.ascontiguousarray(rgba[:, :, [2, 1, 0, 3]])
                    bgra[:, :, 3] = 255
                    self._source.capture_frame(
                        rtc.VideoFrame(W, H, rtc.VideoBufferType.BGRA, bgra.tobytes())
                    )
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
