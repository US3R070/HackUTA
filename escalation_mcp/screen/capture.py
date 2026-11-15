"""Screen / window / image capture helpers."""

from __future__ import annotations

import logging
import platform
import sys
import time
from pathlib import Path
from typing import Callable, Protocol

from PIL import Image

logger = logging.getLogger("escalation_mcp")


class FrameSource(Protocol):
    def grab(self) -> Image.Image:
        ...


def _even(n: int) -> int:
    return n if n % 2 == 0 else n - 1


def downscale(img: Image.Image, max_width: int) -> Image.Image:
    w, h = img.size
    if w <= max_width:
        ew, eh = _even(w), _even(h)
        if (ew, eh) != (w, h):
            return img.crop((0, 0, ew, eh))
        return img
    ratio = max_width / float(w)
    nw = _even(max_width)
    nh = _even(max(2, int(h * ratio)))
    return img.resize((nw, nh), Image.Resampling.BILINEAR)


def letterbox(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    tw, th = size
    canvas = Image.new("RGB", (tw, th), (0, 0, 0))
    img = img.convert("RGB")
    iw, ih = img.size
    scale = min(tw / iw, th / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    resized = img.resize((nw, nh), Image.Resampling.BILINEAR)
    ox, oy = (tw - nw) // 2, (th - nh) // 2
    canvas.paste(resized, (ox, oy))
    return canvas


class SolidFrameSource:
    """Deterministic frame source for tests (no display)."""

    def __init__(self, width: int = 640, height: int = 360, color=(40, 40, 50)) -> None:
        self._img = Image.new("RGB", (width, height), color)

    def grab(self) -> Image.Image:
        return self._img.copy()


class ScreenFrameSource:
    def __init__(self, monitor_index: int = 1, max_width: int = 1280) -> None:
        self.monitor_index = monitor_index
        self.max_width = max_width
        import mss

        self._mss = mss.mss()

    def grab(self) -> Image.Image:
        monitors = self._mss.monitors
        idx = min(max(self.monitor_index, 1), len(monitors) - 1)
        shot = self._mss.grab(monitors[idx])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return downscale(img, self.max_width)


class WindowFrameSource:
    def __init__(
        self,
        title_substr: str,
        max_width: int = 1280,
        fallback: FrameSource | None = None,
    ) -> None:
        self.title_substr = title_substr
        self.max_width = max_width
        self.fallback = fallback or ScreenFrameSource(max_width=max_width)
        self.warning: str | None = None

    def _find_rect_windows(self) -> tuple[int, int, int, int] | None:
        try:
            import pygetwindow as gw  # type: ignore
        except ImportError:
            self.warning = "pygetwindow not installed; falling back to full screen"
            return None
        matches = [
            w
            for w in gw.getAllWindows()
            if self.title_substr.lower() in (w.title or "").lower()
            and w.width > 0
            and w.height > 0
        ]
        if not matches:
            self.warning = f"window matching {self.title_substr!r} not found"
            return None
        w = matches[0]
        return int(w.left), int(w.top), int(w.width), int(w.height)

    def grab(self) -> Image.Image:
        # Window bounds: pygetwindow on Windows; macOS/Linux fall back to full screen
        # (Quartz/X11 helpers are optional — full-screen share is the reliable demo path).
        if sys.platform != "win32":
            if platform.system() == "Darwin":
                self.warning = "window capture on macOS falls back to full screen"
            else:
                self.warning = "window capture on this OS falls back to full screen"
            return self.fallback.grab()

        rect = self._find_rect_windows()
        if rect is None:
            return self.fallback.grab()

        left, top, width, height = rect
        import mss

        with mss.mss() as sct:
            monitor = {"left": left, "top": top, "width": width, "height": height}
            try:
                shot = sct.grab(monitor)
            except Exception:  # noqa: BLE001
                self.warning = "window grab failed; falling back to full screen"
                return self.fallback.grab()
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return downscale(img, self.max_width)


class ImageFrameSource:
    def __init__(self, path: str, max_width: int = 1280, size: tuple[int, int] | None = None) -> None:
        self.path = Path(path)
        self.max_width = max_width
        self.size = size or (max_width, _even(int(max_width * 9 / 16)))
        self._mtime: float | None = None
        self._cached: Image.Image | None = None

    def grab(self) -> Image.Image:
        if not self.path.exists():
            return Image.new("RGB", self.size, (20, 20, 20))
        mtime = self.path.stat().st_mtime
        if self._cached is None or self._mtime != mtime:
            img = Image.open(self.path).convert("RGB")
            self._cached = letterbox(downscale(img, self.max_width), self.size)
            self._mtime = mtime
        return self._cached.copy()


def make_frame_source(
    view: str,
    *,
    monitor: int = 1,
    max_width: int = 1280,
    fake: FrameSource | None = None,
) -> tuple[FrameSource, list[str]]:
    """Build a frame source from a share/view string. Returns (source, warnings)."""
    warnings: list[str] = []
    if fake is not None and view.startswith("fake"):
        return fake, warnings
    if fake is not None and view == "none":
        return fake, warnings

    if view == "screen" or view == "none":
        if fake is not None:
            return fake, warnings
        return ScreenFrameSource(monitor_index=monitor, max_width=max_width), warnings

    if view.startswith("window:"):
        title = view[len("window:") :]
        if fake is not None:
            return fake, warnings
        src = WindowFrameSource(title, max_width=max_width)
        # probe once
        _ = src.grab()
        if src.warning:
            warnings.append(src.warning)
        return src, warnings

    if view.startswith("image:"):
        path = view[len("image:") :]
        src = ImageFrameSource(path, max_width=max_width)
        return src, warnings

    warnings.append(f"unknown view {view!r}; using screen")
    if fake is not None:
        return fake, warnings
    return ScreenFrameSource(monitor_index=monitor, max_width=max_width), warnings


class CaptureLoop:
    """Periodically grab frames, apply overlay, push to backends."""

    def __init__(
        self,
        frame_source: FrameSource,
        overlay_fn: Callable[[Image.Image], Image.Image],
        fps: float = 5.0,
    ) -> None:
        self.frame_source = frame_source
        self.overlay_fn = overlay_fn
        self.fps = max(0.5, fps)
        self._latest_jpeg: bytes | None = None
        self._latest_rgba: Image.Image | None = None
        self._running = False

    @property
    def latest_jpeg(self) -> bytes | None:
        return self._latest_jpeg

    @property
    def latest_rgba(self) -> Image.Image | None:
        return self._latest_rgba

    def capture_once(self) -> Image.Image:
        raw = self.frame_source.grab().convert("RGB")
        framed = self.overlay_fn(raw)
        self._latest_rgba = framed
        from io import BytesIO

        buf = BytesIO()
        framed.save(buf, format="JPEG", quality=75)
        self._latest_jpeg = buf.getvalue()
        return framed

    async def run(self, stop_event) -> None:  # type: ignore[no-untyped-def]
        """Async wrapper; real capture runs in a thread so MCP tool calls cannot freeze video."""
        import asyncio

        await asyncio.to_thread(self.run_blocking, stop_event)

    def run_blocking(self, stop_event) -> None:  # type: ignore[no-untyped-def]
        self._running = True
        interval = 1.0 / self.fps
        while not stop_event.is_set():
            t0 = time.monotonic()
            try:
                self.capture_once()
            except Exception:  # noqa: BLE001
                logger.exception("capture frame failed")
            elapsed = time.monotonic() - t0
            # Event may be asyncio.Event — polling via is_set is enough; sleep in thread
            time.sleep(max(0.01, interval - elapsed))
        self._running = False
