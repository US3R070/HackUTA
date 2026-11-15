"""STT + Gemini LLM + ElevenLabs TTS config. Owner: Febri.

Pipeline (README): ears=STT → brain=Gemini → voice=ElevenLabs.
"""

from __future__ import annotations

import os
from pathlib import Path

from livekit.agents import AgentSession, inference
from livekit.plugins import elevenlabs, google

_PERSONA_PATH = Path(__file__).with_name("persona.md")

# Flash burns fewer free-tier credits than Multilingual v2.
_DEFAULT_TTS_MODEL = "eleven_flash_v2_5"
# Calm default; override with ELEVENLABS_VOICE_ID from the ElevenLabs voice library.
_DEFAULT_VOICE_ID = "hpp4J3VqNfWAUOO0d1Us"
# 3.8-flash is often 503 "high demand" on free tier; lite is more available.
_DEFAULT_LLM_MODEL = "gemini-3.5-flash-lite"
# Via LiveKit Inference (uses LIVEKIT_* keys). Direct GeminiSTT Live WS 404'd on free keys.
_DEFAULT_STT_MODEL = "google/gemini-3.5-transcribe-live"


def load_persona() -> str:
    return _PERSONA_PATH.read_text(encoding="utf-8").strip()


def _env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip().strip('"').strip("'")
    return value or None


def sync_env_aliases() -> None:
    """Map project .env names to what LiveKit plugins read.

    Empty values in .env (e.g. GOOGLE_API_KEY=) must not block GEMINI_API_KEY.
    Prefer a single Gemini env name to avoid google-genai dual-key warnings.
    """
    gemini = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")
    if gemini:
        os.environ["GOOGLE_API_KEY"] = gemini
        # Drop the duplicate so google-genai doesn't warn / pick the wrong one.
        os.environ.pop("GEMINI_API_KEY", None)

    eleven = _env("ELEVEN_API_KEY") or _env("ELEVENLABS_API_KEY")
    if eleven:
        os.environ["ELEVEN_API_KEY"] = eleven
        os.environ["ELEVENLABS_API_KEY"] = eleven


def require_voice_keys() -> None:
    sync_env_aliases()
    missing = [
        name
        for name, ok in (
            ("GOOGLE_API_KEY or GEMINI_API_KEY", bool(_env("GOOGLE_API_KEY"))),
            ("ELEVEN_API_KEY or ELEVENLABS_API_KEY", bool(_env("ELEVEN_API_KEY"))),
        )
        if not ok
    ]
    if missing:
        raise RuntimeError(
            "Missing API keys for the call pipeline: " + ", ".join(missing)
        )


def build_session() -> AgentSession:
    """STT (Gemini) → LLM (Gemini) → TTS (ElevenLabs), with Silero VAD for turn ends."""
    require_voice_keys()
    sync_env_aliases()

    voice_id = _env("ELEVENLABS_VOICE_ID") or _DEFAULT_VOICE_ID
    tts_model = _env("ELEVENLABS_TTS_MODEL") or _DEFAULT_TTS_MODEL
    llm_model = _env("GEMINI_LLM_MODEL") or _DEFAULT_LLM_MODEL
    stt_model = _env("GEMINI_STT_MODEL") or _DEFAULT_STT_MODEL
    gemini_key = _env("GOOGLE_API_KEY") or _env("GEMINI_API_KEY")

    return AgentSession(
        # LiveKit Inference STT (billed on LiveKit Cloud; no GCP Speech creds).
        stt=inference.STT(model=stt_model, language="en"),
        llm=google.LLM(model=llm_model, api_key=gemini_key),
        tts=elevenlabs.TTS(voice_id=voice_id, model=tts_model),
        vad=inference.VAD(model="silero"),
    )


def greeting_instructions() -> str:
    return (
        "Greet the caller briefly as Mentor from the Odyssey. "
        "Say you are here because a long-running process may have gone astray, "
        "and ask what they see. Keep it to two short sentences."
    )
