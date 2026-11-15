import asyncio
import os
import threading

from incident.models import Incident

os.environ.setdefault("ESCALATION_MODE", "live")

from escalation_mcp import session as sess  # noqa: E402

_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True, name="escalation-loop").start()
asyncio.run_coroutine_threadsafe(asyncio.sleep(0), _loop).result()
_loop.call_soon_threadsafe(sess.configure_runtime)

ACTIVE: dict[str, str] = {}  # our incident id -> escalation incident id


def _fix_text(inc: Incident) -> str:
    t = inc.triage
    if not t:
        return ""
    if t.fix.changes:
        return ", ".join(f"{k} -> {v}" for k, v in t.fix.changes.items())
    return "code patch drafted" if t.fix.patch else ""


async def _start(inc: Incident) -> dict:
    t = inc.triage
    res = await sess.start_escalation(
        title=t.summary if t else f"{inc.type} in {inc.run_id}",
        summary=t.cause if t else "",
        failure_type=inc.type,
        severity=t.severity if t else inc.default_severity(),
        proposed_fix=_fix_text(inc),
        run_id=inc.run_id,
        details={"step": inc.step, "traceback": inc.traceback,
                 "log_tail": inc.log_tail[-30:], "config": inc.config},
        share="screen",
    )
    await sess.report_action(res["incident_id"], "Grok diagnosed failure", detail=_fix_text(inc), status="done")
    return res


def escalate(inc: Incident) -> None:
    """register() handler: HIGH incidents start a live escalation (call + screen + overlay)."""
    severity = inc.triage.severity if inc.triage else inc.default_severity()
    if severity != "HIGH":
        print(f"[escalate] {inc.id} is {severity}; not calling")
        return
    res = asyncio.run_coroutine_threadsafe(_start(inc), _loop).result(timeout=180)
    ACTIVE[inc.id] = res["incident_id"]
    print(f"[escalate] {inc.id} -> {res['incident_id']} warnings={res['warnings']}")


def wait_forever() -> None:
    """Keep the process (and the call) alive after training exits."""
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        for eid in ACTIVE.values():
            # hang_up_now: skip the watch grace, the process is about to exit
            asyncio.run_coroutine_threadsafe(
                sess.end_escalation(eid, "aborted", "operator exit", hang_up_now=True), _loop
            ).result(10)
