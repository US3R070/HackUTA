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
) -> dict[str, Any]:
    """Open an escalation: build packet, start screen share, notify cloud agent.

    Prefer share=\"window:<terminal or IDE title>\" for privacy. Use share=\"none\"
    to skip screen share. If an escalation is already active, returns it.
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

    On decision=stop, subsequent calls keep returning stop until end_escalation.
    """
    return await STATE.get_human_response(incident_id, wait_seconds)


@mcp.tool()
async def set_screen_view(incident_id: str, view: str) -> dict[str, Any]:
    """Switch shared view: screen | window:<title> | image:<path>."""
    return await sess.set_screen_view(incident_id, view)


@mcp.tool()
async def end_escalation(
    incident_id: str,
    outcome: Literal["resolved", "aborted", "handed_off"],
    summary: str,
) -> dict[str, Any]:
    """Finish the escalation: final report, stop screen share, leave room."""
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
