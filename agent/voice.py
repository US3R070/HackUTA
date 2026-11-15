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
_DEFAULT_LLM_MODEL = "gemini-2.5-flash"
_DEFAULT_STT_MODEL = "gemini-2.5-flash"


def load_persona() -> str:
    return _PERSONA_PATH.read_text(encoding="utf-8").strip()


def sync_env_aliases() -> None:
    """Map project .env names to what LiveKit plugins read."""
    gemini = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if gemini:
        os.environ.setdefault("GOOGLE_API_KEY", gemini)
        os.environ.setdefault("GEMINI_API_KEY", gemini)

    eleven = os.getenv("ELEVEN_API_KEY") or os.getenv("ELEVENLABS_API_KEY")
    if eleven:
        os.environ.setdefault("ELEVEN_API_KEY", eleven)
        os.environ.setdefault("ELEVENLABS_API_KEY", eleven)


def require_voice_keys() -> None:
    sync_env_aliases()
    missing = [
        name
        for name, ok in (
            ("GOOGLE_API_KEY or GEMINI_API_KEY", bool(os.getenv("GOOGLE_API_KEY"))),
            ("ELEVEN_API_KEY or ELEVENLABS_API_KEY", bool(os.getenv("ELEVEN_API_KEY"))),
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

    voice_id = os.getenv("ELEVENLABS_VOICE_ID", _DEFAULT_VOICE_ID)
    tts_model = os.getenv("ELEVENLABS_TTS_MODEL", _DEFAULT_TTS_MODEL)
    llm_model = os.getenv("GEMINI_LLM_MODEL", _DEFAULT_LLM_MODEL)
    stt_model = os.getenv("GEMINI_STT_MODEL", _DEFAULT_STT_MODEL)

    return AgentSession(
        stt=google.STT(model=stt_model),
        llm=google.LLM(model=llm_model),
        tts=elevenlabs.TTS(voice_id=voice_id, model=tts_model),
        vad=inference.VAD(model="silero"),
    )


def greeting_instructions() -> str:
    return (
        "Greet the caller briefly as Mentor from the Odyssey. "
        "Say you are here because a long-running process may have gone astray, "
        "and ask what they see. Keep it to two short sentences."
    )
