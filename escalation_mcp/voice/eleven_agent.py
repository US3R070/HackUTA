"""ElevenLabs Agents (ConvAI) bridge into the existing LiveKit agent participant.

Phone audio → ElevenLabs (16 kHz) → Gemini conversation + TTS → play_pcm (48 kHz).
Client tool send_to_grok → HumanDecision → Allen's decision queue for Grok MCP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

import numpy as np

from escalation_mcp.models import EscalationPacket, HumanDecision
from escalation_mcp.state import STATE
from escalation_mcp.voice.resample import resample_pcm16

if TYPE_CHECKING:
    from escalation_mcp.livekit_room import LiveKitRoomConnection

logger = logging.getLogger("escalation_mcp")

EL_SR = 16_000
LK_SR = 48_000
# 100 ms uplink chunks: ElevenLabs suggests ~250 ms, but smaller chunks cut up to
# ~150 ms off end-of-speech detection (snappier replies) at negligible cost.
EL_CHUNK_SAMPLES = 1_600
EL_CHUNK_BYTES = EL_CHUNK_SAMPLES * 2
# Only inject keep-alive silence when no real phone audio has arrived for this
# long. Mixing silence blocks into live phone audio doubles the uplink rate and
# splices gaps into speech, which wrecks ElevenLabs VAD / turn-taking.
REAL_AUDIO_GAP_S = 0.3
GATE_LOG_EVERY_S = 5.0
# How long send_to_grok(approve|instruction) waits for Grok's fix_done before
# returning "still working". Keep below the tool's response timeout in the
# ElevenLabs dashboard.
FIX_WAIT_S = float(os.getenv("MENTOR_FIX_WAIT_S") or "75")

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
    rationale = details.get("rationale") or "(none given; reason from the summary and logs)"
    fixes_s = format_fixes(details.get("fixes"))
    extra = {
        k: v
        for k, v in details.items()
        if k not in {"log_tail", "traceback", "config", "rationale", "fixes"}
    }
    extra_s = json.dumps(extra, default=str)[:800] if extra else "(none)"
    return (
        f"incident_id: {packet.incident_id}\n"
        f"title: {packet.title}\n"
        f"failure_type: {packet.failure_type}\n"
        f"severity: {packet.severity}\n"
        f"summary: {packet.summary}\n"
        f"proposed_fix: {packet.proposed_fix or '(none)'}\n"
        f"rationale (why this cause, why this exact fix/value): {rationale}\n"
        f"fixes (ordered; refer to them by number):\n{fixes_s}\n"
        f"run_id: {packet.run_id or '(none)'}\n"
        f"config: {cfg}\n"
        f"other details: {extra_s}\n"
        f"log_tail:\n{tail}\n"
        f"traceback:\n{tb}\n"
    )


def format_fixes(fixes: Any) -> str:
    """Render details.fixes ([{id,title,file,change,why,kind}]) for the agent."""
    if not isinstance(fixes, list) or not fixes:
        return "  (single fix: see proposed_fix)"
    lines = []
    for i, fx in enumerate(fixes, 1):
        if not isinstance(fx, dict):
            lines.append(f"  {i}. {fx}")
            continue
        kind = str(fx.get("kind") or "required")
        lines.append(
            f"  {fx.get('id', i)}. [{kind}] {fx.get('title', '')}"
            f" | file: {fx.get('file', '?')} | change: {fx.get('change', '?')}"
            f" | why: {fx.get('why', '?')}"
        )
    return "\n".join(lines)


def _fix_count(packet: EscalationPacket) -> int:
    fixes = (packet.details or {}).get("fixes")
    return len(fixes) if isinstance(fixes, list) else 1


_PROMPT_PATH = Path(__file__).resolve().parents[2] / "skills" / "mentor_voice.md"


def mentor_prompt_with_context(packet: EscalationPacket) -> str:
    """Full system prompt: repo-owned persona/rules + this incident's context."""
    try:
        persona = _PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("mentor prompt missing at %s; using minimal prompt", _PROMPT_PATH)
        persona = (
            "You are Mentor, a friendly ML engineer on a FaceTime call about a failed "
            "training run. Be brief and conversational. Use send_to_grok for decisions "
            "and end_call when the caller is done."
        )
    return f"{persona}\n\n## INCIDENT CONTEXT\n{incident_context(packet)}"


