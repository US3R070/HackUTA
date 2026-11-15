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
) -> dict[str, Any]:
    """Open an escalation: build packet, start screen share, notify cloud agent.

    Prefer share=\"window:<terminal or IDE title>\" for privacy. Use share=\"none\"
    to skip screen share. Pass incident_id to reuse a pre-minted id from a demo
    script. If an escalation is already active, returns it.
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
async def speak_to_caller(incident_id: str, text: str) -> dict[str, Any]:
    """Speak a short line on the live FaceTime call (ElevenLabs TTS).

    Use after applying a fix ("There we go, I fixed it…") and again for goodbye
    before end_escalation. Returns duration_s — wait that long before hanging up.
    """
    return await sess.speak_to_caller(incident_id, text)


@mcp.tool()
async def end_escalation(
    incident_id: str,
    outcome: Literal["resolved", "aborted", "handed_off"],
    summary: str,
) -> dict[str, Any]:
    """Finish the escalation: final report, stop screen share, leave room.

    Speak goodbye with speak_to_caller first and wait duration_s before calling this.
    """
    return await sess.end_escalation(incident_id, outcome, summary)


def main() -> None:
    # Configure runtime from env before serving
    sess.configure_runtime()
    logger.info(
        "escalation MCP starting (mode=%s)", sess.RUNTIME.settings.mode
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
