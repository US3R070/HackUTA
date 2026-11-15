# Escalation hand-off contract (local MCP ↔ cloud Chert agent)

This is a **proposed** contract. Cloud-side teammates: confirm or change field names here; they live in `escalation_mcp/models.py` so a change is one edit.

## Auth

- Header: `Authorization: Bearer {ESCALATION_SHARED_SECRET}`
- Timeout: 10 s, 2 retries with backoff
- On failure: local keeps running (screen share still works), returns a warning, and saves `runs/<id>/packet.json` for resend

## Endpoints

### `POST {CHERT_AGENT_URL}/escalations`

Creates / notifies about an escalation.

```json
{
  "schema_version": "1",
  "incident_id": "inc_20261114_153012_a3f9",
  "created_at": "ISO-8601",
  "source": "grok-local",
  "severity": "HIGH",
  "failure_type": "nan",
  "title": "Training run crashed: NaN loss at step 4200",
  "summary": "Loss became NaN at step 4200. Learning rate 5.0 looks too high.",
  "proposed_fix": "Lower lr to 3e-4 and resume from the step-4000 checkpoint.",
  "default_action": "Keep the run paused at checkpoint 4000 and text a summary.",
  "run_id": "run_2026-11-14_1530",
  "details": {
    "traceback": "...",
    "log_tail": ["..."],
    "metrics_tail": [],
    "config": {},
    "git_diff": "..."
  },
  "media": {
    "transport": "livekit",
    "livekit_url": "wss://<project>.livekit.cloud",
    "room": "esc-inc_20261114_153012_a3f9",
    "screen_participant": "local-screen",
    "preview_url": "http://127.0.0.1:8765"
  },
  "channels": {
    "events_topic": "agent_events",
    "decisions_topic": "human_decisions"
  },
  "contacts": ["+1...", "+1..."]
}
```

Notes:

- `contacts` comes from `ESCALATION_CONTACTS` (optional); cloud may ignore and use its own list.
- **Never** include LiveKit API secrets in the packet. Cloud mints caller tokens with the same LiveKit project credentials.
- `preview_url` is local-only / informational.
- `default_action` is what the agent does if the human never decides (no answer after one redial, hangs up, or goes idle). Always non-destructive. **The call agent should say it in its opening line**, e.g. "If you hang up, I'll keep the run paused at checkpoint 4000."

### `POST {CHERT_AGENT_URL}/escalations/{incident_id}/report`

Final report when the local side ends the escalation:

```json
{
  "outcome": "resolved|aborted|handed_off",
  "summary": "...",
  "ended_at": "ISO-8601",
  "actions": [{ "type": "action", "seq": 1, "action": "...", "...": "..." }]
}
```

### `POST {CHERT_AGENT_URL}/escalations/{incident_id}/redial` (HTTP sink only)

Nobody joined within `CALL_NO_ANSWER_S`. Body: `{"attempt": 2}`. Place the call again. (With the current `ChertSink` the laptop redials Chert directly.)

### `POST {CHERT_AGENT_URL}/escalations/{incident_id}/hangup` (HTTP sink only)

The call is over on the local side (idle timeout, agent done, etc.). Hang up the phone leg. Body: `{}`.

### `POST {CHERT_AGENT_URL}/escalations/{incident_id}/events` (optional)

Mirrored only when `ESCALATION_HTTP_EVENTS=1` (fallback if cloud is not in the LiveKit room).

## LiveKit data-channel messages

Local joins room `esc-<incident_id>` as identity `local-screen`.

```json
// agent_events (local -> cloud)
{
  "type": "action",
  "incident_id": "...",
  "seq": 7,
  "ts": "ISO",
  "action": "edit_file",
  "detail": "config.yaml lr 5.0 -> 3e-4",
  "status": "done"
}

// human_decisions (cloud -> local)
{
  "type": "decision",
  "incident_id": "...",
  "ts": "ISO",
  "decision": "approve|reject|stop|instruction|hangup",
  "text": "lower the LR and resume",
  "from": "<participant name>"
}

// call_state (local -> cloud, on agent_events) - sent on every phase change
{
  "type": "call_state",
  "incident_id": "...",
  "ts": "ISO",
  "state": "waiting_answer|live|watching|agent_done|ended",
  "reason": null,              // when ended: hangup|caller_left|idle_timeout|agent_done|no_answer|reset
  "attempt": 1,
  "ends_in_s": 118.5,
  "default_action": "..."
}
```

## Call lifecycle

A decision does **not** end the call. The monitor stays live so the human can watch the fix run and keep giving instructions.

| Phase | Entered when | Ends / moves on when |
|---|---|---|
| `waiting_answer` | call placed | caller joins or sends a decision -> `live`; no join in `CALL_NO_ANSWER_S` -> redial once, then `ended(no_answer)` |
| `live` | caller joined | first decision -> `watching`. **No timer**: without a decision channel the call stays up until hang-up / caller leaves / agent done |
| `watching` | any decision | `CALL_IDLE_TIMEOUT_S` after the last decision -> `ended(idle_timeout)`; each decision resets it |
| `agent_done` | agent called `end_escalation` | `CALL_DONE_GRACE_S` after the agent finished or the human last spoke -> `ended(agent_done)` |
| `ended` | `hangup` decision, caller left (> `CALL_LEAVE_GRACE_S`), or a timer | final |

Cloud call agent responsibilities:
- When the human says they are done ("that's all, hang up"), send `decision: "hangup"`.
- When it receives `call_state` with `state: "ended"`, say goodbye and drop the call.
- Caller presence is detected locally from the LiveKit room: the Chert bridge participant (`chert-bridge`) joining / leaving.

Ignore and log messages with unknown `type` or mismatched `incident_id`.

## Open questions (do not block)

1. Does the cloud Chert agent join the LiveKit room as a participant (so it can receive `agent_events` and send `human_decisions`), or should events go over HTTP?
2. Who mints FaceTime caller tokens: the cloud agent (needs LiveKit creds) or local (would need an inbound endpoint)? This contract assumes the cloud agent does.
3. Is `agent/worker.py` (local Gemini call agent in MVP.md) still needed now that the call lives in the cloud?