def first_message_for(packet: EscalationPacket) -> str:
    """Spoken verbatim by TTS, so no file paths or raw numbers like 3e-4."""
    summary = (packet.summary or packet.title or "your training run failed").strip()
    first = re.split(r"(?<=[.!?])\s+", summary, maxsplit=1)[0].rstrip(".!?")
    if len(first) > 120:
        first = first[:117] + "…"
    found = (
        "I found a couple of things in your training code, and I've got fixes ready."
        if _fix_count(packet) > 1
        else "I think I know why, and I've got a fix ready."
    )
    return (
        f"Hey, it's Mentor. Heads up, {first[0].lower() + first[1:] if first else 'your run broke'}. "
        f"{found} Want me to walk you through it, or just apply it?"
    )


def _rms(pcm: bytes) -> float:
    samples = np.frombuffer(pcm, dtype=np.int16)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


class VoiceGate:
    """Replace quiet phone audio (distant room chatter) with silence.

    Two thresholds (int16 RMS): a normal one, and a higher "barge-in" one while
    the agent is talking, so only someone close to the phone mic can interrupt.
    Once open, the gate holds briefly so word tails and soft consonants pass.
    Silence (not dropped audio) keeps the uplink continuous for ElevenLabs.

    Env: VOICE_GATE=0 disables; VOICE_GATE_RMS, VOICE_GATE_BARGE_RMS,
    VOICE_GATE_HOLD_S, VOICE_GATE_BARGE_ATTACK_S tune it. Periodic
    "voice gate:" log lines show live levels for calibration.
    """

    def __init__(self, is_agent_speaking: Callable[[], bool]) -> None:
        self.enabled = os.getenv("VOICE_GATE", "1") != "0"
        self.open_rms = _env_float("VOICE_GATE_RMS", 500.0)
        self.barge_rms = _env_float("VOICE_GATE_BARGE_RMS", 1400.0)
        self.hold_s = _env_float("VOICE_GATE_HOLD_S", 0.45)
        self.barge_attack_s = _env_float("VOICE_GATE_BARGE_ATTACK_S", 0.06)
        self._agent_speaking = is_agent_speaking
        self._open_until = 0.0
        self._loud_run_s = 0.0
        self._reset_stats(time.monotonic())

    def _reset_stats(self, now: float) -> None:
        self._stat_t0 = now
        self._levels: list[float] = []
        self._passed_s = 0.0
        self._total_s = 0.0
        self._agent_s = 0.0

    def process(self, pcm: bytes) -> bytes:
        if not self.enabled or not pcm:
            return pcm
        now = time.monotonic()
        dur = len(pcm) / (2.0 * EL_SR)
        level = _rms(pcm)
        try:
            agent = bool(self._agent_speaking())
        except Exception:  # noqa: BLE001
            agent = False

        threshold = self.barge_rms if agent else self.open_rms
        if level >= threshold:
            self._loud_run_s += dur
            attack = self.barge_attack_s if agent else 0.0
            if self._loud_run_s >= attack:
                self._open_until = now + self.hold_s
        else:
            self._loud_run_s = 0.0
        is_open = now < self._open_until

        self._levels.append(level)
        self._total_s += dur
        self._passed_s += dur if is_open else 0.0
        self._agent_s += dur if agent else 0.0
        if now - self._stat_t0 >= GATE_LOG_EVERY_S and self._levels:
            arr = np.asarray(self._levels)
            logger.info(
                "voice gate: rms p50=%d p90=%d peak=%d · passed %d%% · agent talking %d%% "
                "· thresholds open=%d barge=%d",
                np.percentile(arr, 50),
                np.percentile(arr, 90),
                arr.max(),
                100 * self._passed_s / max(self._total_s, 1e-6),
                100 * self._agent_s / max(self._total_s, 1e-6),
                self.open_rms,
                self.barge_rms,
            )
            self._reset_stats(now)

        return pcm if is_open else bytes(len(pcm))


