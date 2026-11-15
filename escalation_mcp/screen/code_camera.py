"""Code camera: pan/zoom the shared screen onto one line of code in VS Code.

No editor extension and no OCR:
  1. While the camera is in use, workspace settings make VS Code paint the cursor
     line a unique solid color (and use a fixed font size).
  2. `code -r -g file:line:col` jumps VS Code to the target (it centers the line).
  3. Every captured frame, the colored band is located with numpy (a few ms) - but
     only inside a visible, uncovered VS Code window, so our own stream preview or
     stale pixels can never be mistaken for it.
  4. A virtual camera eases toward that line (critically damped spring) and the
     frame is cropped/scaled from native resolution. If the line disappears
     (VS Code closed/covered/scrolled away) the camera eases back to the full screen.

Zoom and font size are fixed per process (env CODE_CAMERA_ZOOM / CODE_CAMERA_FONT_SIZE).
When the camera is idle on the full screen, frames are identical to ScreenFrameSource.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from escalation_mcp.screen.capture import downscale

logger = logging.getLogger("escalation_mcp")

FOCUS_ASPECT = 16 / 9
SEARCH_EXTS = {".py", ".yaml", ".yml", ".json", ".toml", ".cfg", ".md", ".txt", ".log", ".jsonl"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "poc"}
_BACKUP_SUFFIX = ".code-camera-backup"
_ABSENT = "__code_camera_file_absent__"


@dataclass
class CameraConfig:
    workspace: Path
    zoom: float = 1.8
    font_size: int = 18
    color: str = "#5c4400"
    smooth_s: float = 0.6     # roughly how long a pan takes
    lost_grace_s: float = 0.5  # line missing this long -> back to full screen
    jump_grace_s: float = 2.5  # time allowed for `code -g` to land

    @classmethod
    def from_env(cls, default_workspace: Path) -> "CameraConfig":
        def _f(name: str, default: float) -> float:
            try:
                return float(os.getenv(name) or default)
            except ValueError:
                return default

        ws = os.getenv("CODE_CAMERA_WORKSPACE") or os.getenv("HACKUTA_ROOT") or str(default_workspace)
        return cls(
            workspace=Path(ws).resolve(),
            zoom=_f("CODE_CAMERA_ZOOM", 1.8),
            font_size=int(_f("CODE_CAMERA_FONT_SIZE", 18)),
            color=os.getenv("CODE_CAMERA_COLOR") or "#5c4400",
        )


# ---------------------------------------------------------------- VS Code settings

class SettingsPatch:
    """Temporarily merge keys into <workspace>/.vscode/settings.json.

    A backup file is written next to it before patching, so a crashed/killed MCP can
    be cleaned up on the next start via restore_leftover()."""

    def __init__(self, workspace: Path, patch: dict) -> None:
        self.path = workspace / ".vscode" / "settings.json"
        self.backup = self.path.with_name(self.path.name + _BACKUP_SUFFIX)
        self.patch = patch
        self.applied = False
        self._lock = threading.Lock()

    @staticmethod
    def _parse(text: str) -> dict:
        try:
            return json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            stripped = re.sub(r"^\s*//.*$", "", text, flags=re.M)
            stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
            return json.loads(stripped)

    def restore_leftover(self) -> bool:
        """Undo a patch left behind by a previous process. Returns True if one was found."""
        if not self.backup.exists():
            return False
        original = self.backup.read_text(encoding="utf-8")
        self.backup.unlink(missing_ok=True)
        if original == _ABSENT:
            self.path.unlink(missing_ok=True)
            try:
                self.path.parent.rmdir()  # only if empty
            except OSError:
                pass
        else:
            self.path.write_text(original, encoding="utf-8")
        logger.info("code camera: restored leftover VS Code settings at %s", self.path)
        return True

    def apply(self) -> None:
        with self._lock:
            if self.applied:
                return
            self.restore_leftover()
            original = self.path.read_text(encoding="utf-8") if self.path.exists() else None
            current = self._parse(original) if original is not None else {}
            merged = dict(current)
            for key, value in self.patch.items():
                if isinstance(value, dict) and isinstance(merged.get(key), dict):
                    merged[key] = {**merged[key], **value}
                else:
                    merged[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.backup.write_text(original if original is not None else _ABSENT, encoding="utf-8")
            self.path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
            self.applied = True
            logger.info("code camera: patched VS Code settings at %s", self.path)

    def restore(self) -> None:
        with self._lock:
            if not self.applied:
                return
            self.applied = False
            self.restore_leftover()


# ---------------------------------------------------------------- query -> file:line

@dataclass
class Target:
    path: Path
    line: int
    col: int = 1

    def label(self, root: Path) -> str:
        try:
            rel = self.path.relative_to(root)
        except ValueError:
            rel = self.path
        return f"{rel.as_posix()}:{self.line}"


def parse_location(query: str, root: Path) -> Target | None:
    """'file:line' or 'file:line:col' (relative to root or absolute)."""
    m = re.fullmatch(r"(.+?):(\d+)(?::(\d+))?", query.strip())
    if not m:
        return None
    p = Path(m.group(1))
    if not p.is_absolute():
        p = root / p
    if not p.is_file():
        return None
    return Target(p.resolve(), max(1, int(m.group(2))), max(1, int(m.group(3) or 1)))


def search(text: str, root: Path, limit: int = 20) -> list[Target]:
    """Plain substring search over source/config/log files; exact case first."""
    exact: list[Target] = []
    loose: list[Target] = []
    needle = text.lower()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.suffix.lower() not in SEARCH_EXTS:
                continue
            try:
                lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                idx = line.find(text)
                if idx >= 0:
                    exact.append(Target(p, i, idx + 1))
                elif needle in line.lower():
                    loose.append(Target(p, i, line.lower().find(needle) + 1))
            if len(exact) >= limit:
                return exact
    return (exact + loose)[:limit]


def resolve(query: str, root: Path) -> tuple[Target | None, list[Target]]:
    q = (query or "").strip()
    if not q:
        return None, []
    loc = parse_location(q, root)
    if loc:
        return loc, []
    hits = search(q, root)
    return (hits[0] if hits else None), hits[1:4]


# ---------------------------------------------------------------- windows (Windows only)

def make_dpi_aware() -> None:
    """Physical pixels everywhere, so window rects line up with mss frames."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def find_vscode_window() -> tuple[int, tuple[int, int, int, int]] | None:
    """(hwnd, (left, top, right, bottom)) of the largest visible VS Code window."""
    if sys.platform != "win32":
        return None
    try:
        import pygetwindow as gw  # type: ignore
    except ImportError:
        return None
    best = None
    for w in gw.getAllWindows():
        title = w.title or ""
        if "Visual Studio Code" not in title or w.isMinimized or w.width < 200 or w.height < 200:
            continue
        if best is None or w.width * w.height > best.width * best.height:
            best = w
    if best is None:
        return None
    return best._hWnd, (best.left, best.top, best.left + best.width, best.top + best.height)


