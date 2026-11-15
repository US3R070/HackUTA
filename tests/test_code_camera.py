"""Code camera: detection, easing, lost-line fallback, settings safety (no real screen)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest

from escalation_mcp.screen import code_camera as cc

W, H = 2560, 1600
HL = (0x5C, 0x44, 0x00)  # rgb of the default marker color
VSCODE = 111


def _frame(band: bool = True) -> np.ndarray:
    f = np.full((H, W, 4), 30, np.uint8)
    if band:
        f[700:745, 300:2400, :3] = (HL[2], HL[1], HL[0])  # BGR order
    return f


class Fake:
    def __init__(self) -> None:
        self.frame = _frame(True)
        self.window = (VSCODE, (0, 0, W, H))
        self.owner = VSCODE

    def source(self, tmp_path: Path) -> cc.CodeCameraSource:
        cfg = cc.CameraConfig(workspace=tmp_path, lost_grace_s=0.2, jump_grace_s=0.2)
        return cc.CodeCameraSource(
            cfg,
            max_width=1280,
            grab_raw=lambda: self.frame,
            screen_size=(W, H),
            find_window=lambda: self.window,
            window_at=lambda x, y: self.owner,
            code_cli="code",
        )


def _run(src: cc.CodeCameraSource, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        src.grab()
        time.sleep(1 / 30)


def test_find_line_exact_box():
    assert cc.find_line(_frame(True), HL) == (300, 700, 2400, 745)
    assert cc.find_line(_frame(False), HL) is None


def test_camera_eases_without_overshoot():
    cam = cc.Camera(W, H, smooth_s=0.6)
    cam.aim(800, 720, 1000)
    widths = []
    for _ in range(60):
        cam.step(1 / 30)
        widths.append(cam.pos[2])
    assert abs(cam.pos[2] - 1000) < 5
    assert min(widths) > 1000 - 1  # never zooms past the target
    cam.overview()
    for _ in range(90):
        cam.step(1 / 30)
    assert cam.at_overview()


def test_idle_camera_is_plain_downscaled_screen(tmp_path):
    src = Fake().source(tmp_path)
    img = src.grab()
    assert img.size == (1280, 800)  # same as ScreenFrameSource on a 2560x1600 screen


def _focus_now(src: cc.CodeCameraSource) -> None:
    """focus() minus the `code -g` subprocess."""
    src.mode = "focus"
    src.focus_started = time.monotonic()
    src.last_seen = 0.0
    src.lost = False


def test_focus_zooms_then_falls_back_when_vscode_closes(tmp_path):
    fake = Fake()
    src = fake.source(tmp_path)
    _focus_now(src)
    _run(src, 0.9)
    assert not src.lost
    x0, y0, x1, y1 = src.camera.rect()
    assert x1 - x0 < W * 0.7  # zoomed in
    assert y0 < 722 < y1  # line in view
    assert src.grab().size == (1280, 720)  # 16:9 while focused

    fake.window = None  # VS Code closed
    _run(src, 1.2)
    assert src.lost
    assert src.camera.rect() == (0, 0, W, H)


def test_marker_inside_another_window_is_ignored(tmp_path):
    """Our own stream preview shows the marker color too; never chase it."""
    fake = Fake()
    fake.owner = 222  # something else is on top where the band is
    src = fake.source(tmp_path)
    _focus_now(src)
    _run(src, 0.8)
    assert src.lost
    assert src.camera.rect() == (0, 0, W, H)


def test_reacquire_after_loss(tmp_path):
    fake = Fake()
    fake.window = None
    src = fake.source(tmp_path)
    _focus_now(src)
    _run(src, 0.6)
    assert src.lost
    fake.window = (VSCODE, (0, 0, W, H))
    _run(src, 0.9)
    assert not src.lost
    assert src.camera.rect()[2] - src.camera.rect()[0] < W * 0.7


def test_overview_returns_to_passthrough(tmp_path):
    src = Fake().source(tmp_path)
    _focus_now(src)
    _run(src, 0.5)
    src.overview()
    _run(src, 1.2)
    assert src.grab().size == (1280, 800)


def test_resolve_point_queries(tmp_path):
    (tmp_path / "cfg.yaml").write_text("batch: 64\nlr: 5.0\n", encoding="utf-8")
    t, _ = cc.resolve("cfg.yaml:2", tmp_path)
    assert t and t.line == 2
    t, _ = cc.resolve("lr:", tmp_path)
    assert t and t.label(tmp_path) == "cfg.yaml:2"
    assert cc.resolve("nope.yaml:3", tmp_path)[0] is None or cc.resolve("nope.yaml:3", tmp_path)[0].line
    assert cc.resolve("", tmp_path) == (None, [])


def test_settings_patch_restores_and_survives_crash(tmp_path):
    vs = tmp_path / ".vscode"
    vs.mkdir()
    original = '{\n  // mine\n  "editor.tabSize": 2,\n}'
    (vs / "settings.json").write_text(original, encoding="utf-8")

    p = cc.SettingsPatch(tmp_path, {"editor.fontSize": 18})
    p.apply()
    patched = json.loads((vs / "settings.json").read_text(encoding="utf-8"))
    assert patched == {"editor.tabSize": 2, "editor.fontSize": 18}
    p.restore()
    assert (vs / "settings.json").read_text(encoding="utf-8") == original

    # simulated crash: applied, never restored; a new instance cleans it up
    cc.SettingsPatch(tmp_path, {"editor.fontSize": 18}).apply()
    assert cc.SettingsPatch(tmp_path, {}).restore_leftover()
    assert (vs / "settings.json").read_text(encoding="utf-8") == original


def test_settings_patch_removes_file_it_created(tmp_path):
    p = cc.SettingsPatch(tmp_path, {"editor.fontSize": 18})
    p.apply()
    assert (tmp_path / ".vscode" / "settings.json").exists()
    p.restore()
    assert not (tmp_path / ".vscode").exists()


@pytest.mark.asyncio
async def test_focus_code_needs_screen_share(monkeypatch):
    from escalation_mcp import session as sess

    monkeypatch.setattr(sess.STATE, "require", lambda iid: object())
    monkeypatch.setattr(sess.RUNTIME, "code_camera", None)
    r = await sess.focus_code("inc_x", "lr:")
    assert r["ok"] is False and "screen" in r["error"]
    r = await sess.show_full_screen("inc_x")
    assert r["ok"] is True