class LiveKitAudioInterface:
    """AudioInterface adapter: LiveKit room mic ↔ ElevenLabs 16 kHz PCM."""

    def __init__(self, room_conn: "LiveKitRoomConnection") -> None:
        self._room = room_conn
        self._gate = VoiceGate(getattr(room_conn, "agent_speaking", lambda: False))
        self._input_callback: Optional[Callable[[bytes], None]] = None
        self._running = False
        self._in_buf = bytearray()
        self._lock = threading.Lock()
        self._out_hz = EL_SR
        self._silence_thread: threading.Thread | None = None
        self._bytes_out = 0
        self._mute_agent_out = False
        self._last_real_audio = 0.0

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
        self._last_real_audio = 0.0
        # Keep a silent uplink alive whenever FaceTime audio isn't flowing.
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
            self._room.mark("first_agent_audio")
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
        self._last_real_audio = time.monotonic()
        self._push(self._gate.process(pcm16k))

    def _push(self, pcm16k: bytes) -> None:
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
            if time.monotonic() - self._last_real_audio > REAL_AUDIO_GAP_S:
                self._push(silence)
            threading.Event().wait(EL_CHUNK_SAMPLES / float(EL_SR))


def _agent_capabilities(agent_id: str, api_key: str) -> dict[str, Any]:
    """Read the agent's dashboard config: which overrides are allowed, tool setup."""
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/convai/agents/{agent_id}",
        headers={"xi-api-key": api_key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode())
    agent_ov = (
        ((data.get("platform_settings") or {}).get("overrides") or {})
        .get("conversation_config_override", {})
        .get("agent", {})
    )
    prompt_cfg = (
        (data.get("conversation_config") or {}).get("agent", {}).get("prompt", {})
    )
    tools = {t.get("name"): t for t in (prompt_cfg.get("tools") or [])}
    built_in = prompt_cfg.get("built_in_tools") or {}
    return {
        "prompt_override": bool((agent_ov.get("prompt") or {}).get("prompt")),
        "first_message_override": bool(agent_ov.get("first_message")),
        "send_to_grok_timeout": (tools.get("send_to_grok") or {}).get("response_timeout_secs"),
        "end_call": bool(built_in.get("end_call")) or "end_call" in tools,
        "skip_turn": bool(built_in.get("skip_turn")) or "skip_turn" in tools,
    }


