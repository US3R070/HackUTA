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
        settings=Settings(
            mode="mock",
            preview_port=18765,
            screen_fps=2.0,
            # short call timers so lifecycle tests run in ~seconds
            call_no_answer_s=1.5,
            call_idle_timeout_s=2.0,
            call_done_grace_s=0.5,
            call_leave_grace_s=0.3,
            call_max_attempts=2,
        ),
        fake_frames=SolidFrameSource(320, 180),
        sink=MockSink(),
        channel=MockChannel(),
    )
    yield tmp_path / "runs"
    # Cleanup any active escalation
    active = await STATE.get_active()
    if active:
        try:
            await sess.reset_runtime(incident_id=active.incident_id, save_report=False)
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

    # Agent done while the human is still on the call: monitor stays up for the grace.
    ended = await sess.end_escalation(incident_id, "resolved", "fixed in test")
    assert ended["ok"]
    assert ended["call_state"] == "agent_done"
    assert STATE.active is not None
    assert sess.RUNTIME.preview is not None
    await _wait_until(lambda: STATE.active is None, timeout=5.0)
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


# ---------------------------------------------------------------------------
# Call lifecycle: monitor stays up after decisions; ends on hangup / leave / timers
# ---------------------------------------------------------------------------


async def _wait_until(pred, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not pred():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


async def _next_decision(incident_id: str, timeout: float = 5.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        got = await STATE.get_human_response(incident_id, 0)
        if got["decision"] != "none":
            return got
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("no decision arrived")
        await asyncio.sleep(0.05)


async def _start(share: str = "screen", **kw) -> str:
    result = await sess.start_escalation(
        title="NaN loss",
        summary="Loss went NaN",
        failure_type="nan",
        severity="HIGH",
        proposed_fix="lower lr",
        share=share,
        **kw,
    )
    assert result["call_state"] == "waiting_answer"
    return result["incident_id"]


@pytest.mark.asyncio
async def test_decision_keeps_monitor_live_until_hangup(mock_runtime: Path) -> None:
    iid = await _start(default_action="keep run paused at ckpt_4000")
    assert simulate_main([iid, "join"]) == 0
    await _wait_until(lambda: STATE.active.call.phase == "live")

    assert simulate_main([iid, "approve"]) == 0
    got = await _next_decision(iid)
    assert got["decision"] == "approve"
    assert got["call_state"] == "watching"
    assert sess.RUNTIME.preview is not None  # still streaming after the decision

    assert simulate_main([iid, "drop batch 4200 too"]) == 0
    got = await _next_decision(iid)
    assert got["decision"] == "instruction" and got["text"] == "drop batch 4200 too"

    assert simulate_main([iid, "hangup"]) == 0
    got = await _next_decision(iid)
    assert got["decision"] == "hangup"
    assert got["end_reason"] == "hangup"
    assert got["run_default_action"] is False  # human already decided
    assert sess.RUNTIME.preview is None  # media gone, incident kept for polling
    assert STATE.active is not None

    again = await STATE.get_human_response(iid, 0)
    assert again["decision"] == "hangup"  # sticky

    ended = await sess.end_escalation(iid, "resolved", "done")
    assert ended["ok"] and STATE.active is None
    assert (mock_runtime / iid / "final_report.json").exists()
    outbox = (mock_runtime / iid / "outbox.jsonl").read_text(encoding="utf-8")
    assert '"call_state"' in outbox and '"watching"' in outbox and '"ended"' in outbox


@pytest.mark.asyncio
async def test_caller_leaves_without_deciding_runs_default(mock_runtime: Path) -> None:
    iid = await _start(share="none", default_action="keep run paused")
    assert simulate_main([iid, "join"]) == 0
    await _wait_until(lambda: STATE.active.call.phase == "live")
    assert simulate_main([iid, "leave"]) == 0
    got = await STATE.get_human_response(iid, wait_seconds=3)
    assert got["decision"] == "hangup"
    assert got["end_reason"] == "caller_left"
    assert got["run_default_action"] is True
    assert got["default_action"] == "keep run paused"


@pytest.mark.asyncio
async def test_rejoin_within_grace_keeps_call(mock_runtime: Path) -> None:
    iid = await _start(share="none")
    assert simulate_main([iid, "join"]) == 0
    await _wait_until(lambda: STATE.active.call.phase == "live")
    assert simulate_main([iid, "leave"]) == 0
    assert simulate_main([iid, "join"]) == 0
    await asyncio.sleep(0.6)  # > leave grace
    assert not STATE.active.call.ended


@pytest.mark.asyncio
async def test_no_answer_redials_once_then_default(mock_runtime: Path) -> None:
    iid = await _start(share="none")
    got = await STATE.get_human_response(iid, wait_seconds=6)
    assert got["decision"] == "hangup"
    assert got["end_reason"] == "no_answer"
    assert got["attempt"] == 2
    assert got["run_default_action"] is True
    assert "No changes" in got["default_action"]  # safe fallback
    redials = (mock_runtime / iid / "redials.jsonl").read_text(encoding="utf-8")
    assert '"attempt": 2' in redials


@pytest.mark.asyncio
async def test_idle_timeout_after_decision(mock_runtime: Path) -> None:
    iid = await _start(share="none")
    assert simulate_main([iid, "approve"]) == 0  # decision implies caller is there
    assert (await _next_decision(iid))["decision"] == "approve"
    got = await STATE.get_human_response(iid, wait_seconds=5)
    assert got["decision"] == "hangup"
    assert got["end_reason"] == "idle_timeout"
    assert got["run_default_action"] is False


@pytest.mark.asyncio
async def test_end_escalation_before_answer_hangs_up_now(mock_runtime: Path) -> None:
    iid = await _start(share="none")
    ended = await sess.end_escalation(iid, "aborted", "fixed it myself")
    assert ended["ok"] and ended["call_state"] == "ended"
    assert STATE.active is None


@pytest.mark.asyncio
async def test_end_escalation_hang_up_now(mock_runtime: Path) -> None:
    iid = await _start()
    assert simulate_main([iid, "join"]) == 0
    await _wait_until(lambda: STATE.active.call.phase == "live")
    ended = await sess.end_escalation(iid, "resolved", "done", hang_up_now=True)
    assert ended["call_state"] == "ended"
    assert STATE.active is None and sess.RUNTIME.preview is None


# ---------------------------------------------------------------------------
# Integration guards for the auto-escalation path (chert/escalate.py): nobody
# polls decisions there, Chert may queue the call, operator exits with Ctrl+C.
# ---------------------------------------------------------------------------


class RecordingSink(MockSink):
    """MockSink that records phone-side calls and can simulate a slow place_call."""

    def __init__(self, place_delay: float = 0.0) -> None:
        self.place_delay = place_delay
        self.redials: list[int] = []
        self.end_calls: list[str] = []

    async def send_packet(self, packet):  # type: ignore[override]
        await asyncio.sleep(self.place_delay)
        return await super().send_packet(packet)

    async def redial(self, packet, attempt):  # type: ignore[override]
        self.redials.append(attempt)
        return []

    async def end_call(self, incident_id):  # type: ignore[override]
        self.end_calls.append(incident_id)
        return []


@pytest.mark.asyncio
async def test_answered_call_without_decisions_never_idles_out(mock_runtime: Path) -> None:
    iid = await _start(share="none")
    assert simulate_main([iid, "join"]) == 0
    await _wait_until(lambda: STATE.active.call.phase == "live")
    await asyncio.sleep(2.5)  # > CALL_IDLE_TIMEOUT_S (2.0) in the fixture
    assert not STATE.active.call.ended
    got = await STATE.get_human_response(iid, 0)
    assert got["decision"] == "none" and got["call_state"] == "live"
    assert got["ends_in_s"] is None


@pytest.mark.asyncio
async def test_no_answer_timer_starts_after_call_is_placed(mock_runtime: Path) -> None:
    sink = RecordingSink(place_delay=2.0)  # Chert queue longer than CALL_NO_ANSWER_S (1.5)
    sess.RUNTIME.sink = sink
    iid = await _start(share="none")
    await asyncio.sleep(0.5)
    assert sink.redials == []  # timer only started once placing returned
    assert STATE.active.call.phase == "waiting_answer"


@pytest.mark.asyncio
async def test_operator_exit_hangs_up_immediately(mock_runtime: Path) -> None:
    """Mirrors chert/escalate.py: start, narrate, Ctrl+C -> end_escalation(hang_up_now)."""
    sink = RecordingSink()
    sess.RUNTIME.sink = sink
    iid = await _start()
    await sess.report_action(iid, "Grok diagnosed failure", detail="lr -> 3e-4", status="done")
    assert simulate_main([iid, "join"]) == 0
    await _wait_until(lambda: STATE.active.call.phase == "live")
    ended = await sess.end_escalation(iid, "aborted", "operator exit", hang_up_now=True)
    assert ended["call_state"] == "ended"
    assert sink.end_calls == [iid]
    assert STATE.active is None and sess.RUNTIME.preview is None


@pytest.mark.asyncio
async def test_chert_redial_skips_active_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from escalation_mcp.sink import chert as chert_sink
    from escalation_mcp.models import EscalationPacket

    sink = chert_sink.ChertSink(Settings(mode="live"))
    sink.call_ids["inc_x"] = "call_1"
    placed: list[str] = []

    async def fake_place(packet):
        placed.append(packet.incident_id)
        return []

    monkeypatch.setattr(sink, "_place", fake_place)
    packet = EscalationPacket(incident_id="inc_x", created_at="t", severity="HIGH",
                              failure_type="nan", title="t", summary="s")

    async def ringing(call_id):
        return {"status": "ringing", "terminal_reason": None}

    monkeypatch.setattr(sink, "_call_status", ringing)
    warnings = await sink.redial(packet, 2)
    assert placed == [] and "not redialing" in warnings[0]

    async def unknown(call_id):
        return None  # status lookup failed -> be safe, don't redial

    monkeypatch.setattr(sink, "_call_status", unknown)
    assert (await sink.redial(packet, 2)) and placed == []

    async def missed(call_id):
        return {"status": "failed", "terminal_reason": "no_answer"}

    monkeypatch.setattr(sink, "_call_status", missed)
    assert await sink.redial(packet, 2) == []
    assert placed == ["inc_x"]


@pytest.mark.asyncio
async def test_livekit_channel_forwards_presence() -> None:
    from escalation_mcp.channel.livekit import LiveKitChannel
    from escalation_mcp.livekit_room import LiveKitRoomConnection

    conn = LiveKitRoomConnection(Settings())
    events: list[str] = []

    async def on_presence(ev: str) -> None:
        events.append(ev)

    async def on_decision(d) -> None:
        pass

    conn.bridge_joined.set()  # bridge joined before the channel started
    ch = LiveKitChannel(conn)
    await ch.start("inc_p", on_decision, on_presence)
    conn._fire_presence("left")
    await asyncio.sleep(0.05)
    assert events == ["joined", "left"]
    await ch.stop()
    conn._fire_presence("joined")
    await asyncio.sleep(0.05)
    assert events == ["joined", "left"]  # handler removed on stop
