---
name: escalate
description: Escalate a failed or risky task to a human via the escalation MCP (screen share + cloud call hand-off). Use when training crashes, NaN/OOM, blocked pipelines, destructive fixes, or the user must approve.
---

# Escalate skill (Grok IDE agent)

Use the **escalation** MCP when a task failed and you need a human in the loop. Tools are namespaced as `escalation__<tool>` in Grok (`search_tool` / `use_tool`).

## When to escalate

Escalate when **any** of these hold:

- You cannot find a confident fix.
- The fix is destructive or irreversible (delete files/data, force-push, kill/restart a long job, spend money or compute).
- A human is explicitly required (permissions, credentials, product decision).

Qualifying failures include: training NaN / crash / OOM, failing builds or tests that block a long pipeline, a process stuck longer than N minutes, permission or credential prompts.

Do **not** escalate trivial errors you can fix safely yourself.

## Protocol

1. Call `escalate_failure` **once** with a speakable `summary` (<15s), `severity` (`LOW`|`HIGH`), `proposed_fix`, and `details` that include a `rationale`: why this is the cause and why this exact fix/value is right (the voice agent explains this to the human). Prefer `share="screen"` for the phone demo (or `window:<title>` on Windows for privacy).
2. Narrate with `report_action` **before and after every meaningful step** (read file, run command, edit, test).
3. **The voice agent owns the call.** A live voice agent (Mentor) greets, explains, answers questions, says goodbye and hangs up. You never speak: do **not** use `speak_to_caller` while it is live.
4. **Autonomous wait (required):** right after `escalate_failure`, loop `get_human_response(wait_seconds=25)` until the call ends. Do **not** stop and ask the TUI user what to do — the human is on the phone; decisions arrive via this tool.
   - `none`: loop again immediately.
   - `approve`: apply the proposed fix **quickly** (the human is waiting on the line), `report_action` started/done, then `fix_done(incident_id, summary, applied=True)`. Keep looping.
   - `instruction`: follow it if safe, then `fix_done` with what you changed and why (or `applied=False` with the reason). Keep looping.
   - `reject`: do not edit. Keep looping.
   - `stop`: halt and revert uncommitted edits from this incident. Keep looping.
   - `hangup`: the call is over. `end_escalation(outcome="resolved"` if you applied a fix, else `"aborted"`, `summary=...)`, then return control.
5. **`fix_done` summary:** 1–2 plain sentences the voice agent will explain: what changed (file, old → new value), why that value, what the human should see next. Example: "Changed lr from 5.0 to 3e-4 in the bad-LR config; updates stay small and stable, so loss should drop smoothly within ~100 steps."
6. Never treat silence / `none` as approval.
7. Use `set_screen_view` so the human sees what they need (traceback terminal, diff, rendered plot via `image:<path>`).
8. **Repo boundary (this HackUTA demo):** config fixes may edit `demo/configs/*.yaml` in this repo. For a separate train repo, use branch `agent/fix-<incident_id>` there instead. Never push or merge without approval.
9. Only after `end_escalation` may you return control to the TUI user.
10. **No voice agent** (mock mode, or the escalation result warns voice failed): fall back to one `speak_to_caller` line after the fix and before `end_escalation`.

Long commands: run them in the background and poll `get_human_response` — MCP cannot interrupt mid-command.

## Example transcript (NaN loss) — tool calls only

```text
escalate_failure(
  title="Training run crashed: NaN loss at step 4200",
  summary="Loss became NaN at step 4200. Learning rate 5.0 looks too high.",
  failure_type="nan",
  severity="HIGH",
  details={"log_tail": ["step=4200 loss=nan lr=5.0"], "config": {"lr": 5.0},
           "rationale": "lr=5.0 overshoots every step so loss diverges to inf then NaN; 3e-4 is the standard Adam default, ~16,000x smaller, stable but still fast."},
  proposed_fix="Lower lr to 3e-4 and resume from the step-4000 checkpoint.",
  share="screen"
)
report_action(incident_id, "screen_share", "live", "done")
get_human_response(incident_id, wait_seconds=25)  # -> none
get_human_response(incident_id, wait_seconds=25)  # -> approve
report_action(incident_id, "edit_file", "configs/bad_lr.yaml", "started")
report_action(incident_id, "edit_file", "lr 5.0 -> 3e-4", "done")
fix_done(incident_id, "Changed lr from 5.0 to 3e-4 in bad_lr.yaml; updates stay stable, so loss should drop smoothly within ~100 steps.")
get_human_response(incident_id, wait_seconds=25)  # -> hangup (agent said goodbye)
end_escalation(incident_id, "resolved", "Lowered lr to 3e-4; ready to resume.")
```
