"""Call lifecycle: keeps the monitor alive until the human is done, not until they decide.

Phases:
  waiting_answer --caller joins / any decision--> live --first decision--> watching
  any live phase --agent calls end_escalation--> agent_done (short grace)
  any phase --hangup / caller_left / timer--> ended

Timers (all reset by human activity):
  waiting_answer: no_answer_s per attempt (starts once the call is placed);
                  redial up to max_attempts, then end(no_answer)
  live:           no timer: stays up until hang-up / caller leaves / agent done
  watching:       idle_s since the last human decision, then end(idle_timeout)
  agent_done:     done_grace_s since agent finished or last human activity, then end(agent_done)
  caller left:    leave_grace_s to rejoin, then end(caller_left)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from escalation_mcp.models import CallPhase, EndReason

logger = logging.getLogger("escalation_mcp")

StateCallback = Callable[["CallController"], Awaitable[None]]
RedialCallback = Callable[[int], Awaitable[None]]
EndCallback = Callable[[EndReason], Awaitable[None]]

ACTIVE_PHASES: tuple[CallPhase, ...] = ("live", "watching", "agent_done")


@dataclass
class CallTimings:
    no_answer_s: float = 60.0
    idle_s: float = 120.0
    done_grace_s: float = 30.0
    leave_grace_s: float = 5.0
    max_attempts: int = 2
    tick_s: float = 0.25


class CallController:
    def __init__(
        self,
        incident_id: str,
        timings: CallTimings,
        *,
        on_state: StateCallback,
        on_redial: RedialCallback,
        on_end: EndCallback,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.incident_id = incident_id
        self.t = timings
        self._on_state = on_state
        self._on_redial = on_redial
        self._on_end = on_end
        self._clock = clock
        self.phase: CallPhase = "waiting_answer"
        self.attempt = 1
        self.end_reason: EndReason | None = None
        self.human_decided = False  # any approve/reject/stop/instruction seen
        self._deadline: float | None = None
        self._left_at: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._ended = asyncio.Event()

    # ---- lifecycle ----
    async def start(self) -> None:
        """Call once the call has actually been placed (place_call may queue for minutes)."""
        if self.phase == "waiting_answer":
            self._deadline = self._clock() + self.t.no_answer_s
        self._task = asyncio.create_task(self._run(), name=f"call-{self.incident_id}")
        await self._emit()

    async def cancel(self) -> None:
        """Stop timers without firing on_end (used by reset)."""
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        if self.phase != "ended":
            self.phase = "ended"
            self.end_reason = self.end_reason or "reset"
        self._ended.set()

    @property
    def ended(self) -> bool:
        return self.phase == "ended"

    @property
    def finished(self) -> bool:
        """Ended AND on_end (hang-up + media teardown) has completed."""
        return self._ended.is_set()

    async def wait_ended(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._ended.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def ends_in_s(self) -> float | None:
        if self.phase == "ended" or self._deadline is None:
            return None
        remaining = self._deadline - self._clock()
        if self._left_at is not None:
            remaining = min(remaining, self._left_at + self.t.leave_grace_s - self._clock())
        return max(0.0, round(remaining, 1))

    def snapshot(self) -> dict[str, object]:
        return {
            "call_state": self.phase,
            "ends_in_s": self.ends_in_s(),
            "end_reason": self.end_reason,
            "attempt": self.attempt,
        }

    # ---- inputs ----
    async def caller_joined(self) -> None:
        async with self._lock:
            if self.ended:
                return
            self._left_at = None
            if self.phase == "waiting_answer":
                # No idle timer until the first decision: without a decision channel
                # (e.g. no voice agent yet) the call must stay up until hang-up.
                self.phase = "live"
                self._deadline = None
                changed = True
            else:
                changed = False
        if changed:
            await self._emit()

    async def caller_left(self) -> None:
        async with self._lock:
            if self.ended or self.phase == "waiting_answer":
                return
            self._left_at = self._clock()
        logger.info("caller left %s; ending in %.1fs unless they rejoin",
                    self.incident_id, self.t.leave_grace_s)

    async def human_activity(self, decision: str) -> None:
        """Any decision from the human. A decision also proves they are on the call."""
        if decision == "hangup":
            await self.end("hangup")
            return
        async with self._lock:
            if self.ended:
                return
            self._left_at = None
            self.human_decided = True
            now = self._clock()
            if self.phase == "agent_done":
                self._deadline = now + self.t.done_grace_s
            else:
                self.phase = "watching"
                self._deadline = now + self.t.idle_s
        await self._emit()

    async def agent_done(self) -> None:
        async with self._lock:
            if self.ended:
                return
            if self.phase == "waiting_answer":
                hang_now = True
            else:
                hang_now = False
                self.phase = "agent_done"
                self._deadline = self._clock() + self.t.done_grace_s
        if hang_now:
            await self.end("agent_done")
        else:
            await self._emit()

    async def end(self, reason: EndReason) -> None:
        async with self._lock:
            if self.ended:
                return
            self.phase = "ended"
            self.end_reason = reason
            self._deadline = None
        logger.info("call %s ended: %s", self.incident_id, reason)
        await self._emit()
        try:
            await self._on_end(reason)
        except Exception:  # noqa: BLE001
            logger.exception("on_end failed")
        finally:
            self._ended.set()

    # ---- timer loop ----
    async def _run(self) -> None:
        while not self.ended:
            await asyncio.sleep(self.t.tick_s)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("call timer tick failed")

    async def _tick(self) -> None:
        now = self._clock()
        if self._left_at is not None and now - self._left_at >= self.t.leave_grace_s:
            await self.end("caller_left")
            return
        if self._deadline is None or now < self._deadline:
            return
        if self.phase == "waiting_answer":
            if self.attempt < self.t.max_attempts:
                self.attempt += 1
                self._deadline = None  # paused while redialing (place_call can block)
                logger.info("no answer for %s; redial attempt %d", self.incident_id, self.attempt)
                try:
                    await self._on_redial(self.attempt)
                except Exception:  # noqa: BLE001
                    logger.exception("redial failed")
                if self.phase == "waiting_answer":
                    self._deadline = self._clock() + self.t.no_answer_s
                await self._emit()
            else:
                await self.end("no_answer")
        elif self.phase == "agent_done":
            await self.end("agent_done")
        else:
            await self.end("idle_timeout")

    async def _emit(self) -> None:
        try:
            await self._on_state(self)
        except Exception:  # noqa: BLE001
            logger.exception("call state callback failed")
