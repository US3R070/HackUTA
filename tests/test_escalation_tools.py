"""Tools end-to-end in mock mode (no network, fake frames)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from escalation_mcp.channel.mock import MockChannel
from escalation_mcp.config import Settings, runs_dir
from escalation_mcp.screen.capture import SolidFrameSource
from escalation_mcp import session as sess
from escalation_mcp.sink.mock import MockSink
from escalation_mcp.state import STATE
from escalation_mcp.simulate import main as simulate_main


@pytest.fixture
async def mock_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Isolate runs/ under tmp by patching runs_dir consumers via env + monkeypatch
    monkeypatch.setenv("ESCALATION_MODE", "mock")
    monkeypatch.setenv("PREVIEW_PORT", "18765")

    # Point runs_dir to tmp
    import escalation_mcp.config as cfg
    import escalation_mcp.packet as pkt
    import escalation_mcp.simulate as sim

    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)

    def _runs() -> Path:
        return runs

    monkeypatch.setattr(cfg, "runs_dir", _runs)
    monkeypatch.setattr(pkt, "runs_dir", _runs)
    monkeypatch.setattr(sim, "runs_dir", _runs)

    # Reset global state
    STATE.active = None
    sess.configure_runtime(
        settings=Settings(mode="mock", preview_port=18765, screen_fps=2.0),
        fake_frames=SolidFrameSource(320, 180),
        sink=MockSink(),
        channel=MockChannel(),
    )
    yield tmp_path / "runs"
    # Cleanup any active escalation
    active = await STATE.get_active()
    if active:
        try:
            await sess.end_escalation(active.incident_id, "aborted", "test cleanup")
        except Exception:  # noqa: BLE001
            STATE.active = None
            if sess.RUNTIME.preview:
                await sess.RUNTIME.preview.stop()
            sess.RUNTIME.stop_event.set()


@pytest.mark.asyncio
async def test_tools_mock_roundtrip(mock_runtime: Path) -> None:
    result = await sess.start_escalation(
        title="Training run crashed: NaN loss",
        summary="Loss became NaN. LR too high.",
        failure_type="nan",
        severity="HIGH",
        details={"log_tail": ["step=42 loss=nan"], "config": {"lr": 5.0}},
        proposed_fix="Lower lr to 3e-4",
        run_id="run_test",
        share="screen",
    )
    assert result["status"] == "active"
    incident_id = result["incident_id"]
    assert result["sink"] == "mock"
    assert (mock_runtime / incident_id / "packet.json").exists()

    # Idempotent-ish second call
    again = await sess.start_escalation(
        title="other",
        summary="other",
        failure_type="other",
        severity="LOW",
        share="none",
    )
    assert again["incident_id"] == incident_id
    assert again["status"] == "already_active"

    a1 = await sess.report_action(incident_id, "read_file", "config.yaml", "started")
    a2 = await sess.report_action(incident_id, "read_file", "config.yaml", "done")
    assert a1["ok"] and a2["seq"] == 2

    none = await STATE.get_human_response(incident_id, wait_seconds=0)
    assert none["decision"] == "none"

    # simulate approve via inbox
    assert simulate_main([incident_id, "approve"]) == 0
    # allow mock channel tail to pick it up
    decision = None
    for _ in range(25):
        decision = await STATE.get_human_response(incident_id, wait_seconds=0)
        if decision["decision"] != "none":
            break
        await asyncio.sleep(0.2)
    assert decision is not None
    assert decision["decision"] == "approve"

    view = await sess.set_screen_view(incident_id, "screen")
    assert view["ok"]

    ended = await sess.end_escalation(incident_id, "resolved", "fixed in test")
    assert ended["ok"]
    assert (mock_runtime / incident_id / "agent_log.jsonl").exists()
    assert (mock_runtime / incident_id / "outbox.jsonl").exists()
    assert (mock_runtime / incident_id / "final_report.json").exists()

    # No secret-looking leftovers from our test payload
    packet = json.loads((mock_runtime / incident_id / "packet.json").read_text())
    assert packet["failure_type"] == "nan"
    assert STATE.active is None


@pytest.mark.asyncio
async def test_stop_sticky_via_simulate(mock_runtime: Path) -> None:
    result = await sess.start_escalation(
        title="stuck job",
        summary="Process stuck",
        failure_type="stuck",
        severity="HIGH",
        share="none",
    )
    incident_id = result["incident_id"]
    # share=none skips channel start with capture — but channel should still start
    assert simulate_main([incident_id, "stop"]) == 0
    got = None
    for _ in range(25):
        got = await STATE.get_human_response(incident_id, 0)
        if got["decision"] != "none":
            break
        await asyncio.sleep(0.2)
    assert got is not None and got["decision"] == "stop"
    again = await STATE.get_human_response(incident_id, 0)
    assert again["decision"] == "stop"
    await sess.end_escalation(incident_id, "aborted", "stopped by human")
