

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from escalation_mcp.call import CallController, CallTimings
from escalation_mcp.channel.base import Channel
from escalation_mcp.channel.mock import MockChannel
from escalation_mcp.config import Settings, load_settings
from escalation_mcp.models import (
    DEFAULT_SAFE_ACTION,
    ActionEvent,
    CallStateEvent,
    EndReason,
    FinalReport,
    HumanDecision,
)
from escalation_mcp.packet import (
    append_agent_log,
    build_packet,
    incident_dir,
    new_incident_id,
    save_packet,
)
from escalation_mcp.screen.backends.base import ScreenBackend
from escalation_mcp.screen.backends.preview import (
    PreviewBackend,
    wait_for_port_free,
)
from escalation_mcp.screen.capture import CaptureLoop, FrameSource, make_frame_source
from escalation_mcp.screen.overlay import draw_overlay
from escalation_mcp.sink.base import Sink
from escalation_mcp.sink.chert import ChertSink
from escalation_mcp.sink.mock import MockSink
from escalation_mcp.state import STATE, ActiveIncident

logger = logging.getLogger("escalation_mcp")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RuntimeHandles:
    settings: Settings
    sink: Sink
    channel: Channel
    preview: PreviewBackend | None = None
    livekit_backend: ScreenBackend | None = None
    room_conn: Any | None = None
    capture: CaptureLoop | None = None
    capture_task: asyncio.Task[None] | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    fake_frames: FrameSource | None = None  # injected in tests
    sharing: bool = False


RUNTIME = RuntimeHandles(settings=Settings(), sink=MockSink(), channel=MockChannel())


def configure_runtime(
    *,
    settings: Settings | None = None,
    fake_frames: FrameSource | None = None,
    sink: Sink | None = None,
    channel: Channel | None = None,
) -> RuntimeHandles:
    """(Re)configure global runtime. Used by server startup and tests."""
    global RUNTIME
    s = settings or load_settings(require_live=False)
    if sink is None:
        sink = ChertSink(s) if s.mode == "live" else MockSink()
    if channel is None:
        channel = MockChannel()
    RUNTIME = RuntimeHandles(
        settings=s,
        sink=sink,
        channel=channel,
        fake_frames=fake_frames,
    )
    return RUNTIME


def _overlay_fn(img):
    incident = STATE.active
    if not incident:
        return img
    return draw_overlay(
        img,
        title=incident.title,
        severity=incident.severity,
        actions=incident.overlay_actions,
        stop_from=incident.stop_banner_from,
        call_line=_call_line(incident),
    )


_PHASE_LABEL = {
    "waiting_answer": "CALLING",
    "live": "LIVE",
    "watching": "WATCHING",
    "agent_done": "DONE \u2713",
    "ended": "CALL ENDED",
}


def _fmt_secs(secs: float | None) -> str:
    if secs is None:
        return ""
    total = int(round(secs))
    return f"{total // 60}:{total % 60:02d}"


def _call_line(incident: ActiveIncident) -> str | None:
    call = incident.call
    if call is None:
        return None
    parts = [_PHASE_LABEL.get(call.phase, call.phase.upper())]
    if call.phase == "waiting_answer" and call.attempt > 1:
        parts[0] += f" (try {call.attempt})"
    d = incident.last_decision
    if d is not None:
        said = d.decision if d.decision != "instruction" else f'"{(d.text or "")[:40]}"'
        parts.append(f"You: {said}")
    remaining = call.ends_in_s()
    if remaining is not None and call.phase != "waiting_answer":
        parts.append(f"auto-hangup {_fmt_secs(remaining)}")
    return " \u00b7 ".join(parts)


async def _setup_logging(incident_id: str) -> None:
    log_path = incident_dir(incident_id) / "mcp.log"
    root = logging.getLogger("escalation_mcp")
    root.setLevel(logging.INFO)
    # Avoid duplicate file handlers for same path
    for h in list(root.handlers):
        if getattr(h, "baseFilename", None) == str(log_path):
            return
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    root.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers):
        sh = logging.StreamHandler()  # stderr
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(sh)


