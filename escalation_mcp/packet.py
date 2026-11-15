"""Build, redact, size-cap, and persist escalation packets."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from escalation_mcp.config import Settings, runs_dir
from escalation_mcp.models import EscalationPacket, MediaInfo

MAX_LOG_TAIL_LINES = 200
MAX_STRING_BYTES = 8 * 1024
MAX_PACKET_BYTES = 256 * 1024
TRUNCATION_MARKER = "…[truncated]"

_SECRET_NAME_RE = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD)", re.IGNORECASE)
_SECRET_VALUE_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bxai-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*\b", re.IGNORECASE),
    re.compile(
        r"\beyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\b"
    ),  # JWT-looking
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?"
        r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_incident_id(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    stamp = dt.strftime("%Y%m%d_%H%M%S")
    suffix = f"{os.urandom(2).hex()}"
    return f"inc_{stamp}_{suffix}"


def incident_dir(incident_id: str) -> Path:
    path = runs_dir() / incident_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _secret_env_values() -> set[str]:
    values: set[str] = set()
    for name, value in os.environ.items():
        if not value or not _SECRET_NAME_RE.search(name):
            continue
        if len(value) >= 4:
            values.add(value)
    return values


def redact_string(text: str, secret_values: set[str] | None = None) -> str:
    if not text:
        return text
    secrets = secret_values if secret_values is not None else _secret_env_values()
    out = text
    for secret in sorted(secrets, key=len, reverse=True):
        if secret and secret in out:
            out = out.replace(secret, "[REDACTED]")
    for pattern in _SECRET_VALUE_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


def _cap_string(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_STRING_BYTES:
        return text
    # Truncate on byte boundary
    truncated = encoded[: MAX_STRING_BYTES - len(TRUNCATION_MARKER.encode("utf-8"))]
    return truncated.decode("utf-8", errors="ignore") + TRUNCATION_MARKER


def redact_value(value: Any, secret_values: set[str] | None = None) -> Any:
    secrets = secret_values if secret_values is not None else _secret_env_values()
    if isinstance(value, str):
        return _cap_string(redact_string(value, secrets))
    if isinstance(value, list):
        return [redact_value(v, secrets) for v in value]
    if isinstance(value, dict):
        return {str(k): redact_value(v, secrets) for k, v in value.items()}
    return value


def _normalize_details(details: dict[str, Any] | None) -> dict[str, Any]:
    if not details:
        return {}
    out = dict(details)
    log_tail = out.get("log_tail")
    if isinstance(log_tail, list) and len(log_tail) > MAX_LOG_TAIL_LINES:
        out["log_tail"] = log_tail[-MAX_LOG_TAIL_LINES:]
    elif isinstance(log_tail, str):
        lines = log_tail.splitlines()
        if len(lines) > MAX_LOG_TAIL_LINES:
            out["log_tail"] = lines[-MAX_LOG_TAIL_LINES:]
    return out


def _packet_bytes(data: dict) -> int:
    return len(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def enforce_packet_size(packet: EscalationPacket) -> EscalationPacket:
    data = packet.model_dump(by_alias=True)
    if _packet_bytes(data) <= MAX_PACKET_BYTES:
        return packet

    # Cap every details value first
    details = dict(data.get("details") or {})
    for key in list(details.keys()):
        details[key] = TRUNCATION_MARKER
    data["details"] = details
    if _packet_bytes(data) <= MAX_PACKET_BYTES:
        return EscalationPacket.model_validate(data)

    # Drop details keys until under budget
    while details and _packet_bytes(data) > MAX_PACKET_BYTES:
        details.pop(next(iter(details)))
        data["details"] = details

    if _packet_bytes(data) <= MAX_PACKET_BYTES:
        return EscalationPacket.model_validate(data)

    # Last resort: strip details entirely and truncate summary/title
    data["details"] = {"note": TRUNCATION_MARKER}
    data["summary"] = _cap_string(str(data.get("summary", "")))
    data["title"] = _cap_string(str(data.get("title", "")))
    if _packet_bytes(data) > MAX_PACKET_BYTES:
        data["summary"] = TRUNCATION_MARKER
        data["title"] = TRUNCATION_MARKER
    return EscalationPacket.model_validate(data)


def build_packet(
    *,
    incident_id: str,
    title: str,
    summary: str,
    failure_type: str,
    severity: str,
    details: dict[str, Any] | None,
    proposed_fix: str | None,
    run_id: str | None,
    settings: Settings,
    room: str,
    preview_url: str,
) -> EscalationPacket:
    secrets = _secret_env_values()
    normalized = _normalize_details(details)
    redacted_details = redact_value(normalized, secrets)
    assert isinstance(redacted_details, dict)

    packet = EscalationPacket(
        incident_id=incident_id,
        created_at=_utc_now_iso(),
        severity=severity,  # type: ignore[arg-type]
        failure_type=failure_type,
        title=_cap_string(redact_string(title, secrets)),
        summary=_cap_string(redact_string(summary, secrets)),
        proposed_fix=(
            _cap_string(redact_string(proposed_fix, secrets))
            if proposed_fix
            else None
        ),
        run_id=run_id,
        details=redacted_details,
        media=MediaInfo(
            transport="livekit" if settings.mode == "live" else "preview",
            livekit_url=settings.livekit_url if settings.mode == "live" else "",
            room=room,
            screen_participant="local-screen",
            preview_url=preview_url,
        ),
        contacts=list(settings.escalation_contacts),
    )
    return enforce_packet_size(packet)


def save_packet(packet: EscalationPacket) -> Path:
    path = incident_dir(packet.incident_id) / "packet.json"
    path.write_text(
        json.dumps(packet.model_dump(by_alias=True), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def append_agent_log(incident_id: str, record: dict[str, Any]) -> None:
    path = incident_dir(incident_id) / "agent_log.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
