"""Localhost MJPEG preview server (dev + LiveKit fallback).

Always binds the configured port (default 8765). Callers must tear down the
previous preview before starting a new one — we do not hop to 8766+.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from escalation_mcp.screen.backends.base import ScreenBackend

if TYPE_CHECKING:
    from escalation_mcp.screen.capture import CaptureLoop

logger = logging.getLogger("escalation_mcp")


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


async def wait_for_port_free(
    host: str, port: int, *, timeout: float = 5.0, interval: float = 0.1
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_in_use(host, port):
            return True
        await asyncio.sleep(interval)
    return not port_in_use(host, port)


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
                            import time as _time

                            _time.sleep(0.2)
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
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(jpeg)
                    return
                self.send_response(404)
                self.end_headers()

        class _PreviewServer(ThreadingHTTPServer):
            allow_reuse_address = True

        last_err: Exception | None = None
        for attempt in range(15):
            try:
                self._httpd = _PreviewServer((self.host, self.port), Handler)
                break
            except OSError as exc:
                last_err = exc
                await asyncio.sleep(0.15)
        else:
            raise RuntimeError(
                f"could not bind preview to {self.host}:{self.port} "
                f"(locked port; stop the previous preview first): {last_err}"
            )

        self.preview_url = f"http://{self.host}:{self.port}"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="mjpeg-preview", daemon=True
        )
        self._thread.start()
        logger.info("preview server at %s", self.preview_url)

    async def stop(self) -> None:
        if self._httpd is not None:
            httpd = self._httpd
            self._httpd = None

            def _shutdown() -> None:
                try:
                    httpd.shutdown()
                finally:
                    try:
                        httpd.server_close()
                    except Exception:  # noqa: BLE001
                        pass

            await asyncio.get_event_loop().run_in_executor(None, _shutdown)
            # Give Windows a moment to release TIME_WAIT listeners we own
            await wait_for_port_free(self.host, self.port, timeout=3.0)
        self._thread = None
        self._capture = None
