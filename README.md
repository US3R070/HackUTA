# Argos: the watchdog

**You start an 8-hour training run and go to bed. Twelve minutes later the loss hits NaN.** The GPU sits idle all night, and you don't find out until morning. Monitoring dashboards don't help when nobody is watching them.

Argos watches your ML training runs for you. When a run breaks, it FaceTimes you, shares your laptop screen live on the call, explains what went wrong, and applies the fix once you say yes.

> Named after Odysseus's dog, who waited 20 years and was the first to recognize his master when he came home.

## How it works

1. **Detect.** A Python watcher tails your training logs and `metrics.jsonl`, and catches crashes, NaN loss, out-of-memory errors and plateaus.
2. **Diagnose.** A local Grok coding agent reads the failure and the repo, finds the likely cause, and drafts a fix on an `agent/fix-<id>` branch.
3. **Call.** Grok escalates through our MCP server. Chert places a FaceTime call to your phone and bridges it into a LiveKit room.
4. **Talk.** On the call, Mentor (a voice agent built on ElevenLabs Agents with Gemini) explains the problem, shows your code, logs or loss curve on the shared screen, and talks through the fix with you.
5. **Fix.** Once you approve out loud, your decision goes back to Grok, which applies the fix and resumes training from the last checkpoint.

Argos never changes anything without your explicit yes.

```
train.py ──► watcher ──► Grok (diagnose + draft fix)
                              │ escalate (MCP)
                              ▼
                 Chert ──► FaceTime ──► LiveKit room
                                          ├─ screen share (code / logs / loss plot)
                                          └─ Mentor voice agent (ElevenLabs + Gemini)
                                                │ "yes, fix it"
                                                ▼
                              Grok applies fix ──► resume from checkpoint
```

## Tech stack

| Part | Tech |
|---|---|
| Failure detection | Python watcher + detectors |
| Diagnosis and code fixes | Grok (xAI), guided by `skills/` |
| Escalation | MCP server (`escalation_mcp/`) |
| Phone call | Chert (FaceTime bridge) |
| Real-time media | LiveKit (audio, screen share, data channel) |
| Voice conversation | ElevenLabs Agents + Gemini |

## Getting started

```bash
# macOS / Linux
python -m venv .venv && source .venv/bin/activate

# Windows
python -m venv .venv && .venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env   # add your API keys (Windows: copy .env.example .env)
```

Run the hands-off demo (works on macOS and Windows):

```bash
python demo/run_hands_off.py
```

A training run with a learning rate that's too high goes NaN, your phone rings, and you can approve the fix on the call.

## Repo layout

| Path | Purpose |
|---|---|
| `watcher/` | Runs training, tails logs, detects failures |
| `incident/` | Incident schema, builder and store |
| `triage/` | Calls Grok (or Gemini as a fallback) to diagnose an incident |
| `escalation_mcp/` | MCP server: places the call, shares the screen, runs the voice agent, returns your decision |
| `chert/` | Chert API client and webhooks |
| `agent/` | Call agent persona, tools and screen capture |
| `fixer/` | Applies the fix, dry runs, resumes and verifies |
| `skills/` | Agent skills for diagnosis, escalation and the Mentor voice |
| `demo/` | Demo training script, configs and launchers |

## Safety

- Every fix goes on its own `agent/fix-<incident_id>` branch.
- Nothing destructive happens without a spoken or typed "yes."
- Every agent action is logged to `runs/<run_id>/agent_log.jsonl`.

## Team

Built at HackUTA 2026 by Binoy, Ming, Febri and Ron.
