# Escalation MCP

Local MCP server the Grok bot calls when a failure needs a human. It starts a laptop screen share (with a live action overlay), packages an escalation packet for the cloud Chert agent, and returns human decisions (`approve` / `reject` / `stop` / free-text) as a tool.

Calling / FaceTime / iMessage live entirely in the cloud agent. This package does **not** place calls.

## Quick setup

```bash
# from repo root
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # set ESCALATION_MODE=mock for local work
```

## Mock walkthrough (no network, no phone)

1. Start the MCP Inspector (or let Grok spawn the server):

   ```bash
   npx @modelcontextprotocol/inspector python -m escalation_mcp
   ```

2. Call `escalate_failure` with a title/summary/`severity=HIGH` / `share=screen` (or `share=none` in CI).
3. Open the returned `preview_url` in a browser (MJPEG + overlay).
4. Call `report_action` a few times — the bottom banner updates.
5. In another terminal:

   ```bash
   python -m escalation_mcp.simulate <incident_id> approve
   ```

6. Call `get_human_response` → `decision=approve`, `call_state=watching`. The preview keeps streaming.
7. Simulate the rest of the call:

   ```bash
   python -m escalation_mcp.simulate <incident_id> join      # you picked up
   python -m escalation_mcp.simulate <incident_id> "also drop batch 4200"
   python -m escalation_mcp.simulate <incident_id> hangup    # "that's all, bye"
   python -m escalation_mcp.simulate <incident_id> leave     # you hung up the phone
   ```

8. Call `end_escalation`. Check `runs/<incident_id>/` for `packet.json`, `agent_log.jsonl`, `outbox.jsonl` (includes `call_state` events), `mcp.log`.

### Call lifecycle

Decisions no longer tear down the stream. The call ends on a verbal `hangup`, the caller leaving (`CALL_LEAVE_GRACE_S`, default 5 s, to rejoin), or a timer:

| Env | Default | Meaning |
|---|---|---|
| `CALL_NO_ANSWER_S` | 60 | wait for pickup per attempt (starts after Chert accepts the call; never redials over a call Chert still reports as active) |
| `CALL_MAX_ATTEMPTS` | 2 | total dial attempts (1 redial) |
| `CALL_IDLE_TIMEOUT_S` | 120 | after the first decision, hang up after this long with no new one (demo: 30–45). Before any decision there is no timer |
| `CALL_DONE_GRACE_S` | 30 | keep the monitor up after `end_escalation` |
| `CALL_LEAVE_GRACE_S` | 5 | caller can rejoin within this window |

If the call ends without any human decision, `get_human_response` returns `run_default_action: true` and the agent performs its declared, non-destructive `default_action`. The overlay shows `WATCHING · You: approve · auto-hangup 1:42`.

Standalone failure demo:

```bash
python -m escalation_mcp.examples.fail_demo
```

## Live walkthrough

1. Fill `.env`: `ESCALATION_MODE=live`, `LIVEKIT_*`, `CHERT_AGENT_URL`, `ESCALATION_SHARED_SECRET`.
2. Start the MCP (or Grok). Call `escalate_failure`.
3. Mint a viewer token and join from a phone browser:

   ```bash
   python -m escalation_mcp.viewer_token <incident_id>
   # paste into https://meet.livekit.io (custom server)
   ```

4. Confirm the screenshare track (≥3 fps) and send a data-channel `stop` / use the cloud agent for `human_decisions`.

HTTP sink smoke test against the echo server:

```bash
uvicorn escalation_mcp.dev_echo_server:app --port 8787
# CHERT_AGENT_URL=http://127.0.0.1:8787 ESCALATION_SHARED_SECRET=dev
```

## Grok bot configuration

Grok's native MCP config is **TOML**, not JSON. Confirmed from `~/.grok/docs/user-guide/07-mcp-servers.md`.

**Project scope (recommended)** — `.grok/config.toml` is portable (macOS + Windows).
It expands `${HACKUTA_PYTHON}` / `${HACKUTA_ROOT}` (set automatically by
`python demo/run_hands_off.py`):

```toml
[mcp_servers.escalation]
command = "${HACKUTA_PYTHON:-python}"
args = ["-m", "escalation_mcp"]
cwd = "${HACKUTA_ROOT:-.}"
enabled = true
startup_timeout_sec = 30
tool_timeout_sec = 60
tool_timeouts = { get_human_response = 60 }
env = { ESCALATION_MODE = "live", PYTHONPATH = "${HACKUTA_ROOT:-.}", PYTHONUTF8 = "1" }
```

Hands-off live call demo (either OS, from repo root):

```bash
# macOS / Linux
./demo/run_hands_off.sh
# Windows
demo\run_hands_off.cmd
# or any OS
python demo/run_hands_off.py
```

Or register via CLI from the repo root (after activating `.venv`):

```bash
# macOS / Linux
grok mcp add escalation --scope project -e ESCALATION_MODE=mock -- .venv/bin/python -m escalation_mcp
# Windows
grok mcp add escalation --scope project -e ESCALATION_MODE=mock -- .venv\Scripts\python.exe -m escalation_mcp
```

A JSON-shaped example for other hosts is in `grok_mcp_config.example.json`.

**Skill:** `skills/escalate.md` (team contract) and `.grok/skills/escalate/SKILL.md` (Grok discovery format).

Verify:

```bash
grok mcp list
grok mcp doctor escalation
```

Project MCP servers require folder trust. From the repo root once:

```bash
grok --trust -p "ok"
```

Pin `mcp>=1.0,<2` — MCP 2.x renamed FastMCP.

## Tools

| Tool | Purpose |
|------|---------|
| `escalate_failure` | Create incident, share screen, send packet |
| `report_action` | Overlay + `agent_events` narration |
| `get_human_response` | Poll / long-poll human decisions + `call_state` |
| `set_screen_view` | `screen` / `window:…` / `image:…` |
| `end_escalation` | Agent done; grace period if the human is still watching, then final report + tear down |

## Known limitations

- Interrupt granularity is **between tool calls**: MCP cannot preempt the Grok bot mid-command. Long shell commands finish before `stop` is seen. Mitigation: the skill tells the bot to run long commands in the background and poll.
- One active incident at a time.
- Window capture on macOS falls back to full screen.