async def _teardown_media() -> None:
    """Stop capture/preview/LiveKit and free the locked preview port (8765)."""
    port = RUNTIME.settings.preview_port
    RUNTIME.stop_event.set()
    if RUNTIME.capture_task:
        try:
            await asyncio.wait_for(RUNTIME.capture_task, timeout=2.0)
        except Exception:  # noqa: BLE001
            RUNTIME.capture_task.cancel()
            try:
                await RUNTIME.capture_task
            except Exception:  # noqa: BLE001
                pass
        RUNTIME.capture_task = None

    if RUNTIME.livekit_backend:
        try:
            await RUNTIME.livekit_backend.stop()
        except Exception:  # noqa: BLE001
            logger.exception("livekit backend stop failed")
        RUNTIME.livekit_backend = None

    if RUNTIME.preview:
        try:
            await RUNTIME.preview.stop()
        except Exception:  # noqa: BLE001
            logger.exception("preview stop failed")
        RUNTIME.preview = None

    if RUNTIME.room_conn:
        try:
            await RUNTIME.room_conn.disconnect()
        except Exception:  # noqa: BLE001
            logger.exception("room disconnect failed")
        RUNTIME.room_conn = None

    RUNTIME.capture = None
    RUNTIME.stop_event = asyncio.Event()
    freed = await wait_for_port_free("127.0.0.1", port, timeout=5.0)
    if not freed:
        logger.warning(
            "preview port %s still busy after teardown; next escalate may fail to bind",
            port,
        )


async def reset_runtime(
    *,
    incident_id: str | None = None,
    reason: str = "reset",
    save_report: bool = True,
    outcome: str = "aborted",
    summary: str | None = None,
) -> dict[str, Any]:
    """Fully reset escalation state and kill preview so :8765 can be reused."""
    active = await STATE.get_active()
    iid = incident_id or (active.incident_id if active else None)

    if active and active.call is not None:
        await active.call.cancel()

    if save_report and active and iid == active.incident_id:
        report = FinalReport(
            outcome=outcome,  # type: ignore[arg-type]
            summary=summary or f"auto-reset after {reason}",
            ended_at=_utc_now_iso(),
            actions=[a.model_dump(by_alias=True) for a in active.actions],
        )
        try:
            await RUNTIME.sink.send_report(iid, report)
        except Exception:  # noqa: BLE001
            logger.exception("reset report failed")

    try:
        await RUNTIME.channel.stop()
    except Exception:  # noqa: BLE001
        logger.exception("channel stop failed during reset")

    await _teardown_media()

    if iid:
        await STATE.clear_active(iid)
    elif active:
        await STATE.clear_active(active.incident_id)

    # Fresh mock channel for the next escalation in this MCP process
    RUNTIME.channel = MockChannel()

    if iid:
        append_agent_log(
            iid,
            {"ts": _utc_now_iso(), "tool": "reset_runtime", "reason": reason},
        )
    logger.info("runtime reset (%s); preview port %s freed", reason, RUNTIME.settings.preview_port)
    return {
        "ok": True,
        "reset": True,
        "incident_id": iid,
        "preview_port": RUNTIME.settings.preview_port,
        "reason": reason,
    }


async def poll_human_response(incident_id: str, wait_seconds: int = 0) -> dict[str, Any]:
    """get_human_response. Decisions no longer tear the call down: the monitor stays
    live until the human hangs up, leaves, or the idle timer fires."""
    return await STATE.get_human_response(incident_id, wait_seconds)


def _call_timings() -> CallTimings:
    st = RUNTIME.settings
    return CallTimings(
        no_answer_s=st.call_no_answer_s,
        idle_s=st.call_idle_timeout_s,
        done_grace_s=st.call_done_grace_s,
        leave_grace_s=st.call_leave_grace_s,
        max_attempts=max(1, st.call_max_attempts),
    )


