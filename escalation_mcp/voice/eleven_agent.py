"""ElevenLabs Agents (ConvAI) bridge into the existing LiveKit agent participant.

Phone audio → ElevenLabs (16 kHz) → Gemini conversation + TTS → play_pcm (48 kHz).
Client tool send_to_grok → HumanDecision → Allen's decision queue for Grok MCP.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Optional

from escalation_mcp.models import EscalationPacket, HumanDecision
from escalation_mcp.state import STATE
from escalation_mcp.voice.resample import resample_pcm16

if TYPE_CHECKING:
    from escalation_mcp.livekit_room import LiveKitRoomConnection

logger = logging.getLogger("escalation_mcp")

EL_SR = 16_000
LK_SR = 48_000
# ElevenLabs recommends ~250 ms chunks at 16 kHz mono PCM16.
EL_CHUNK_SAMPLES = 4_000
EL_CHUNK_BYTES = EL_CHUNK_SAMPLES * 2

_VALID = frozenset({"approve", "reject", "stop", "instruction"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env(*names: str) -> str | None:
    for name in names:
        raw = os.getenv(name)
        if raw is None:
            continue
        value = raw.strip().strip('"').strip("'")
        if value:
            return value
    return None


def incident_context(packet: EscalationPacket) -> str:
    details = packet.details or {}
    log_tail = details.get("log_tail") or []
    if isinstance(log_tail, list):
        tail = "\n".join(str(x) for x in log_tail[-12:])
    else:
        tail = str(log_tail)[:800]
    tb = str(details.get("traceback") or "")[:600]
    cfg = details.get("config")
    return (
        f"incident_id: {packet.incident_id}\n"
        f"title: {packet.title}\n"
        f"failure_type: {packet.failure_type}\n"
        f"severity: {packet.severity}\n"
        f"summary: {packet.summary}\n"
        f"proposed_fix: {packet.proposed_fix or '(none)'}\n"
        f"run_id: {packet.run_id or '(none)'}\n"
        f"config: {cfg}\n"
        f"log_tail:\n{tail}\n"
        f"traceback:\n{tb}\n"
    )


def mentor_prompt_with_context(packet: EscalationPacket) -> str:
    return (
        "You are Mentor on a FaceTime call about a failed ML training run.\n"
        "Be calm, brief, and concrete. Explain the problem in one or two sentences, "
        "ask if they want the proposed fix, and wait for a clear yes/no or instruction.\n"
        "When the human approves, rejects, asks to stop, or gives an instruction, "
        "call the send_to_grok tool. Do not claim you edited code yourself.\n"
        "After send_to_grok succeeds: stay completely silent. Do not check if they are "
        "still there, do not ask if they need anything else, do not say goodbye.\n\n"
        "INCIDENT CONTEXT:\n"
        f"{incident_context(packet)}"
    )


def first_message_for(packet: EscalationPacket) -> str:
    fix = packet.proposed_fix or "a safe fix"
    summary = (packet.summary or packet.title or "a training failure").strip()
    if len(summary) > 180:
        summary = summary[:177] + "…"
    return (
        f"Hey — {summary} "
        f"I can apply this: {fix}. Want me to go ahead?"
    )


class LiveKitAudioInterface:
    """AudioInterface adapter: LiveKit room mic ↔ ElevenLabs 16 kHz PCM."""

    def __init__(self, room_conn: "LiveKitRoomConnection") -> None:
        self._room = room_conn
        self._input_callback: Optional[Callable[[bytes], None]] = None
        self._running = False
        self._in_buf = bytearray()
        self._lock = threading.Lock()
        self._out_hz = EL_SR
        self._silence_thread: threading.Thread | None = None
        self._bytes_out = 0
        self._mute_agent_out = False

    def set_output_sample_rate(self, hz: int) -> None:
        if hz > 0:
            self._out_hz = hz

    def mute_agent_output(self) -> None:
        """Drop Mentor TTS chunks (used while we play our own filler)."""
        self._mute_agent_out = True
        self._room.clear_audio_queue()

    def start(self, input_callback: Callable[[bytes], None]) -> None:
        self._input_callback = input_callback
        self._running = True
        self._mute_agent_out = False
        self._in_buf.clear()
        self._bytes_out = 0
        # Keep a silent uplink alive until FaceTime audio is subscribed.
        self._silence_thread = threading.Thread(
            target=self._feed_silence, name="el-silence-uplink", daemon=True
        )
        self._silence_thread.start()

    def stop(self) -> None:
        self._running = False
        self._mute_agent_out = True
        self._input_callback = None
        with self._lock:
            self._in_buf.clear()
        t = self._silence_thread
        self._silence_thread = None
        if t and t.is_alive():
            t.join(timeout=1.0)

    def output(self, audio: bytes) -> None:
        if not audio or not self._running or self._mute_agent_out:
            return
        self._bytes_out += len(audio)
        if self._bytes_out == len(audio):
            logger.info(
                "ElevenLabs audio out: first chunk %d bytes (assuming %d Hz)",
                len(audio),
                self._out_hz,
            )
        elif self._bytes_out % 32000 < len(audio):
            logger.info(
                "ElevenLabs audio out: +%d bytes (total %d)",
                len(audio),
                self._bytes_out,
            )
        pcm48 = resample_pcm16(audio, self._out_hz, LK_SR)
        try:
            self._room.play_pcm(pcm48)
        except Exception:  # noqa: BLE001
            logger.exception("play_pcm from ElevenLabs failed")

    def interrupt(self) -> None:
        self._room.clear_audio_queue()

    def push_user_pcm16_16k(self, pcm16k: bytes) -> None:
        """Feed phone/user audio (already 16 kHz mono PCM16) into ElevenLabs."""
        if not self._running or not self._input_callback or not pcm16k:
            return
        with self._lock:
            self._in_buf.extend(pcm16k)
            while len(self._in_buf) >= EL_CHUNK_BYTES:
                chunk = bytes(self._in_buf[:EL_CHUNK_BYTES])
                del self._in_buf[:EL_CHUNK_BYTES]
                cb = self._input_callback
                if cb is None:
                    return
                try:
                    cb(chunk)
                except Exception:  # noqa: BLE001
                    logger.exception("ElevenLabs input_callback failed")

    def _feed_silence(self) -> None:
        silence = b"\x00\x00" * EL_CHUNK_SAMPLES
        while self._running:
            self.push_user_pcm16_16k(silence)
            threading.Event().wait(EL_CHUNK_SAMPLES / float(EL_SR))


class ElevenAgentBridge:
    """Owns one ElevenLabs Conversation tied to a live escalation."""

    def __init__(
        self,
        room_conn: "LiveKitRoomConnection",
        packet: EscalationPacket,
        *,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._room = room_conn
        self._packet = packet
        self._loop = loop
        self._audio = LiveKitAudioInterface(room_conn)
        self._conversation: Any = None
        self._started = False

    @property
    def audio(self) -> LiveKitAudioInterface:
        return self._audio

    async def start(self) -> None:
        if self._started:
            return
        agent_id = _env("ELEVENLABS_AGENT_ID", "AGENT_ID")
        api_key = _env("ELEVENLABS_API_KEY", "ELEVEN_API_KEY")
        if not agent_id:
            raise RuntimeError("ELEVENLABS_AGENT_ID is not set")
        if not api_key:
            raise RuntimeError("ELEVENLABS_API_KEY / ELEVEN_API_KEY is not set")

        from elevenlabs.client import ElevenLabs
        from elevenlabs.conversational_ai.conversation import (
            ClientTools,
            Conversation,
            ConversationInitiationData,
        )

        client_tools = ClientTools(loop=self._loop)
        client_tools.register("send_to_grok", self._send_to_grok, is_async=True)

        # Free plan: first_message / prompt overrides are unavailable. Leave
        # conversation_config_override empty and set the greeting in the
        # ElevenLabs dashboard. Incident details go via contextual update.
        config = ConversationInitiationData(
            conversation_config_override={},
            dynamic_variables={
                "incident_id": self._packet.incident_id,
                "summary": self._packet.summary or "",
                "proposed_fix": self._packet.proposed_fix or "",
                "failure_type": self._packet.failure_type,
                "severity": self._packet.severity,
            },
        )

        def _on_disconnect(details: Any) -> None:
            logger.warning("ElevenLabs disconnected: %s", details)

        self._conversation = Conversation(
            ElevenLabs(api_key=api_key),
            agent_id,
            requires_auth=True,
            audio_interface=self._audio,
            config=config,
            client_tools=client_tools,
            callback_agent_response=lambda t: logger.info("mentor: %s", t),
            callback_user_transcript=lambda t: logger.info("caller: %s", t),
            callback_disconnect=_on_disconnect,
        )
        # SDK runs the websocket on a background thread.
        await asyncio.to_thread(self._conversation.start_session)
        self._started = True
        self._room.set_user_audio_sink(self._audio.push_user_pcm16_16k)

        # Incident details without requiring prompt-override permission.
        context = incident_context(self._packet)
        try:
            await asyncio.sleep(0.8)
            await asyncio.to_thread(
                self._conversation.send_contextual_update,
                f"Incident context for this call:\n{context}",
            )
        except Exception:  # noqa: BLE001
            logger.exception("send_contextual_update failed")

        logger.info(
            "ElevenLabs agent session started for %s (agent_id=%s…)",
            self._packet.incident_id,
            agent_id[:12],
        )

    async def stop(self) -> None:
        self._room.set_user_audio_sink(None)
        conv = self._conversation
        self._conversation = None
        self._started = False
        if conv is None:
            return
        try:
            await asyncio.to_thread(conv.end_session)
        except Exception:  # noqa: BLE001
            logger.exception("ElevenLabs end_session failed")
        try:
            await asyncio.to_thread(conv.wait_for_session_end)
        except Exception:  # noqa: BLE001
            logger.debug("wait_for_session_end raised", exc_info=True)

    async def _park_conversation(self) -> None:
        """Stop ConvAI so it can't fill silence with 'are you still there?'."""
        self._room.set_user_audio_sink(None)
        # Mute agent TTS immediately so late Mentor chunks can't talk over us.
        self._audio.stop()
        conv = self._conversation
        self._conversation = None
        self._started = False
        if conv is None:
            return
        try:
            await asyncio.to_thread(conv.end_session)
        except Exception:  # noqa: BLE001
            logger.debug("park end_session", exc_info=True)
        try:
            await asyncio.to_thread(conv.wait_for_session_end)
        except Exception:  # noqa: BLE001
            logger.debug("park wait_for_session_end", exc_info=True)

    async def _thinking_filler_then_park(self) -> None:
        """Human-sounding cover while Grok edits; then mute ConvAI."""
        # Cut Mentor immediately so it can't ask "are you still there?" over us.
        self._audio.mute_agent_output()
        line = "Okay… on it. Give me a second while I apply that."
        try:
            from escalation_mcp.voice.tts import synthesize_pcm48

            pcm = await asyncio.to_thread(synthesize_pcm48, line)
            self._room.clear_audio_queue()
            self._room.play_pcm(pcm)
            # Let most of the line play before tearing down the agent socket.
            await asyncio.sleep(min(3.5, max(1.2, len(pcm) / (48000 * 2) * 0.85)))
        except Exception:  # noqa: BLE001
            logger.exception("thinking filler TTS failed")
        await self._park_conversation()
        logger.info("Mentor ConvAI parked after approve (TTS owns the rest of the call)")

    async def _send_to_grok(self, parameters: dict) -> str:
        raw = str(parameters.get("decision") or "").strip().lower()
        text = parameters.get("text")
        if isinstance(text, str):
            text = text.strip() or None
        else:
            text = None
        if raw not in _VALID:
            return f"error: invalid decision {raw!r}; use approve|reject|stop|instruction"
        decision = HumanDecision.model_validate(
            {
                "incident_id": self._packet.incident_id,
                "ts": _utc_now_iso(),
                "decision": raw,
                "text": text,
                "from": "facetime-mentor",
            }
        )
        await STATE.enqueue_decision(decision)
        logger.info(
            "send_to_grok → %s (%s)",
            decision.decision,
            (decision.text or "")[:80],
        )
        if decision.decision == "approve":
            # Cover the edit gap + stop ConvAI idle prompts.
            asyncio.create_task(
                self._thinking_filler_then_park(), name="mentor-thinking-park"
            )
            return (
                "queued decision=approve. "
                "Say NOTHING else. Do not ask if the user is still there. Stay silent."
            )
        if decision.decision in ("reject", "stop"):
            asyncio.create_task(self._park_conversation(), name="mentor-park")
            return (
                f"queued decision={decision.decision}. "
                "Say NOTHING else. Stay silent."
            )
        # ElevenLabs client_tool_result.result must be a string (dict → 1008 close).
        return f"queued decision={decision.decision}"
