"""Localhost MJPEG preview server (dev + LiveKit fallback)."""

from __future__ import annotations

import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from escalation_mcp.screen.backends.base import ScreenBackend

if TYPE_CHECKING:
    from escalation_mcp.screen.capture import CaptureLoop

logger = logging.getLogger("escalation_mcp")


class PreviewBackend(ScreenBackend):
    name = "preview"

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self.preview_url = f"http://{host}:{port}"
        self._capture: CaptureLoop | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    async def start(self, frame_source: "CaptureLoop") -> None:
        self._capture = frame_source
        capture_ref = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
                logger.debug("preview: " + fmt, *args)

            def do_GET(self) -> None:  # noqa: N802
                if self.path in ("/", "/stream.mjpg", "/stream"):
                    self.send_response(200)
                    self.send_header(
                        "Content-Type", "multipart/x-mixed-replace; boundary=frame"
                    )
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    try:
                        while True:
                            jpeg = (
                                capture_ref._capture.latest_jpeg
                                if capture_ref._capture
                                else None
                            )
                            if jpeg:
                                self.wfile.write(b"--frame\r\n")
                                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                                self.wfile.write(
                                    f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                                )
                                self.wfile.write(jpeg)
                                self.wfile.write(b"\r\n")
                            # Blocking sleep in handler thread is fine
                            import time

                            time.sleep(0.2)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                if self.path.startswith("/frame.jpg"):
                    jpeg = (
                        capture_ref._capture.latest_jpeg
                        if capture_ref._capture
                        else None
                    )
                    if not jpeg:
                        self.send_response(503)
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(jpeg)))
                    self.end_headers()
                    self.wfile.write(jpeg)
                    return
                self.send_response(404)
                self.end_headers()

        # Bind; if port busy, try a few next ports
        bound = False
        last_err: Exception | None = None
        for offset in range(0, 10):
            port = self.port + offset
            try:
                self._httpd = ThreadingHTTPServer((self.host, port), Handler)
                self.port = port
                self.preview_url = f"http://{self.host}:{port}"
                bound = True
                break
            except OSError as exc:
                last_err = exc
        if not bound or self._httpd is None:
            raise RuntimeError(f"could not bind preview port: {last_err}")

        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="mjpeg-preview", daemon=True
        )
        self._thread.start()
        logger.info("preview server at %s", self.preview_url)

    async def stop(self) -> None:
        if self._httpd is not None:
            await asyncio.get_event_loop().run_in_executor(
                None, self._httpd.shutdown
            )
            self._httpd.server_close()
            self._httpd = None
        self._thread = None
        self._capture = None
