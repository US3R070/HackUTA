"""Decision queue, sticky stop, long-poll timeout."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from escalation_mcp.models import EscalationPacket, HumanDecision
from escalation_mcp.state import ActiveIncident, EscalationState


def _incident(state_id: str = "inc_state_1") -> ActiveIncident:
    packet = EscalationPacket(
        incident_id=state_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        severity="HIGH",
        failure_type="nan",
        title="t",
        summary="s",
    )
    return ActiveIncident(
        incident_id=state_id,
        title="t",
        severity="HIGH",
        packet=packet,
        room=f"esc-{state_id}",
        preview_url="",
        share="none",
    )


@pytest.mark.asyncio
async def test_decision_queue_fifo() -> None:
    state = EscalationState()
    inc = _incident()
    await state.set_active(inc)

    for i, kind in enumerate(["approve", "reject", "instruction"]):
        await state.enqueue_decision(
            HumanDecision(
                incident_id=inc.incident_id,
                ts=datetime.now(timezone.utc).isoformat(),
                decision=kind,  # type: ignore[arg-type]
                text=f"n{i}" if kind == "instruction" else None,
                **{"from": "tester"},
            )
        )

    first = await state.get_human_response(inc.incident_id, wait_seconds=0)
    assert first["decision"] == "approve"
    second = await state.get_human_response(inc.incident_id, wait_seconds=0)
    assert second["decision"] == "reject"
    third = await state.get_human_response(inc.incident_id, wait_seconds=0)
    assert third["decision"] == "instruction"
    assert third["text"] == "n2"


@pytest.mark.asyncio
async def test_sticky_stop() -> None:
    state = EscalationState()
    inc = _incident("inc_stop")
    await state.set_active(inc)
    await state.enqueue_decision(
        HumanDecision(
            incident_id=inc.incident_id,
            ts=datetime.now(timezone.utc).isoformat(),
            decision="stop",
            text=None,
            **{"from": "alice"},
        )
    )
    a = await state.get_human_response(inc.incident_id, 0)
    b = await state.get_human_response(inc.incident_id, 0)
    assert a["decision"] == "stop"
    assert b["decision"] == "stop"
    assert b["from"] == "alice"
    assert inc.stop_banner_from == "alice"


@pytest.mark.asyncio
async def test_long_poll_timeout() -> None:
    state = EscalationState()
    inc = _incident("inc_wait")
    await state.set_active(inc)
    t0 = asyncio.get_event_loop().time()
    result = await state.get_human_response(inc.incident_id, wait_seconds=1)
    elapsed = asyncio.get_event_loop().time() - t0
    assert result["decision"] == "none"
    assert elapsed >= 0.9
