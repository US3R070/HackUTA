# Proactive Training Agent

Watches your ML training runs, texts you when one breaks, FaceTimes you when it's serious, shows your laptop screen live on the call, explains the problem out loud, and fixes it with your OK.

**Full spec: [MVP.md](MVP.md). Coding agents: read README.md and MVP.md before writing code.**

## How it works
```
LOCAL (laptop)
  demo/train.py ──logs + metrics.jsonl──► watcher (Python, detects crash/NaN/OOM/plateau)
        │ failure
        ▼
  incident builder ──► Grok IDE agent + skills/watcher.md
                        (diagnose, severity, draft fix on agent/fix-<id>)

OUTBOUND  (we -> Chert API)
  send iMessage alert:  "NaN at step 4.2k, LR too high. Reply FIX or CALL"
  place FaceTime call into our LiveKit room (on HIGH or CALL)

INBOUND   (Chert -> our FastAPI webhook, via ngrok/cloudflared)
  iMessage replies (FIX / CALL / text), call events

LIVE CALL (LiveKit room; NOT through FastAPI)
  call agent on laptop: STT -> Gemini (+tools) -> ElevenLabs voice
  publishes live screen track (repo / logs / loss plot) -> user's FaceTime
  optional: receives user's camera

FIX
  user approves by voice -> fixer applies Grok's fix -> resume from checkpoint
  -> verify loss healthy -> iMessage report (before/after)
```

## Simple pipeline
```
train.py
  -> Python watcher            (detects failure)
  -> Grok                      (diagnoses + drafts fix)
  -> Chert iMessage            (texts you the alert)
  -> Chert FaceTime -> LiveKit room
  -> Our call agent (laptop):
       ears:  speech-to-text
       brain: Gemini           (talks, sees screen + camera, calls tools)
       voice: ElevenLabs
       eyes out: live screen track
  -> Fixer                     (applies Grok's fix, resumes run)
  -> Chert iMessage            (final report)
```

## Roles
- **Chert:** the phone pipe only (iMessage + FaceTime bridge into LiveKit). Not the brain.
- **Grok (xAI):** IDE coding agent: diagnosis + drafting code/config fixes (`skills/watcher.md`)
- **Gemini:** live call brain: conversation, tool use, sees the published screen frames + caller camera, writes the final report
- **ElevenLabs:** call voice (TTS)
- **LiveKit:** media room where our agent and the FaceTime caller meet

Pitch: *Grok writes the code, Gemini sees and talks.*

## Repo layout
| Path | Purpose | Owner |
|---|---|---|
| `watcher/` | run training, tail logs, detect failures | Binoy |
| `incident/` | Incident schema, builder, store | Binoy |
| `skills/watcher.md` | Grok agent skill: diagnose + draft fix | Binoy |
| `triage/grok.py` | invoke Grok with skill on an Incident | Binoy |
| `triage/gemini.py` | fallback triage | Binoy |
| `chert/` | Chert API client, iMessage, webhook, call escalation | Ming |
| `agent/` | LiveKit call agent: worker, tools, voice, screen, persona | Ming / Febri |
| `fixer/` | apply fix, dry run, resume, verify | Ron |
| `demo/` | demo training script + configs | Ron |
| `runs/` | generated logs, metrics, checkpoints (gitignored) | - |

## Contracts (don't change without telling the team)
- Incident schema: MVP.md section 4 -> `incident/models.py`
- Metrics: one JSON line per log step in `runs/<run_id>/metrics.jsonl`
- Fix branches: `agent/fix-<incident_id>`; nothing destructive without explicit "yes"
- All agent actions logged to `runs/<run_id>/agent_log.jsonl`

## Setup
```bash
# macOS / Linux
python -m venv .venv && source .venv/bin/activate
# Windows
python -m venv .venv && .venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env   # fill keys (Windows: copy .env.example .env)
```

Laptop OS can be **macOS or Windows** — Chert bridges the phone (FaceTime) into LiveKit; screen share + MCP run locally. Hands-off demo: `python demo/run_hands_off.py` (or `demo/run_hands_off.sh` / `demo\run_hands_off.cmd`).

## Build order
1. Chert + LiveKit smoke test (call connects, screen visible on phone)
2. `demo/train.py` + `bad_lr.yaml` NaNs ~2 min in
3. Watcher + detectors -> Incident
4. Grok skill triage -> JSON + drafted fix
5. iMessage alert + reply webhook
6. Escalation call into LiveKit room
7. Call agent (voice + live screen + tools)
8. Fixer -> resume -> verify -> report
9. Wow layers

## Git rules
- Branch per area: `feat/<area>`; PR into `main`; keep `main` runnable.
- Never commit `.env`.