def top_window_at(x: int, y: int) -> int:
    """Root hwnd of whatever is actually visible at screen point (x, y)."""
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    root = user32.GetAncestor(hwnd, 2) if hwnd else None  # GA_ROOT
    return int(root or 0)


# ---------------------------------------------------------------- detection

def hex_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def find_line(bgr: np.ndarray, rgb: tuple[int, int, int], step: int = 4,
              min_px: int = 150) -> tuple[int, int, int, int] | None:
    """Exact-color band on a raw BGR(A) frame -> (x0, y0, x1, y1) or None."""
    r, g, b = rgb
    sub = bgr[:, ::step]
    mask = (sub[..., 2] == r) & (sub[..., 1] == g) & (sub[..., 0] == b)
    counts = mask.sum(axis=1)
    rows = np.flatnonzero(counts >= max(4, min_px // step))
    if rows.size == 0:
        return None
    runs = np.split(rows, np.flatnonzero(np.diff(rows) > 1) + 1)
    best = max(runs, key=lambda rr: counts[rr].sum())
    y0, y1 = int(best.min()), int(best.max()) + 1
    band = bgr[y0:y1]
    full = (band[..., 2] == r) & (band[..., 1] == g) & (band[..., 0] == b)
    cols = np.flatnonzero(full.any(axis=0))
    return int(cols.min()), y0, int(cols.max()) + 1, y1


# ---------------------------------------------------------------- camera

class Camera:
    """Rect (cx, cy, w, h) eased toward a target with a critically damped spring.
    Overview = the whole screen (its own aspect); focus = 16:9 around the line."""

    def __init__(self, screen_w: int, screen_h: int, smooth_s: float = 0.6) -> None:
        self.sw, self.sh = screen_w, screen_h
        self.full = np.array([screen_w / 2, screen_h / 2, screen_w, screen_h], dtype=np.float64)
        self.pos = self.full.copy()
        self.vel = np.zeros(4)
        self.target = self.full.copy()
        self.omega = 5.8 / max(0.05, smooth_s)  # ~98% settled after smooth_s

    def focus_width_for(self, zoom: float) -> float:
        return min(self.sw, self.sh * FOCUS_ASPECT) / max(1.0, zoom)

    def aim(self, cx: float, cy: float, w: float) -> None:
        w = float(np.clip(w, 160, min(self.sw, self.sh * FOCUS_ASPECT)))
        self.target = np.array([cx, cy, w, w / FOCUS_ASPECT])

    def overview(self) -> None:
        self.target = self.full.copy()

    def step(self, dt: float) -> None:
        """Exact critically damped step: stable at any frame rate (10 fps capture included)."""
        dt = float(np.clip(dt, 0.0, 0.5))
        w = self.omega
        x0 = self.pos - self.target
        v0 = self.vel
        c = v0 + w * x0
        decay = np.exp(-w * dt)
        self.pos = self.target + (x0 + c * dt) * decay
        self.vel = (v0 - w * c * dt) * decay

    def at_overview(self, tol: float = 1.5) -> bool:
        return bool(np.all(np.abs(self.target - self.full) < 1e-6)
                    and np.all(np.abs(self.pos - self.full) < tol))

    def rect(self) -> tuple[int, int, int, int]:
        cx, cy, w, h = self.pos
        w = float(np.clip(w, 16, self.sw))
        h = float(np.clip(h, 9, self.sh))
        x0 = float(np.clip(cx - w / 2, 0, self.sw - w))
        y0 = float(np.clip(cy - h / 2, 0, self.sh - h))
        return int(round(x0)), int(round(y0)), int(round(x0 + w)), int(round(y0 + h))


# ---------------------------------------------------------------- frame source

class CodeCameraSource:
    """FrameSource for share="screen" that can pan/zoom onto a VS Code line.

    grab() is called from the capture thread; focus()/overview() from tool handlers.
    """

    def __init__(
        self,
        config: CameraConfig,
        *,
        monitor_index: int = 1,
        max_width: int = 1280,
        grab_raw: Callable[[], np.ndarray] | None = None,
        screen_size: tuple[int, int] | None = None,
        screen_origin: tuple[int, int] = (0, 0),
        find_window: Callable[[], tuple[int, tuple[int, int, int, int]] | None] = find_vscode_window,
        window_at: Callable[[int, int], int] = top_window_at,
        code_cli: str | None = None,
    ) -> None:
        self.config = config
        self.max_width = max_width
        self.rgb = hex_rgb(config.color)
        self._find_window = find_window
        self._window_at = window_at
        self._code_cli = code_cli
        self._lock = threading.Lock()
        self.warning: str | None = None

        if grab_raw is None:
            make_dpi_aware()
            import mss

            self._sct = None
            self._sct_tid: int | None = None
            with (mss.MSS() if hasattr(mss, "MSS") else mss.mss()) as sct:
                monitors = sct.monitors
                mon = monitors[min(max(monitor_index, 1), len(monitors) - 1)]
            self._mon = mon
            self._grab_raw = self._grab_mss
            screen_size = (mon["width"], mon["height"])
            screen_origin = (mon["left"], mon["top"])
        else:
            self._grab_raw = grab_raw
            assert screen_size is not None
        self.origin = screen_origin
        self.camera = Camera(*screen_size, smooth_s=config.smooth_s)

        self.patch = SettingsPatch(config.workspace, {
            "editor.fontSize": config.font_size,
            "editor.renderLineHighlight": "all",
            "editor.renderLineHighlightOnlyWhenFocus": False,
            "editor.cursorBlinking": "solid",
            "workbench.colorCustomizations": {
                "editor.lineHighlightBackground": config.color,
                "editor.lineHighlightBorder": "#00000000",
            },
        })
        try:
            self.patch.restore_leftover()  # clean up after a crashed previous run
        except Exception:  # noqa: BLE001
            logger.exception("code camera: leftover settings restore failed")

        self.mode = "overview"
        self.target_label = ""
        self.line_box: tuple[int, int, int, int] | None = None
        self.last_seen = 0.0
        self.focus_started = 0.0
        self.lost = False
        self.streak = 0
        self._last_grab = time.monotonic()
        self._vscode: tuple[int, tuple[int, int, int, int]] | None = None
        self._vscode_checked = 0.0

    # -- raw capture (mss handles are per-thread)
    def _grab_mss(self) -> np.ndarray:
        import mss

        tid = threading.get_ident()
        if self._sct is None or self._sct_tid != tid:
            self._sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
            self._sct_tid = tid
        shot = self._sct.grab(self._mon)
        return np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)

    # -- detection
    def _vscode_window(self):
        now = time.monotonic()
        if now - self._vscode_checked > 0.3:
            try:
                self._vscode = self._find_window()
            except Exception:  # noqa: BLE001
                self._vscode = None
            self._vscode_checked = now
        return self._vscode

    def _detect(self, frame: np.ndarray):
        win = self._vscode_window()
        if win is None:
            return None
        hwnd, (l, t, r, b) = win
        ox, oy = self.origin
        x0, y0 = max(0, l - ox), max(0, t - oy)
        x1, y1 = min(frame.shape[1], r - ox), min(frame.shape[0], b - oy)
        if x1 - x0 < 50 or y1 - y0 < 50:
            return None
        box = find_line(frame[y0:y1, x0:x1], self.rgb)
        if box is None:
            return None
        box = (box[0] + x0, box[1] + y0, box[2] + x0, box[3] + y0)
        cy = (box[1] + box[3]) // 2
        for px in (box[0] + 8, (box[0] + box[2]) // 2):
            if self._window_at(px + ox, cy + oy) != hwnd:
                return None  # covered by another window (e.g. our own preview)
        return box

    def _aim(self, box) -> None:
        x0, y0, x1, y1 = box
        w = self.camera.focus_width_for(self.config.zoom)
        # left-align on the gutter (code reads left to right), small margin
        self.camera.aim(x0 - w * 0.03 + w / 2, (y0 + y1) / 2, w)

    def _track(self, frame: np.ndarray) -> None:
        now = time.monotonic()
        box = self._detect(frame)
        if box:
            self.streak += 1
            self.line_box = box
            self.last_seen = now
            # first hit after a jump aims at once; after a loss wait for 2 hits
            if not self.lost or self.streak >= 2:
                self.lost = False
                self._aim(box)
        else:
            self.streak = 0
            since = max(self.last_seen, self.focus_started + self.config.jump_grace_s)
            if not self.lost and now - since > self.config.lost_grace_s:
                self.lost = True
                self.camera.overview()

    # -- FrameSource API
    def grab(self) -> Image.Image:
        frame = self._grab_raw()
        now = time.monotonic()
        with self._lock:
            if self.mode == "focus":
                self._track(frame)
            self.camera.step(now - self._last_grab)
            self._last_grab = now
            if self.mode == "overview" and self.camera.at_overview():
                rect = None
            else:
                rect = self.camera.rect()
        if rect is None:
            rgb = frame[..., :3][..., ::-1]
            return downscale(Image.fromarray(np.ascontiguousarray(rgb)), self.max_width)
        x0, y0, x1, y1 = rect
        crop = np.ascontiguousarray(frame[y0:y1, x0:x1, :3][..., ::-1])
        img = Image.fromarray(crop)
        out_w = min(self.max_width, max(2, img.width * 4))
        out_h = max(2, int(round(out_w * img.height / img.width)))
        out_w -= out_w % 2
        out_h -= out_h % 2
        return img.resize((out_w, out_h), Image.Resampling.BILINEAR)

    # -- control (blocking: call via asyncio.to_thread)
    def focus(self, query: str) -> dict:
        target, others = resolve(query, self.config.workspace)
        if target is None:
            return {"ok": False, "error": f"nothing matches {query!r}"}
        cli = self._code_cli or shutil.which("code") or shutil.which("code.cmd")
        if not cli:
            return {"ok": False, "error": "VS Code CLI `code` not found on PATH"}
        self.patch.apply()
        with self._lock:
            self.target_label = target.label(self.config.workspace)
            self.last_seen = 0.0
            self.lost = False
            self.streak = 0
            self.focus_started = time.monotonic()
            self.mode = "focus"
        t0 = time.monotonic()
        try:
            subprocess.run([cli, "-r", "-g", f"{target.path}:{target.line}:{target.col}"],
                           check=False, timeout=10, capture_output=True)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "target": self.target_label, "error": f"code -g failed: {exc}"}
        deadline = time.monotonic() + self.config.jump_grace_s
        while time.monotonic() < deadline and self.last_seen == 0.0:
            time.sleep(0.05)
        found = self.last_seen > 0.0
        result: dict = {"ok": found, "target": self.target_label,
                        "seconds": round(time.monotonic() - t0, 2)}
        if others:
            result["other_matches"] = [o.label(self.config.workspace) for o in others]
        if not found:
            result["error"] = ("line not visible on screen (VS Code closed, covered, or not "
                               "opened on the workspace folder); showing the full screen")
        return result

    def overview(self) -> dict:
        with self._lock:
            self.mode = "overview"
            self.lost = False
            self.camera.overview()
        return {"ok": True, "view": "full_screen"}

    def close(self) -> None:
        """Back to the full screen and undo the VS Code settings."""
        self.overview()
        try:
            self.patch.restore()
        except Exception:  # noqa: BLE001
            logger.exception("code camera: settings restore failed")

    def state(self) -> dict:
        return {"mode": self.mode, "target": self.target_label, "lost": self.lost,
                "line_box": self.line_box, "camera": self.camera.rect()}
