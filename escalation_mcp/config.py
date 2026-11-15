"""Environment loading and mode validation for the escalation MCP."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

Mode = Literal["mock", "live"]

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_LOADED = False


def _ensure_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    load_dotenv(_REPO_ROOT / ".env", override=False)
    _ENV_LOADED = True


def repo_root() -> Path:
    return _REPO_ROOT


def runs_dir() -> Path:
    path = _REPO_ROOT / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Settings:
    mode: Mode = "mock"
    chert_agent_url: str = ""
    escalation_shared_secret: str = ""
    escalation_contacts: list[str] = field(default_factory=list)
    escalation_http_events: bool = False
    screen_default: str = "screen"
    screen_monitor: int = 1
    screen_fps: float = 5.0
    screen_max_width: int = 1280
    preview_port: int = 8765
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""


def load_settings(*, require_live: bool | None = None) -> Settings:
    """Load settings from the environment.

    In live mode, fail fast if required LiveKit / Chert vars are missing
    when require_live is True (default when mode is live).
    """
    _ensure_env()
    raw_mode = (os.getenv("ESCALATION_MODE") or "mock").strip().lower()
    if raw_mode not in ("mock", "live"):
        raise ValueError(f"ESCALATION_MODE must be 'mock' or 'live', got {raw_mode!r}")
    mode: Mode = raw_mode  # type: ignore[assignment]

    contacts_raw = os.getenv("ESCALATION_CONTACTS") or ""
    contacts = [c.strip() for c in contacts_raw.split(",") if c.strip()]

    settings = Settings(
        mode=mode,
        chert_agent_url=(os.getenv("CHERT_AGENT_URL") or "").rstrip("/"),
        escalation_shared_secret=os.getenv("ESCALATION_SHARED_SECRET") or "",
        escalation_contacts=contacts,
        escalation_http_events=(os.getenv("ESCALATION_HTTP_EVENTS") or "0").strip()
        in ("1", "true", "True", "yes"),
        screen_default=os.getenv("SCREEN_DEFAULT") or "screen",
        screen_monitor=int(os.getenv("SCREEN_MONITOR") or "1"),
        screen_fps=float(os.getenv("SCREEN_FPS") or "10"),
        screen_max_width=int(os.getenv("SCREEN_MAX_WIDTH") or "1280"),
        preview_port=int(os.getenv("PREVIEW_PORT") or "8765"),
        livekit_url=os.getenv("LIVEKIT_URL") or "",
        livekit_api_key=os.getenv("LIVEKIT_API_KEY") or "",
        livekit_api_secret=os.getenv("LIVEKIT_API_SECRET") or "",
    )

    check_live = require_live if require_live is not None else (mode == "live")
    if check_live:
        required = [
            ("LIVEKIT_URL", settings.livekit_url),
            ("LIVEKIT_API_KEY", settings.livekit_api_key),
            ("LIVEKIT_API_SECRET", settings.livekit_api_secret),
            # Chert call (chert/client.py + sink/chert.py)
            ("CHERT_API_KEY", os.getenv("CHERT_API_KEY")),
            ("CHERT_PROJECT_ID", os.getenv("CHERT_PROJECT_ID")),
            ("CHERT_LINE_ID", os.getenv("CHERT_LINE_ID")),
            ("USER_PHONE", os.getenv("USER_PHONE")),
        ]
        missing = [name for name, val in required if not val]
        if missing:
            raise RuntimeError(
                "live mode requires missing env vars: " + ", ".join(missing)
            )
    return settings
