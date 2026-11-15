"""One-shot ElevenLabs TTS → 48 kHz PCM for LiveKit play_pcm."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from escalation_mcp.voice.resample import resample_pcm16

logger = logging.getLogger("escalation_mcp")

EL_SR = 16_000
LK_SR = 48_000


def _env(*names: str) -> str | None:
    for name in names:
        raw = os.getenv(name)
        if raw is None:
            continue
        value = raw.strip().strip('"').strip("'")
        if value:
            return value
    return None


def resolve_voice_id() -> str:
    """Prefer ELEVENLABS_VOICE_ID; else read the agent’s configured voice."""
    voice_id = _env("ELEVENLABS_VOICE_ID")
    if voice_id:
        return voice_id

    agent_id = _env("ELEVENLABS_AGENT_ID", "AGENT_ID")
    api_key = _env("ELEVENLABS_API_KEY", "ELEVEN_API_KEY")
    if not agent_id or not api_key:
        raise RuntimeError(
            "Set ELEVENLABS_VOICE_ID in .env (copy from your ElevenLabs agent Voice)."
        )

    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/convai/agents/{agent_id}",
        headers={"xi-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        raise RuntimeError(
            f"Could not load agent voice ({e.code}). Set ELEVENLABS_VOICE_ID in .env. {body}"
        ) from None
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"Could not load agent voice: {e}. Set ELEVENLABS_VOICE_ID in .env."
        ) from e

    # conversation_config.tts.voice_id is the usual path
    tts = (
        (data.get("conversation_config") or {}).get("tts")
        or (data.get("agent") or {}).get("tts")
        or {}
    )
    voice_id = tts.get("voice_id") or data.get("voice_id")
    if not voice_id:
        raise RuntimeError(
            "Agent has no voice_id in API response. Set ELEVENLABS_VOICE_ID in .env."
        )
    logger.info("resolved Mentor voice_id from agent config: %s…", str(voice_id)[:8])
    # Cache for this process
    os.environ["ELEVENLABS_VOICE_ID"] = str(voice_id)
    return str(voice_id)


def synthesize_pcm48(text: str) -> bytes:
    """Return 48 kHz mono PCM16 bytes for the given line."""
    api_key = _env("ELEVENLABS_API_KEY", "ELEVEN_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY not set")
    voice_id = resolve_voice_id()
    model = _env("ELEVENLABS_TTS_MODEL") or "eleven_flash_v2_5"

    from elevenlabs.client import ElevenLabs

    client = ElevenLabs(api_key=api_key)
    raw = client.text_to_speech.convert(
        voice_id=voice_id,
        text=text,
        model_id=model,
        output_format="pcm_16000",
    )
    if isinstance(raw, (bytes, bytearray)):
        pcm16 = bytes(raw)
    elif hasattr(raw, "read"):
        pcm16 = raw.read()
    else:
        chunks: list[bytes] = []
        for part in raw:  # type: ignore[assignment]
            if isinstance(part, (bytes, bytearray)):
                chunks.append(bytes(part))
            elif isinstance(part, memoryview):
                chunks.append(part.tobytes())
        pcm16 = b"".join(chunks)
    if not pcm16:
        raise RuntimeError("ElevenLabs TTS returned empty audio")
    return resample_pcm16(pcm16, EL_SR, LK_SR)