class ElevenAgentBridge:
    """Owns one ElevenLabs Conversation for the whole live call.

    The agent stays connected from greeting to goodbye. Grok only edits code:
    send_to_grok(approve|instruction) waits for Grok's fix_done() and returns
    the result so the agent can explain it; the agent's built-in end_call
    hangs up the FaceTime call right after its goodbye finishes playing.
    """

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
        self._prepared = False
        self._stopping = False
        self._agent_id = ""
        self._overrides_applied = False
        # Pending Grok result for an approve/instruction (resolved by fix_done).
        self._fix_future: asyncio.Future[str] | None = None
        # Last successful approve result + the approval texts it covered.
        self._done_result: str = ""
        self._done_texts: list[str] = []

    @property
    def audio(self) -> LiveKitAudioInterface:
        return self._audio

    async def prepare(self) -> None:
        """Do all slow setup while the phone is still ringing.

        Imports the SDK, checks the agent's dashboard config, builds the
        Conversation and prefetches the signed websocket URL so start() only
        has to open the socket once the call is answered.
        """
        if self._prepared:
            return
        await asyncio.to_thread(self._build_conversation)
        self._prepared = True

    def _build_conversation(self) -> None:
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
        # Screen camera (must also be declared as client tools in the dashboard).
        client_tools.register("show_code", self._show_code, is_async=True)
        client_tools.register("show_full_screen", self._show_full_screen, is_async=True)

        # Prompt + greeting live in the repo (skills/mentor_voice.md) and are
        # sent per call, if the agent allows overrides (dashboard → Security).
        override: dict[str, Any] = {}
        try:
            caps = _agent_capabilities(agent_id, api_key)
        except Exception:  # noqa: BLE001
            logger.exception("could not read ElevenLabs agent config; sending no overrides")
            caps = {}
        agent_ov: dict[str, Any] = {}
        if caps.get("prompt_override"):
            agent_ov["prompt"] = {"prompt": mentor_prompt_with_context(self._packet)}
        if caps.get("first_message_override"):
            agent_ov["first_message"] = first_message_for(self._packet)
        if agent_ov:
            override["agent"] = agent_ov
        self._overrides_applied = "prompt" in agent_ov
        if caps:
            logger.info("ElevenLabs agent capabilities: %s", caps)
            if not caps.get("prompt_override") or not caps.get("first_message_override"):
                logger.warning(
                    "ElevenLabs: enable System prompt + First message overrides "
                    "(agent → Security → Overrides) so the repo prompt and the "
                    "per-incident greeting are used; falling back to dashboard prompt"
                )
            timeout = caps.get("send_to_grok_timeout") or 0
            if timeout and timeout < FIX_WAIT_S + 2:
                logger.warning(
                    "ElevenLabs: send_to_grok response timeout is %ss but bridge waits "
                    "up to %ss for Grok; raise it to ~%ds in the dashboard",
                    timeout, FIX_WAIT_S, int(FIX_WAIT_S + 5),
                )
            if not caps.get("end_call"):
                logger.warning("ElevenLabs: built-in end_call tool is off; agent can't hang up")

        config = ConversationInitiationData(
            conversation_config_override=override,
            dynamic_variables={
                "incident_id": self._packet.incident_id,
                "summary": self._packet.summary or "",
                "proposed_fix": self._packet.proposed_fix or "",
                "failure_type": self._packet.failure_type,
                "severity": self._packet.severity,
            },
        )

        def _on_disconnect(details: Any) -> None:
            reason = getattr(details, "reason", None)
            logger.warning("ElevenLabs disconnected: %s", details)
            # "agent" = normal server close, e.g. the agent called end_call.
            if reason == "agent" and not self._stopping:
                asyncio.run_coroutine_threadsafe(self._handle_agent_ended(), self._loop)

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
        self._agent_id = agent_id

        # Prefetch the signed URL now; start_session() would otherwise fetch it
        # synchronously after answer. Signed URLs stay valid ~15 minutes.
        conv = self._conversation
        if getattr(conv, "requires_auth", False) and hasattr(conv, "_get_signed_url"):
            try:
                signed_url = conv._get_signed_url()  # noqa: SLF001
                conv._get_signed_url = lambda: signed_url  # noqa: SLF001
                logger.info("ElevenLabs: signed URL prefetched during ringing")
            except Exception:  # noqa: BLE001
                logger.exception("ElevenLabs signed URL prefetch failed; will fetch on start")

    async def start(self) -> None:
        if self._started:
            return
        if not self._prepared:
            await self.prepare()
        # SDK runs the websocket on a background thread.
        await asyncio.to_thread(self._conversation.start_session)
        self._room.mark("eleven_session_started")
        self._started = True
        self._room.set_user_audio_sink(self._audio.push_user_pcm16_16k)

        if not self._overrides_applied:
            # Dashboard prompt in use: at least give it the incident details.
            try:
                await asyncio.sleep(0.8)
                await asyncio.to_thread(
                    self._conversation.send_contextual_update,
                    f"Incident context for this call:\n{incident_context(self._packet)}",
                )
            except Exception:  # noqa: BLE001
                logger.exception("send_contextual_update failed")

        logger.info(
            "ElevenLabs agent session started for %s (agent_id=%s…, repo prompt=%s)",
            self._packet.incident_id,
            self._agent_id[:12],
            self._overrides_applied,
        )

    async def stop(self) -> None:
        self._stopping = True
        self._room.set_user_audio_sink(None)
        if self._fix_future and not self._fix_future.done():
            self._fix_future.cancel()
        conv = self._conversation
        self._conversation = None
        self._started = False
        self._prepared = False
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

    # ---- Grok → caller ----
    def on_fix_done(self, summary: str, applied: bool = True) -> str:
        """Called by the fix_done MCP tool (event loop). Returns delivery mode."""
        result = (
            f"Grok finished. Applied: {'yes' if applied else 'no'}. {summary.strip()}"
        )
        fut = self._fix_future
        if fut is not None and not fut.done():
            fut.set_result(result)
            return "tool_result"
        # Nobody waiting (tool already timed out): push as context so the agent
        # mentions it on its next turn.
        conv = self._conversation
        if conv is not None:
            try:
                conv.send_contextual_update(
                    f"UPDATE: {result} Tell the caller about this on your next turn."
                )
                return "contextual_update"
            except Exception:  # noqa: BLE001
                logger.exception("contextual update for fix_done failed")
        return "dropped"

    async def _handle_agent_ended(self) -> None:
        """Agent hung up (end_call): let the goodbye finish playing, then end the call."""
        logger.info("Mentor ended the conversation; waiting for goodbye audio to drain")
        deadline = time.monotonic() + 10.0
        while self._room.agent_speaking() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        from escalation_mcp.session import hangup_from_agent

        try:
            await hangup_from_agent(self._packet.incident_id, reason="agent_end_call")
        except Exception:  # noqa: BLE001
            logger.exception("hangup after agent end_call failed")

    # ---- caller → Grok ----
    async def _show_code(self, parameters: dict) -> str:
        """Client tool: zoom the caller's screen onto a code line. Always returns a string."""
        query = str(parameters.get("query") or parameters.get("target") or "").strip()
        if not query:
            return "error: pass query = 'path:line' from the incident's code_anchors, or a short code snippet"
        try:
            from escalation_mcp import session as sess

            result = await sess.focus_code(self._packet.incident_id, query, source="facetime-mentor")
        except Exception as exc:  # noqa: BLE001
            logger.exception("show_code failed")
            return f"Could not move the screen: {exc}. Keep talking without it."
        if result.get("ok"):
            return (
                f"The caller's screen is now zoomed on {result.get('target')} (highlighted line). "
                "Refer to it naturally, e.g. 'see that highlighted line'."
            )
        return (
            f"Could not show it ({result.get('error')}). The caller sees the full screen; "
            "describe it in words instead."
        )

    async def _show_full_screen(self, parameters: dict) -> str:
        try:
            from escalation_mcp import session as sess

            await sess.show_full_screen(self._packet.incident_id, source="facetime-mentor")
        except Exception as exc:  # noqa: BLE001
            logger.exception("show_full_screen failed")
            return f"Could not change the screen: {exc}"
        return "The caller now sees their whole screen again."

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

        wants_change = decision.decision in ("approve", "instruction")
        pending = self._fix_future is not None and not self._fix_future.done()
        if decision.decision == "approve" and not pending and self._already_covered(text):
            # Approved fixes are already applied (often a stray "okay"/background
            # speech after the result). Answer from the stored result instead of
            # sending Grok a second, redundant fix cycle.
            logger.info("send_to_grok → approve (already applied; not re-queued): %s", (text or "")[:60])
            return (
                f"Already done, nothing to send. {self._done_result} "
                "Do not call send_to_grok again for these fixes. Wrap up as instructed."
            )
        if decision.decision == "approve" and pending:
            # Agent re-sent approve while Grok is still working (e.g. after a
            # tool timeout). Don't queue a duplicate edit; wait on the same one.
            logger.info("send_to_grok → approve (duplicate while Grok is working; not re-queued)")
        else:
            if wants_change and not pending:
                self._fix_future = self._loop.create_future()
            await STATE.enqueue_decision(decision)
            logger.info(
                "send_to_grok → %s (%s)", decision.decision, (decision.text or "")[:80]
            )

        # ElevenLabs client_tool_result.result must be a string (dict → 1008 close).
        if decision.decision == "reject":
            return (
                "Recorded: no changes will be made. Acknowledge briefly and ask if "
                "there's anything else."
            )
        if decision.decision == "stop":
            return "Recorded: Grok is stopping and reverting. Confirm briefly."

        fut = self._fix_future
        assert fut is not None
        try:
            result = await asyncio.wait_for(asyncio.shield(fut), timeout=FIX_WAIT_S)
        except asyncio.TimeoutError:
            logger.warning("send_to_grok: no fix_done after %.0fs", FIX_WAIT_S)
            return (
                "Grok is still applying the change. Tell the caller it's taking a few "
                "more seconds; you'll get an update."
            )
        except asyncio.CancelledError:
            return "The call is ending."
        logger.info("send_to_grok result → %s", result[:160])
        if decision.decision == "approve" and "Applied: yes" in result:
            self._done_result = result
            self._done_texts.append((text or "").lower())
        return (
            f"{result} Now, in two or three sentences: what changed, the numbers from the "
            "check, and that you're kicking off the full training run and will keep them "
            "posted. Then say a short goodbye and call end_call (unless they just asked "
            "something; answer it first)."
        )

    def _already_covered(self, text: str | None) -> bool:
        """True if an earlier approval (already applied) covers this one."""
        if not self._done_result:
            return False
        t = (text or "").lower()
        for prev in self._done_texts:
            if any(w in prev for w in ("all", "both", "1 and 2", "everything")) or not prev.strip():
                return True  # everything was already applied
            if t == prev:
                return True
        return False
