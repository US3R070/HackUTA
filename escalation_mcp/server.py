"""FastMCP stdio server exposing the five escalation tools."""

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
    default_action: str | None = None,
) -> dict[str, Any]:
    """Open an escalation: build packet, start screen share, notify cloud agent.

    Prefer share=\"window:<terminal or IDE title>\" for privacy. Use share=\"none\"
    to skip screen share. Pass incident_id to reuse a pre-minted id from a demo
    script. If an escalation is already active, returns it.

    default_action: what you will do if the human never decides (no answer after
    one redial, hangs up, or goes idle). It MUST be non-destructive, e.g. "keep the
    run paused at checkpoint 4000 and text a summary". The call agent announces it
    up front. Omit it to use the safe fallback (change nothing, send a summary).
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
        default_action=default_action,
    )


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

    decision: approve | reject | stop | instruction (with text) | hangup | none.
    The call stays live after a decision so the human can keep watching and give
    more instructions; keep polling between steps. Every response includes
    call_state (waiting_answer|live|watching|agent_done|ended) and ends_in_s.
    decision="hangup" means the call is over (end_reason: hangup | caller_left |
    idle_timeout | no_answer | agent_done). If run_default_action is true the human
    never decided: perform default_action (non-destructive), then end_escalation.
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
async def end_escalation(
    incident_id: str,
    outcome: Literal["resolved", "aborted", "handed_off"],
    summary: str,
    hang_up_now: bool = False,
) -> dict[str, Any]:
    """Agent is done. If the human is still on the call the monitor stays up for a
    short grace (CALL_DONE_GRACE_S, extended while they keep talking) so they see
    the result, then hangs up and sends the final report. hang_up_now=True skips
    the grace."""
    return await sess.end_escalation(incident_id, outcome, summary, hang_up_now)


def main() -> None:
    # Configure runtime from env before serving
    sess.configure_runtime()
    logger.info(
        "escalation MCP starting (mode=%s)", sess.RUNTIME.settings.mode
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
