"""Escalation packet sinks (mock file store or HTTP to Chert)."""

from escalation_mcp.sink.base import Sink
from escalation_mcp.sink.http import HttpSink
from escalation_mcp.sink.mock import MockSink

__all__ = ["Sink", "MockSink", "HttpSink"]