async def _publish_call_state(incident: ActiveIncident, call: CallController) -> None:
    event = CallStateEvent(
        incident_id=incident.incident_id,
        ts=_utc_now_iso(),
        state=call.phase,
        reason=call.end_reason,
        attempt=call.attempt,
        ends_in_s=call.ends_in_s(),
        default_action=incident.default_action,
    )
    try:
        await RUNTIME.channel.publish_raw("agent_events", event.model_dump())
    except Exception:  # noqa: BLE001
        logger.exception("call_state publish failed")
    append_agent_log(
        incident.incident_id,
        {"ts": event.ts, "call_state": call.phase, "reason": call.end_reason,
         "attempt": call.attempt},
    )


async def _on_call_end(incident: ActiveIncident, reason: EndReason) -> None:
    """Phone side is over. Hang up, drop media; keep the incident for polling
    unless the agent already finished (then finalize everything)."""
    warnings = await RUNTIME.sink.end_call(incident.incident_id)
    incident.warnings.extend(warnings)
    if incident.agent_outcome is not None:
        outcome, summary = incident.agent_outcome
        await reset_runtime(
            incident_id=incident.incident_id,
            reason=f"call_ended:{reason}",
            save_report=True,
            outcome=outcome,
            summary=summary,
        )
        return
    try:
        await RUNTIME.channel.stop()
    except Exception:  # noqa: BLE001
        logger.exception("channel stop failed after call end")
    await _teardown_media()


