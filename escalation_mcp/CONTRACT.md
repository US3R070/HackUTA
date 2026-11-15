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
  "decision": "approve|reject|stop|instruction",
  "text": "lower the LR and resume",
  "from": "<participant name>"
}
```

Ignore and log messages with unknown `type` or mismatched `incident_id`.

## Open questions (do not block)

1. Does the cloud Chert agent join the LiveKit room as a participant (so it can receive `agent_events` and send `human_decisions`), or should events go over HTTP?
2. Who mints FaceTime caller tokens: the cloud agent (needs LiveKit creds) or local (would need an inbound endpoint)? This contract assumes the cloud agent does.
3. Is `agent/worker.py` (local Gemini call agent in MVP.md) still needed now that the call lives in the cloud?
