"""FastMCP stdio server exposing the escalation tools."""

from __future__ import annotations

import logging
import sys
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from escalation_mcp import session as sess
from escalation_mcp.state import STATE

# stdout is reserved for MCP; keep our logs on stderr
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("escalation_mcp")

mcp = FastMCP("escalation")


@mcp.tool()
async def escalate_failure(
    title: str,
    summary: str,
    failure_type: str,
    severity: Literal["LOW", "HIGH"],
    details: dict[str, Any] | None = None,
    proposed_fix: str | None = None,
    run_id: str | None = None,
    share: str = "screen",
    incident_id: str | None = None,
    focus: str | None = None,
) -> dict[str, Any]:
    """Open an escalation: build packet, start screen share, notify cloud agent.

    Prefer share=\"window:<terminal or IDE title>\" for privacy. Use share=\"none\"
    to skip screen share. Pass incident_id to reuse a pre-minted id from a demo
    script. If an escalation is already active, returns it.

    focus: optional \"path:line\" of the line you believe caused the failure
    (e.g. \"demo/configs/bad_lr.yaml:1\"). With share=\"screen\" the call opens
    zoomed onto that line in VS Code. Put other useful spots in
    details[\"code_anchors\"] = [{\"label\": ..., \"target\": \"path:line\"}] so the
    voice agent can show them on request.
    """
    return await sess.start_escalation(
        title=title,
        summary=summary,
        failure_type=failure_type,
        severity=severity,
        details=details,
        proposed_fix=proposed_fix,
        run_id=run_id,
        share=share,
        incident_id=incident_id,
        focus=focus,
    )


@mcp.tool()
async def focus_code(incident_id: str, query: str) -> dict[str, Any]:
    """Pan/zoom the caller's screen onto one line of code in VS Code.

    query: \"path:line\" (preferred, relative to the repo) or a short text snippet
    to search for (first match wins). Use it to point at the line you believe is
    the problem, or at the line you just edited. Only works with share=\"screen\";
    if the line can't be seen (VS Code closed/covered) the camera stays on the
    full screen. Call show_full_screen to zoom back out.
    """
    return await sess.focus_code(incident_id, query, source="grok")


@mcp.tool()
async def show_full_screen(incident_id: str) -> dict[str, Any]:
    """Zoom the caller's screen camera back out to the whole screen."""
    return await sess.show_full_screen(incident_id, source="grok")


@mcp.tool()
async def report_action(
    incident_id: str,
    action: str,
    detail: str = "",
    status: Literal["started", "done", "failed"] = "started",
) -> dict[str, Any]:
    """Narrate a step to the human (overlay + agent_events). Call often; cheap."""
    return await sess.report_action(incident_id, action, detail, status)


@mcp.tool()
async def get_human_response(
    incident_id: str,
    wait_seconds: int = 0,
) -> dict[str, Any]:
    """Return the oldest unconsumed human decision. wait_seconds long-polls (max 25).

    When a real decision arrives (approve/reject/stop/instruction), the MCP
    auto-resets: stops the :8765 preview, clears active state, frees the port
    for the next escalate_failure. end_escalation stays safe/idempotent after that.
    """
    return await sess.poll_human_response(incident_id, wait_seconds)


@mcp.tool()
async def reset_escalation(incident_id: str | None = None) -> dict[str, Any]:
    """Force-stop preview on :8765 and clear escalation state (manual cleanup)."""
    return await sess.reset_runtime(
        incident_id=incident_id, reason="manual_reset", save_report=False
    )


@mcp.tool()
async def set_screen_view(incident_id: str, view: str) -> dict[str, Any]:
    """Switch shared view: screen | window:<title> | image:<path>."""
    return await sess.set_screen_view(incident_id, view)


@mcp.tool()
async def fix_done(
    incident_id: str,
    summary: str,
    applied: bool = True,
) -> dict[str, Any]:
    """Report a finished change to the caller after an approve/instruction decision.

    The live voice agent is waiting on this: it explains `summary` to the caller.
    Write 1-2 plain sentences: what you changed (file + old -> new value), why
    that value, and what the caller should see next. applied=False if you
    refused or could not make the change (say why in summary).
    """
    return await sess.fix_done(incident_id, summary, applied)


@mcp.tool()
async def speak_to_caller(incident_id: str, text: str) -> dict[str, Any]:
    """Speak one line on the call via plain TTS. ONLY when no voice agent is live.

    With the FaceTime voice agent running, do not use this; use fix_done and let
    the agent talk. Returns duration_s.
    """
    return await sess.speak_to_caller(incident_id, text)


@mcp.tool()
async def end_escalation(
    incident_id: str,
    outcome: Literal["resolved", "aborted", "handed_off"],
    summary: str,
) -> dict[str, Any]:
    """Finish the escalation: final report, stop screen share, leave room.

    With the voice agent live, call this after get_human_response returns
    decision="hangup" (the agent already said goodbye and ended the call).
    """
    return await sess.end_escalation(incident_id, outcome, summary)


def _exit_when_orphaned() -> None:
    """Exit if the parent (Grok) dies; otherwise we linger holding port 8765."""
    import os
    import threading
    import time

    parent = os.getppid()

    def _watch() -> None:
        while True:
            time.sleep(2.0)
            # Reparented (to launchd/init = PID 1) means the client is gone.
            if os.getppid() != parent or os.getppid() == 1:
                logger.warning("parent process %s exited; escalation MCP shutting down", parent)
                os._exit(0)

    threading.Thread(target=_watch, name="orphan-watchdog", daemon=True).start()


def main() -> None:
    _exit_when_orphaned()
    # Configure runtime from env before serving
    sess.configure_runtime()
    logger.info(
        "escalation MCP starting (mode=%s)", sess.RUNTIME.settings.mode
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
