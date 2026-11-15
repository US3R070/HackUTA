# Prompt: Build the Local Escalation MCP Skill

> Hand this whole file to a coding agent. It is self-contained.
> Read `README.md` and `MVP.md` first for team context, but **this file overrides them for the scope below**.

---

## 0. Your role and scope

You are implementing **one component** of a HackUTA project: a **local MCP server + skill** that a local **Grok bot** (xAI coding agent running on the laptop) will connect to. When the Grok bot hits a failure it cannot or should not resolve alone, it calls this MCP, and the MCP:

- **Branch A: Live screen + live actions.** It starts a live screen share of the laptop so a human on their phone can see the failure, and it streams the Grok bot's real-time actions ("reading traceback", "editing config.yaml", "resuming run") to the remote call agent and as an on-screen overlay.
- **Branch B: Hand-off to the cloud.** It packages the failure (an "escalation packet") and sends it to the **cloud Chert agent**. The cloud agent owns everything about the phone call (iMessage, FaceTime/group call, voice, conversation). **You do NOT implement any calling logic.**
- **Return channel.** It receives the human's decisions/interrupts from the call (approve / reject / stop / free-text instruction) and exposes them to the Grok bot as a tool.

The repo is currently **scaffolding only** (all modules are one-line docstrings). Do not depend on any teammate module being implemented.

