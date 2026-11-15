

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from escalation_mcp.channel.base import Channel
from escalation_mcp.channel.mock import MockChannel
from escalation_mcp.config import Settings, load_settings
from escalation_mcp.models import ActionEvent, FinalReport, HumanDecision
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


def _voice_agent_configured() -> bool:
    return bool(
        (os.getenv("ELEVENLABS_AGENT_ID") or os.getenv("AGENT_ID") or "").strip()
    )


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
    # After a human decision is delivered, runtime auto-resets (preview killed).
    # Voice bridge keeps the FaceTime up so Grok can keep acting — disable then.
    auto_reset_on_decision: bool = True
    voice_bridge: Any | None = None
    voice_task: asyncio.Task[None] | None = None


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
    )


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

    if RUNTIME.voice_task:
        RUNTIME.voice_task.cancel()
        try:
            await RUNTIME.voice_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("voice task cancel failed")
        RUNTIME.voice_task = None

    if RUNTIME.voice_bridge:
        try:
            await RUNTIME.voice_bridge.stop()
        except Exception:  # noqa: BLE001
            logger.exception("voice bridge stop failed")
        RUNTIME.voice_bridge = None

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
    """get_human_response + auto-reset preview/state when a real decision arrives."""
    result = await STATE.get_human_response(incident_id, wait_seconds)
    if result.get("decision") == "none":
        return result

    if RUNTIME.auto_reset_on_decision:
        outcome = {
            "approve": "resolved",
            "reject": "aborted",
            "stop": "aborted",
            "instruction": "handed_off",
        }.get(str(result["decision"]), "aborted")
        reset = await reset_runtime(
            incident_id=incident_id,
            reason=f"human_decision:{result['decision']}",
            save_report=True,
            outcome=outcome,
            summary=f"human {result['decision']}: {result.get('text')}",
        )
        result = {**result, "reset": True, "preview_stopped": True, "reset_info": reset}
    return result


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
) -> dict[str, Any]:
    existing = await STATE.get_active()
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
    )
    await STATE.set_active(incident)

    async def on_decision(decision: HumanDecision) -> None:
        await STATE.enqueue_decision(decision)

    await channel.start(incident_id, on_decision)

    sink_warnings = await RUNTIME.sink.send_packet(packet)
    warnings.extend(sink_warnings)
    incident.warnings = warnings

    # FaceTime Mentor: ElevenLabs Agents over the same LiveKit participant.
    if (
        settings.mode == "live"
        and RUNTIME.sharing
        and RUNTIME.room_conn is not None
        and _voice_agent_configured()
    ):
        RUNTIME.auto_reset_on_decision = False
        RUNTIME.voice_task = asyncio.create_task(
            _start_voice_bridge(packet), name="eleven-agent-bridge"
        )
    elif _voice_agent_configured() and settings.mode != "live":
        warnings.append("ElevenLabs agent configured but ESCALATION_MODE is not live")

    append_agent_log(
        incident_id,
        {
            "ts": _utc_now_iso(),
            "tool": "escalate_failure",
            "title": title,
            "severity": severity,
            "share": share,
        },
    )

    return {
        "incident_id": incident_id,
        "status": "active",
        "room": room,
        "preview_url": preview_url,
        "sink": RUNTIME.sink.name,
        "warnings": warnings,
    }


async def _start_voice_bridge(packet: Any) -> None:
    """Wait for Chert bridge + media grace, then start ElevenLabs on this participant.

    FaceTime shows gray for ~2–3s after answer while video attaches. If Mentor
    greets immediately, the opening words are lost. Wait out that window first.
    """
    room_conn = RUNTIME.room_conn
    if room_conn is None:
        return
    try:
        await asyncio.wait_for(room_conn.bridge_joined.wait(), timeout=90.0)
    except asyncio.TimeoutError:
        logger.warning("ElevenLabs bridge: Chert bridge never joined; skipping voice")
        return

    grace = float(os.getenv("FACETIME_AUDIO_GRACE_S") or "3.5")
    logger.info("ElevenLabs bridge: waiting %.1fs for FaceTime video to attach", grace)
    await asyncio.sleep(max(0.0, grace))

    try:
        from escalation_mcp.voice.eleven_agent import ElevenAgentBridge

        bridge = ElevenAgentBridge(
            room_conn, packet, loop=asyncio.get_running_loop()
        )
        await bridge.start()
        RUNTIME.voice_bridge = bridge
    except Exception as exc:  # noqa: BLE001
        logger.exception("ElevenLabs agent bridge failed to start")
        # Surface into the active incident so Grok/operators see it.
        active = await STATE.get_active()
        if active and active.incident_id == getattr(packet, "incident_id", None):
            msg = f"ElevenLabs voice failed: {exc}"
            if msg not in active.warnings:
                active.warnings.append(msg)


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
) -> dict[str, Any]:
    active = await STATE.get_active()
    if not active or active.incident_id != incident_id:
        # Already auto-reset after a human decision — still free the port.
        await reset_runtime(
            incident_id=incident_id,
            reason="end_escalation_idempotent",
            save_report=False,
        )
        return {"ok": True, "already_reset": True}

    incident = active

    # Final event
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

    reset = await reset_runtime(
        incident_id=incident_id,
        reason=f"end_escalation:{outcome}",
        save_report=True,
        outcome=outcome,
        summary=summary,
    )
    append_agent_log(
        incident_id,
        {"ts": _utc_now_iso(), "tool": "end_escalation", "outcome": outcome},
    )
    return {"ok": True, **reset}
