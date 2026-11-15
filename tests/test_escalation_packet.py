"""Redaction + size caps for escalation packets."""

from __future__ import annotations

import json
import os

import pytest

from escalation_mcp.config import Settings
from escalation_mcp.packet import (
    MAX_LOG_TAIL_LINES,
    MAX_PACKET_BYTES,
    MAX_STRING_BYTES,
    TRUNCATION_MARKER,
    build_packet,
    redact_string,
    redact_value,
)


def test_redact_env_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_API_KEY", "super-secret-value-xyz")
    monkeypatch.setenv("NORMAL_VAR", "keep-me")
    text = "token=super-secret-value-xyz and keep-me"
    out = redact_string(text)
    assert "super-secret-value-xyz" not in out
    assert "[REDACTED]" in out
    assert "keep-me" in out


def test_redact_common_patterns() -> None:
    samples = [
        "sk-abcdefghijklmnop",
        "xai-abcdefghijklmnop",
        "AIzaSyA-test-key-123456",
        "ghp_abcdefghijklmnopqrstuv",
        "Bearer abcdefghijklmnop",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjMifQ.sig",
    ]
    for s in samples:
        assert "[REDACTED]" in redact_string(s)
        assert s.split("-")[0] if "-" in s else True


def test_log_tail_capped() -> None:
    settings = Settings(mode="mock")
    details = {"log_tail": [f"line-{i}" for i in range(500)]}
    packet = build_packet(
        incident_id="inc_test_0001",
        title="t",
        summary="s",
        failure_type="nan",
        severity="HIGH",
        details=details,
        proposed_fix=None,
        run_id=None,
        settings=settings,
        room="esc-inc_test_0001",
        preview_url="",
    )
    assert len(packet.details["log_tail"]) == MAX_LOG_TAIL_LINES


def test_string_cap() -> None:
    settings = Settings(mode="mock")
    huge = "A" * (MAX_STRING_BYTES * 2)
    packet = build_packet(
        incident_id="inc_test_0002",
        title="t",
        summary=huge,
        failure_type="other",
        severity="LOW",
        details={"traceback": huge},
        proposed_fix=None,
        run_id=None,
        settings=settings,
        room="esc-x",
        preview_url="",
    )
    assert len(packet.summary.encode("utf-8")) <= MAX_STRING_BYTES + 16
    assert TRUNCATION_MARKER in packet.summary or len(packet.summary) < len(huge)


def test_packet_size_cap() -> None:
    settings = Settings(mode="mock")
    details = {f"k{i}": ("Z" * 4000) for i in range(100)}
    packet = build_packet(
        incident_id="inc_test_0003",
        title="t",
        summary="s",
        failure_type="other",
        severity="LOW",
        details=details,
        proposed_fix=None,
        run_id=None,
        settings=settings,
        room="esc-x",
        preview_url="",
    )
    raw = json.dumps(packet.model_dump(), ensure_ascii=False).encode("utf-8")
    assert len(raw) <= MAX_PACKET_BYTES


def test_no_secrets_in_packet_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEKIT_API_SECRET", "lksec-should-not-leak")
    settings = Settings(mode="mock", livekit_api_secret="lksec-should-not-leak")
    packet = build_packet(
        incident_id="inc_test_0004",
        title="secret in details",
        summary="see details",
        failure_type="other",
        severity="LOW",
        details={"traceback": "Authorization Bearer lksec-should-not-leak"},
        proposed_fix=None,
        run_id=None,
        settings=settings,
        room="esc-x",
        preview_url="",
    )
    blob = json.dumps(packet.model_dump())
    assert "lksec-should-not-leak" not in blob