async def start_escalation(
    *,
    title: str,
    summary: str,
    failure_type: str,
    severity: str,
    details: dict[str, Any] | None = None,
    proposed_fix: str | None = None,
    run_id: str | None = None,
    share: str = "screen",
    incident_id: str | None = None,
    default_action: str | None = None,
) -> dict[str, Any]:
    existing = await STATE.get_active()
    if existing and existing.call is not None and existing.call.ended:
        # Previous call is over (agent never called end_escalation): close it out.
        await reset_runtime(
            incident_id=existing.incident_id,
            reason="superseded_after_call_end",
            save_report=True,
            outcome="handed_off",
            summary="call ended; superseded by a new escalation",
        )
        existing = None
    capture_alive = bool(
        RUNTIME.capture
        and RUNTIME.capture_task
        and not RUNTIME.capture_task.done()
        and RUNTIME.capture._running  # noqa: SLF001
    )
    if existing and incident_id and existing.incident_id != incident_id:
        logger.warning(
            "replacing active escalation %s with requested %s",
            existing.incident_id,
            incident_id,
        )
        try:
            await RUNTIME.channel.stop()
        except Exception:  # noqa: BLE001
            pass
        await _teardown_media()
        await STATE.clear_active(existing.incident_id)
        existing = None
    elif existing and not capture_alive and share != "none":
        logger.warning(
            "active escalation %s has dead capture; restarting media",
            existing.incident_id,
        )
        try:
            await RUNTIME.channel.stop()
        except Exception:  # noqa: BLE001
            pass
        await _teardown_media()
        await STATE.clear_active(existing.incident_id)
        existing = None
    elif existing:
        return {
            "incident_id": existing.incident_id,
            "status": "already_active",
            "room": existing.room,
            "preview_url": existing.preview_url,
            "sink": RUNTIME.sink.name,
            "warnings": existing.warnings
            + ["escalation already active; returning existing"],
        }

    settings = RUNTIME.settings
    if settings.mode == "live":
        # Validate live requirements now
        load_settings(require_live=True)

    # Always reclaim locked preview port before a new share
    if RUNTIME.preview is not None or RUNTIME.capture is not None:
        await _teardown_media()
    else:
        await wait_for_port_free("127.0.0.1", settings.preview_port, timeout=2.0)

    incident_id = incident_id or new_incident_id()
    await _setup_logging(incident_id)
    room = f"esc-{incident_id}"
    warnings: list[str] = []

    preview_url = ""
    RUNTIME.stop_event = asyncio.Event()
    RUNTIME.sharing = share != "none"

    capture: CaptureLoop | None = None
    if RUNTIME.sharing:
        view = share
        frame_source, view_warnings = make_frame_source(
            view,
            monitor=settings.screen_monitor,
            max_width=settings.screen_max_width,
            fake=RUNTIME.fake_frames,
        )
        if RUNTIME.fake_frames is not None:
            frame_source = RUNTIME.fake_frames
        warnings.extend(view_warnings)

        capture = CaptureLoop(frame_source, _overlay_fn, fps=settings.screen_fps)
        RUNTIME.capture = capture

        preview = PreviewBackend(host="127.0.0.1", port=settings.preview_port)
        try:
            capture.capture_once()
            await preview.start(capture)
            RUNTIME.preview = preview
            preview_url = preview.preview_url  # always http://127.0.0.1:8765
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"preview failed on locked port {settings.preview_port}: {exc}")
            logger.exception("preview start failed")

        RUNTIME.capture_task = asyncio.create_task(
            capture.run(RUNTIME.stop_event), name="capture-loop"
        )
    else:
        RUNTIME.capture = None

    # LiveKit room + channel
    channel: Channel
    room_conn = None
    if settings.mode == "live" and RUNTIME.sharing:
        from escalation_mcp.channel.livekit import LiveKitChannel
        from escalation_mcp.livekit_room import LiveKitRoomConnection, SCREEN_IDENTITY
        from escalation_mcp.screen.backends.livekit import LiveKitScreenBackend

        try:
            room_conn = LiveKitRoomConnection(settings)
            await room_conn.connect(room, identity=SCREEN_IDENTITY)
            RUNTIME.room_conn = room_conn
            lk_backend = LiveKitScreenBackend(room_conn)
            await lk_backend.start(capture)
            RUNTIME.livekit_backend = lk_backend
            channel = LiveKitChannel(room_conn)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"LiveKit failed, continuing with preview/mock channel: {exc}")
            logger.exception("LiveKit setup failed")
            channel = MockChannel()
    else:
        channel = RUNTIME.channel if isinstance(RUNTIME.channel, MockChannel) else MockChannel()
        RUNTIME.channel = channel

    RUNTIME.channel = channel

    packet = build_packet(
        incident_id=incident_id,
        title=title,
        summary=summary,
        failure_type=failure_type,
        severity=severity,
        details=details,
        proposed_fix=proposed_fix,
        run_id=run_id,
        settings=settings,
        room=room,
        preview_url=preview_url,
    )
    packet.default_action = (default_action or "").strip() or DEFAULT_SAFE_ACTION
    save_packet(packet)

    incident = ActiveIncident(
        incident_id=incident_id,
        title=title,
        severity=severity,
        packet=packet,
        room=room,
        preview_url=preview_url,
        share=share,
        warnings=warnings,
        default_action=packet.default_action,
    )
    await STATE.set_active(incident)

    call: CallController | None = None
    if RUNTIME.sink.places_call(packet):

        async def on_state(c: CallController) -> None:
            await _publish_call_state(incident, c)

        async def on_redial(attempt: int) -> None:
            incident.warnings.extend(await RUNTIME.sink.redial(packet, attempt))

        async def on_end(reason: EndReason) -> None:
            await _on_call_end(incident, reason)

        call = CallController(
            incident_id, _call_timings(),
            on_state=on_state, on_redial=on_redial, on_end=on_end,
        )
        incident.call = call

    async def on_decision(decision: HumanDecision) -> None:
        await STATE.enqueue_decision(decision)
        if call is not None:
            await call.human_activity(decision.decision)

    async def on_presence(event: str) -> None:
        if call is None:
            return
        if event == "joined":
            await call.caller_joined()
        elif event == "left":
            await call.caller_left()

    await channel.start(incident_id, on_decision, on_presence)

    # Place the call first (Chert may queue it for minutes), then start the
    # no-answer timer. If placing failed, the timer's redial is the retry.
    sink_warnings = await RUNTIME.sink.send_packet(packet)
    warnings.extend(sink_warnings)
    incident.warnings = warnings
    if call is not None:
        await call.start()

    append_agent_log(
        incident_id,
        {
            "ts": _utc_now_iso(),
            "tool": "escalate_failure",
            "title": title,
            "severity": severity,
            "share": share,
            "default_action": packet.default_action,
        },
    )

    return {
        "incident_id": incident_id,
        "status": "active",
        "room": room,
        "preview_url": preview_url,
        "sink": RUNTIME.sink.name,
        "default_action": packet.default_action,
        "call_state": call.phase if call else None,
        "warnings": warnings,
    }