### Hard boundaries
- Create everything under a **new package `escalation_mcp/`** plus **`skills/escalate.md`**.
- You MAY append to `requirements.txt` and `.env.example` (append only, don't reorder or delete lines).
- Do **NOT** edit files owned by others: `watcher/`, `incident/`, `triage/`, `fixer/`, `chert/`, `agent/`, `demo/`, `skills/watcher.md`, `MVP.md`, `README.md`.
- Do **NOT** invent Chert API endpoints. The cloud agent is a teammate's service; talk to it only through the contract in section 5, behind an interface with a mock.
- Everything in English (code, comments, docs).
- Must run on **Windows 11** (primary dev machine) and macOS. Python 3.11+.

---

## 1. Architecture

```
LAPTOP                                                                    CLOUD / PHONE
──────
 Grok bot (local) ──MCP stdio──► escalation_mcp server
                                   │
                                   ├─ packet.py   build + redact EscalationPacket, save to runs/<id>/
                                   │
                                   ├─ sink/       ── Branch B ──► POST packet ─────────► Chert cloud agent
                                   │   mock.py  (files)                                   (places group call,
                                   │   http.py  (live)                                     talks to humans)
                                   │                                                          │
                                   ├─ screen/     ── Branch A ──► video track ──► LiveKit room ◄──┘ (Chert bridges
                                   │   capture.py   (mss, window crop, image)                      FaceTime callers in)
                                   │   overlay.py   (action banner)
                                   │   backends/preview.py   localhost MJPEG (dev + fallback)
                                   │   backends/livekit.py   publish screenshare track
                                   │
                                   └─ channel/    events up / decisions down
                                       LiveKit data channel (topic "agent_events" / "human_decisions")
                                       + mock: runs/<id>/outbox.jsonl / inbox.jsonl
```

Key decision: the local process **joins the same LiveKit room** that Chert bridges the FaceTime call into. That one connection carries the screen track (down to the phone) **and** the data channel (agent events up, human decisions back). No inbound tunnel is needed on the laptop.

---

## 2. MCP server

- Use the official Python MCP SDK (`mcp` package, `from mcp.server.fastmcp import FastMCP`), **stdio transport**.
- Entry point: `python -m escalation_mcp` (and `escalation_mcp/__main__.py`).
- **stdout is reserved for the MCP protocol.** All logging goes to stderr and to `runs/<incident_id>/mcp.log`. A stray `print()` will break the Grok bot connection.
- Screen share and LiveKit connection run as **background asyncio tasks inside the server process**. They must outlive individual tool calls and shut down cleanly on `end_escalation` or process exit.
- Every tool returns a small JSON-serialisable dict. Never return image bytes or huge logs.
- Every tool call is appended to `runs/<incident_id>/agent_log.jsonl` (team contract from README).

### Tools (exactly these five)

```python
escalate_failure(
    title: str,                 # "Training run crashed: NaN loss at step 4200"
    summary: str,               # 1–3 sentences, speakable in <15s
    failure_type: str,          # free string; known: crash|nan|oom|plateau|overfit|test_fail|build_fail|stuck|other
    severity: Literal["LOW","HIGH"],
    details: dict | None = None,  # optional: traceback, log_tail, metrics_tail, config, git_diff, files, any extra keys
    proposed_fix: str | None = None,
    run_id: str | None = None,
    share: str = "screen",      # "screen" | "window:<title substring>" | "image:<path>" | "none"
) -> {"incident_id", "status", "room", "preview_url", "sink": "mock|http", "warnings": [...]}
```
Creates the incident (`inc_<yyyymmdd_hhmmss>_<4 hex>`), builds and redacts the packet, starts screen share (unless `none`), connects to the LiveKit room (live mode), sends the packet to the sink. **Idempotent-ish:** if an escalation is already active, return it instead of opening a second one (one active incident at a time is fine for the MVP).

```python
report_action(incident_id: str, action: str, detail: str = "",
              status: Literal["started","done","failed"] = "started") -> {"ok", "seq"}
```
Appends an event, updates the on-screen overlay banner, publishes on `agent_events`. Must be cheap (<50 ms, non-blocking): the bot calls it often.

```python
get_human_response(incident_id: str, wait_seconds: int = 0) -> {
    "decision": "approve|reject|stop|instruction|none",
    "text": str | None, "from": str | None, "ts": str | None, "pending_count": int }
```
Returns the oldest unconsumed decision (and marks it consumed). `wait_seconds` long-polls, capped at 25 s to stay under typical MCP tool timeouts. `"stop"` is sticky: once received, every subsequent call returns `stop` until `end_escalation`.

```python
set_screen_view(incident_id: str, view: str) -> {"ok", "view"}
```
Switch what is shared: `"screen"`, `"window:<title substring>"`, `"image:<path>"` (e.g. a loss-plot PNG the bot just rendered). Unknown window → keep current view, return a warning.

```python
end_escalation(incident_id: str, outcome: Literal["resolved","aborted","handed_off"], summary: str) -> {"ok"}
```
Publishes a final event, sends a final report to the sink, stops screen share, leaves the room.

---

## 3. Branch A: screen share + live actions

### Capture (`screen/capture.py`)
- `mss` for full-screen capture (primary monitor, configurable index).
- Window mode: find the window by title substring and crop to its rect. On Windows use `pygetwindow` (or `ctypes` + `EnumWindows`); on macOS fall back to full screen with a warning. Window mode is the **privacy-preferred** default for demos: sharing only the terminal/IDE avoids leaking other apps.
- Image mode: load a file with Pillow, letterbox to the output size, re-read the file when its mtime changes (so a live-updating plot works).
- Downscale to `SCREEN_MAX_WIDTH` (default 1280), keep aspect, even dimensions. Target `SCREEN_FPS` (default 5; frames for screen text, not motion).

### Overlay (`screen/overlay.py`)
Pillow draws onto each frame:
- Top banner: `● LIVE · <incident title>` with severity colour (red HIGH / amber LOW).
- Bottom banner: last 3 `report_action` events, newest first, each `HH:MM:SS  <status icon>  <action>: <detail>` (truncate to one line).
- When a `stop` decision arrives: full-width red bar `STOPPED BY <from>` that stays until `end_escalation`.
The overlay is the visual proof of "real-time agent behaviour". It must work even when the sink is mock.

### Backends (common interface `ScreenBackend.start(frame_source) / stop()`)
1. **`preview` (build first):** a tiny HTTP server on `127.0.0.1:<PREVIEW_PORT>` (default 8765) serving an MJPEG stream at `/` and the latest JPEG at `/frame.jpg`. Always on when sharing. This is the dev tool and the demo fallback if LiveKit fails.
2. **`livekit` (live mode):** join `LIVEKIT_URL` / room from section 4 with identity `local-screen`, create `rtc.VideoSource(w, h)`, publish a `LocalVideoTrack` with `TrackSource.SOURCE_SCREENSHARE`, push RGBA frames via `capture_frame`. Use the `livekit` (rtc) and `livekit-api` packages; **verify the exact API against the installed version** before writing code, since the LiveKit Python SDK changes between releases.
3. **OBS (optional, only if time remains, do not block on it):** OBS 30+ can stream via WHIP into a LiveKit Ingress, and `obsws-python` can switch scenes from `set_screen_view`. Implement behind the same interface; skip if the Python publisher works.

### Data channel (`channel/`)
- Up: on `report_action`, publish JSON on topic `agent_events` (reliable).
- Down: subscribe to `data_received` on topic `human_decisions`, validate, enqueue for `get_human_response`.
- Mock (`ESCALATION_MODE=mock`): write events to `runs/<id>/outbox.jsonl`; tail `runs/<id>/inbox.jsonl` for decisions. Provide `python -m escalation_mcp.simulate <incident_id> approve|reject|stop|"free text"` that appends to the inbox, so the whole loop is testable without a phone.

---

## 4. LiveKit room ownership

Local creates the room identity for each incident:
- `room = "esc-<incident_id>"`
- Mint a token for `local-screen` (publish video + data, subscribe data) using `LIVEKIT_API_KEY/SECRET`.
- Put `livekit_url` and `room` in the escalation packet. **Never put the API secret in the packet.** The cloud agent uses the same LiveKit project's credentials to mint caller tokens for the group call.
- Dev helper: `python -m escalation_mcp.viewer_token <incident_id>` prints a viewer token + room so a teammate can join from `https://meet.livekit.io` (custom server) and verify the screen track and data channel **without Chert**.

---

## 5. Branch B: hand-off contract with the cloud Chert agent

This is a **proposed contract**. Put it in `escalation_mcp/CONTRACT.md` so the cloud-side teammate can confirm or change it. All field names live in one Pydantic module (`escalation_mcp/models.py`) so a change is one edit.

**Request:** `POST {CHERT_AGENT_URL}/escalations`, header `Authorization: Bearer {ESCALATION_SHARED_SECRET}`, timeout 10 s, 2 retries with backoff. On failure: keep running (screen share still works), return a warning, and save the packet to `runs/<id>/packet.json` so it can be resent.

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
  "details": { "traceback": "...", "log_tail": ["..."], "metrics_tail": [], "config": {}, "git_diff": "..." },
  "media": {
    "transport": "livekit",
    "livekit_url": "wss://<project>.livekit.cloud",
    "room": "esc-inc_20261114_153012_a3f9",
    "screen_participant": "local-screen",
    "preview_url": "http://127.0.0.1:8765 (local only, informational)"
  },
  "channels": { "events_topic": "agent_events", "decisions_topic": "human_decisions" },
  "contacts": ["+1...", "+1..."]
}
```
- `contacts` comes from `ESCALATION_CONTACTS` (comma-separated) and is optional; the cloud side may ignore it and use its own list.
- Final report: `POST {CHERT_AGENT_URL}/escalations/{incident_id}/report` with `{outcome, summary, ended_at, actions: [...]}`.
- Also mirror each `agent_events` message to `POST .../escalations/{id}/events` **only if** `ESCALATION_HTTP_EVENTS=1` (fallback for when the cloud agent is not in the LiveKit room).

**Data-channel messages:**
```json
// agent_events (local -> cloud)
{"type":"action","incident_id":"...","seq":7,"ts":"ISO","action":"edit_file","detail":"config.yaml lr 5.0 -> 3e-4","status":"done"}
// human_decisions (cloud -> local)
{"type":"decision","incident_id":"...","ts":"ISO","decision":"approve|reject|stop|instruction","text":"lower the LR and resume","from":"<participant name>"}
```
Ignore and log messages with an unknown `type` or mismatched `incident_id`.

### Redaction (mandatory before anything leaves the laptop)
`packet.py` redacts the packet and every event:
- Values of all variables in `.env` / process env whose names contain `KEY|SECRET|TOKEN|PASSWORD`.
- Common key patterns: `sk-…`, `xai-…`, `AIza…`, `ghp_…`, `Bearer …`, JWT-looking strings, private-key blocks.
- Caps: `log_tail` ≤ 200 lines, any single string ≤ 8 KB, whole packet ≤ 256 KB (truncate with a `"…[truncated]"` marker).
Unit-test redaction.

---

## 6. The skill file for the Grok bot: `skills/escalate.md`

Write the instructions the Grok bot loads. They must say, concisely:

1. **When to escalate:** the task failed and the bot either (a) can't find a confident fix, (b) the fix is destructive or irreversible (deleting files/data, force-push, killing or restarting a long job, spending money or compute), or (c) a human is explicitly required. Training NaN/crash/OOM, failing builds or tests that block a long pipeline, a process stuck >N minutes, and permission or credential prompts all qualify. Do **not** escalate for trivial errors the bot can fix safely itself.
2. **Protocol:**
   - Call `escalate_failure` once with a speakable `summary`, `severity`, `details`, and a `proposed_fix` if any. Prefer `share="window:<terminal or IDE title>"`.
   - Narrate the work with `report_action` **before and after every meaningful step** (read file, run command, edit, test).
   - **Between every step**, call `get_human_response(wait_seconds=0)`. On `stop`: halt immediately, revert uncommitted edits made during this incident, then `end_escalation(outcome="aborted")`. On `instruction`: follow it if safe, otherwise report why not.
   - **Before any destructive or irreversible action**, call `get_human_response(wait_seconds=25)` in a loop until `approve` or `reject`. Never treat silence as approval.
   - Use `set_screen_view` to show whatever the human needs to see (the traceback, the diff, a rendered plot).
   - Work on branch `agent/fix-<incident_id>`; never push or merge.
   - Finish with `end_escalation`.
3. A short worked example transcript (tool calls only) for the NaN-loss case.

Also add `escalation_mcp/grok_mcp_config.example.json` showing how to register the server (`command: "python", args: ["-m", "escalation_mcp"], cwd: <repo root>, env: {...}`). **Check the Grok bot's actual MCP config format and location** (look at its docs or `--help`); if you can't confirm it, say so in the README instead of guessing.

---

## 7. Configuration (append to `.env.example`)

```
ESCALATION_MODE=mock            # mock | live
CHERT_AGENT_URL=
ESCALATION_SHARED_SECRET=
ESCALATION_CONTACTS=            # comma-separated, optional
ESCALATION_HTTP_EVENTS=0
SCREEN_DEFAULT=screen           # screen | window:<title>
SCREEN_MONITOR=1
SCREEN_FPS=5
SCREEN_MAX_WIDTH=1280
PREVIEW_PORT=8765
```
`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` already exist; reuse them. Load with `python-dotenv`. In `live` mode, fail fast with a clear message if a required var is missing. In `mock` mode, nothing external is required.

Append to `requirements.txt`: `mcp`, `livekit`, `pillow`, `python-dotenv`, `httpx`, `pygetwindow; sys_platform == "win32"`.

---

## 8. Layout

```
escalation_mcp/
├── __init__.py
├── __main__.py            # python -m escalation_mcp → run MCP stdio server
├── server.py              # FastMCP app + the 5 tools
├── config.py              # env loading, mode
├── models.py              # Pydantic: EscalationPacket, ActionEvent, HumanDecision, FinalReport
├── packet.py              # build + redact + size caps + save
├── state.py               # active incident, event seq, decision queue, sticky stop
├── sink/{base,mock,http}.py
├── channel/{base,mock,livekit}.py
├── screen/
│   ├── capture.py
│   ├── overlay.py
│   └── backends/{base,preview,livekit}.py
├── livekit_room.py        # room naming, token minting, single shared Room connection
├── simulate.py            # python -m escalation_mcp.simulate <id> <decision|text>
├── viewer_token.py        # python -m escalation_mcp.viewer_token <id>
├── examples/fail_demo.py  # standalone script that fails after ~20s (don't depend on demo/train.py)
├── CONTRACT.md
├── README.md              # setup, mock walkthrough, live walkthrough, Grok config
└── grok_mcp_config.example.json
skills/escalate.md
tests/test_escalation_packet.py   # redaction + caps
tests/test_escalation_state.py    # decision queue, sticky stop, long-poll timeout
tests/test_escalation_tools.py    # tools end-to-end in mock mode (no network, no screen: inject a fake frame source)
```

---

## 9. Build order (each milestone must be demoable before the next)

1. **MCP skeleton, mock mode.** All 5 tools work against mock sink + mock channel; verify with `npx @modelcontextprotocol/inspector python -m escalation_mcp`. `simulate.py` round-trip works. Tests pass.
2. **Screen preview.** `escalate_failure` opens the localhost MJPEG preview with the overlay; `report_action` updates the banner within ~1 s; `set_screen_view` switches screen/window/image; `stop` shows the red bar.
3. **LiveKit.** Live mode joins `esc-<id>`, publishes the screenshare track, and sends/receives data-channel messages. Verify with `viewer_token.py` + meet.livekit.io from a phone browser, with no Chert involved.
4. **HTTP sink.** POST packet/report to `CHERT_AGENT_URL`; test against a throwaway local FastAPI echo server (put it in `escalation_mcp/dev_echo_server.py`).
5. **Grok bot integration.** `skills/escalate.md` + config example; run `examples/fail_demo.py`, let the Grok bot escalate, approve via `simulate.py`.

## 10. Definition of done
- `pytest` passes with no network and no display.
- Mock end-to-end: fail_demo → Grok bot (or MCP Inspector) calls `escalate_failure` → preview shows live overlay → `simulate approve` → bot proceeds → `end_escalation` → `runs/<id>/` contains `packet.json`, `agent_log.jsonl`, `outbox.jsonl`, `mcp.log`.
- Live: a phone in the LiveKit room sees the laptop screen with the action overlay at ≥3 fps and a data-channel `stop` reaches `get_human_response` in <1 s.
- No secrets in any file under `runs/` or any outbound request (grep test).

## 11. Known limitations to document in the README (don't try to solve)
- Interrupt granularity is **between tool calls**: MCP cannot preempt the Grok bot mid-command. A long shell command finishes before `stop` is seen. (Mitigation: the skill tells the bot to run long commands in the background and poll.)
- One active incident at a time.
- Window capture on macOS falls back to full screen.

## 12. Open questions: list these in `CONTRACT.md` for the team, don't block on them
1. Does the cloud Chert agent join the LiveKit room as a participant (so it can receive `agent_events` and send `human_decisions`), or should events go over HTTP?
2. Who mints FaceTime caller tokens: the cloud agent (needs LiveKit creds) or local (would need an inbound endpoint)? This prompt assumes the cloud agent does.
3. Is `agent/worker.py` (local Gemini call agent in MVP.md) still needed now that the call lives in the cloud?
