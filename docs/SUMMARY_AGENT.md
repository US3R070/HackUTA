# Change summary (for coding agents)

**Branch:** `Roni`  
**Commits:** `bc46091` (conversation flow) · `eefec89` (Windows portability)  
**Baseline (already present):** `ce11d3d` hands-off FaceTime Mentor loop (`send_to_grok` → decision queue → Grok `speak_to_caller` / edit / `end_escalation`)

---

## A. Post-approve voice UX (`bc46091`)

### Problem

1. After user says approve, Mentor said “I’ll apply it” then went idle → ElevenLabs ConvAI filled silence with “are you still there?”
2. After “anything else?” + user “no”, Mentor + Grok both spoke → repeated “anything else?” / double goodbye

### Fix — `escalation_mcp/voice/eleven_agent.py`

- `LiveKitAudioInterface._mute_agent_out` + `mute_agent_output()` — drop Mentor TTS chunks; `output()` no-ops when muted/stopped
- `_thinking_filler_then_park()` on `approve`:
  1. Mute Mentor
  2. TTS via `synthesize_pcm48`: *“Okay… on it. Give me a second while I apply that.”* → `room.play_pcm`
  3. Sleep ~85% of audio duration
  4. `_park_conversation()` — clear mic sink, `audio.stop()`, `end_session` / `wait_for_session_end`
- `_park_conversation()` also on `reject` / `stop` (no filler)
- `_send_to_grok` always returns **string** (never dict — dict → WS 1008)
- Tool result on approve/reject/stop: instruct Mentor to say nothing / stay silent
- `mentor_prompt_with_context`: after `send_to_grok`, stay silent (no still-there / goodbye)

**Post-park audio ownership:** only Grok `speak_to_caller` (ElevenLabs TTS → LiveKit `play_pcm`). ConvAI no longer listens → follow-up `get_human_response` usually times out to `none` (by design for demo hangup).

### Skill / prompt contract

Files:

- `.grok/skills/escalate/SKILL.md`
- `skills/escalate.md`
- `demo/hands_off_escalate_prompt.txt`

Rules:

- Do **not** speak a thinking line (bridge owns it)
- After fix: one line *“Okay — it's fixed. Is there anything else you need?”* → wait `duration_s`
- `get_human_response(wait_seconds=8)` **once**
- One goodbye → wait → `end_escalation` — never repeat goodbye / never “are you still there?”

### Call flow (intended)

```
User approve → send_to_grok(approve)
  → filler TTS + park ConvAI
  → Grok edits bad_lr / report_action
  → speak_to_caller("fixed… anything else?")
  → get_human_response(8s) once
  → speak_to_caller(goodbye once) → end_escalation
```

### Retest

Reset `demo/configs/bad_lr.yaml` lr to `1e-2` if needed, then:

```bash
python demo/run_hands_off.py
```

---

## B. macOS + Windows portability (`eefec89`)

### Problem

`.grok/config.toml` hard-coded Mac absolute paths; runner was bash-only.

### MCP config — `.grok/config.toml`

```toml
command = "${HACKUTA_PYTHON:-python}"
cwd = "${HACKUTA_ROOT:-.}"
env.PYTHONPATH = "${HACKUTA_ROOT:-.}"
```

Grok expands `${VAR:-default}` at load. Verified with `grok mcp doctor escalation`.

### New helpers / runners

| File | Role |
|------|------|
| `demo/platform_paths.py` | `venv_python` (win: `.venv/Scripts/python.exe`, unix: `.venv/bin/python` — **do not resolve symlink** or you lose venv site-packages), `find_grok_bin`, `export_hackuta_env` |
| `demo/run_hands_off.py` | chdir repo → export env → find grok → `--always-approve --fullscreen` + prompt; Win `.cmd`/`.bat` via `cmd /c` |
| `demo/run_hands_off.sh` | → venv/`python3` + `.py` |
| `demo/run_hands_off.cmd` / `.ps1` | Windows wrappers |

### Other

- `watcher/runner.py`: UTF-8 pipes; Win `CREATE_NO_WINDOW`
- `escalation_mcp/screen/capture.py`: window crop stays Win/`pygetwindow`; mac/linux → full screen
- Docs/skills: phone wording; setup for both OS (`README.md`, `escalation_mcp/README.md`)

### Architecture note

Phone = Chert FaceTime → LiveKit (OS-agnostic). Laptop = MCP + mss screenshare + ElevenLabs bridge (Mac or Win).

### Do not regress

- Keep `_send_to_grok` return type `str`
- Keep park-after-approve (idle prompts otherwise return)
- Keep Unix venv path un-resolved
- Hands-off needs `[ui] permission_mode = "always-approve"` / `--always-approve`