async def report_action(
    incident_id: str,
    action: str,
    detail: str = "",
    status: str = "started",
) -> dict[str, Any]:
    incident = STATE.require(incident_id)
    seq = STATE.next_seq(incident)
    event = ActionEvent(
        incident_id=incident_id,
        seq=seq,
        ts=_utc_now_iso(),
        action=action,
        detail=detail,
        status=status,  # type: ignore[arg-type]
    )
    STATE.record_action(incident, event)

    # Non-blocking publish
    async def _publish() -> None:
        try:
            await RUNTIME.channel.publish_action(event)
            if RUNTIME.settings.escalation_http_events:
                await RUNTIME.sink.send_event(
                    incident_id, event.model_dump(by_alias=True)
                )
        except Exception:  # noqa: BLE001
            logger.exception("publish action failed")

    asyncio.create_task(_publish())
    append_agent_log(
        incident_id,
        {"ts": event.ts, "tool": "report_action", "seq": seq, "action": action, "status": status},
    )
    return {"ok": True, "seq": seq}


async def set_screen_view(incident_id: str, view: str) -> dict[str, Any]:
    incident = STATE.require(incident_id)
    warnings: list[str] = []
    if RUNTIME.capture is None:
        return {"ok": False, "view": view, "warnings": ["no capture loop"]}

    frame_source, view_warnings = make_frame_source(
        view,
        monitor=RUNTIME.settings.screen_monitor,
        max_width=RUNTIME.settings.screen_max_width,
        fake=RUNTIME.fake_frames,
    )
    warnings.extend(view_warnings)
    if view_warnings and view.startswith("window:"):
        # keep current view on unknown window — make_frame_source already fell back
        pass
    if RUNTIME.fake_frames is not None:
        frame_source = RUNTIME.fake_frames
    RUNTIME.capture.frame_source = frame_source
    incident.share = view
    append_agent_log(
        incident_id,
        {"ts": _utc_now_iso(), "tool": "set_screen_view", "view": view},
    )
    result: dict[str, Any] = {"ok": True, "view": view}
    if warnings:
        result["warnings"] = warnings
    return result


async def end_escalation(
    incident_id: str,
    outcome: str,
    summary: str,
    hang_up_now: bool = False,
) -> dict[str, Any]:
    """Agent is done. If the human is still on the call, keep the monitor up for a
    short grace (CALL_DONE_GRACE_S) so they can see the result; finalize when it ends."""
    active = await STATE.get_active()
    if not active or active.incident_id != incident_id:
        await reset_runtime(
            incident_id=incident_id,
            reason="end_escalation_idempotent",
            save_report=False,
        )
        return {"ok": True, "already_reset": True}

    incident = active

    seq = STATE.next_seq(incident)
    event = ActionEvent(
        incident_id=incident_id,
        seq=seq,
        ts=_utc_now_iso(),
        action="end_escalation",
        detail=f"{outcome}: {summary}",
        status="done",
    )
    STATE.record_action(incident, event)
    try:
        await RUNTIME.channel.publish_action(event)
    except Exception:  # noqa: BLE001
        logger.exception("final event publish failed")
    append_agent_log(
        incident_id,
        {"ts": _utc_now_iso(), "tool": "end_escalation", "outcome": outcome,
         "hang_up_now": hang_up_now},
    )

    call = incident.call
    if call is not None and not call.ended:
        incident.agent_outcome = (outcome, summary)
        if hang_up_now:
            await call.end("agent_done")
        else:
            await call.agent_done()
        if not call.ended:
            return {
                "ok": True,
                "call_state": call.phase,
                "hangs_up_in_s": call.ends_in_s(),
                "note": "human can keep watching; escalation finalizes when the call ends",
            }
        return {"ok": True, "call_state": "ended", "end_reason": call.end_reason}

    reset = await reset_runtime(
        incident_id=incident_id,
        reason=f"end_escalation:{outcome}",
        save_report=True,
        outcome=outcome,
        summary=summary,
    )
    return {"ok": True, **reset}
