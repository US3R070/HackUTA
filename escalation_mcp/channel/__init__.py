"""Bidirectional event channel: agent_events up, human_decisions down."""

from escalation_mcp.channel.base import Channel
from escalation_mcp.channel.mock import MockChannel

__all__ = ["Channel", "MockChannel"]
