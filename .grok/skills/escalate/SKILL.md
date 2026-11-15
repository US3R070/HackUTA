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

1. Call `escalate_failure` **once** with a speakable `summary` (<15s), `severity` (`LOW`|`HIGH`), `details`, and `proposed_fix` if any. Prefer `share="screen"` for the FaceTime demo (or `window:<title>` for privacy).
2. Narrate with `report_action` **before and after every meaningful step** (read file, run command, edit, test).
3. **Autonomous wait (required):** Right after `escalate_failure`, enter a loop and call `get_human_response(wait_seconds=25)` repeatedly until you get a real decision (`approve` / `reject` / `stop` / `instruction`). Do **not** stop and ask the TUI user what to do next — the human is on FaceTime; their yes/no arrives via this tool (from the Mentor voice bridge).
   - On `none`: loop again immediately.
   - On `approve`: apply the proposed fix quickly, `report_action` started/done, then **speak on the call** (see below), then `end_escalation`.
   - On `reject`: do not edit; one short abort line via `speak_to_caller`; `end_escalation(outcome="aborted")`.
   - On `stop`: halt, revert uncommitted edits from this incident, one goodbye; `end_escalation(outcome="aborted")`.
   - On `instruction`: follow if safe; `report_action`; then keep looping for further decisions until approve/reject/stop.
4. Never treat silence / `none` as approval.
5. Use `set_screen_view` so the human sees what they need (traceback terminal, diff, rendered plot via `image:<path>`).
6. **Repo boundary (this HackUTA demo):** config fixes may edit `demo/configs/*.yaml` in this repo. For a separate train repo, use branch `agent/fix-<incident_id>` there instead. Never push or merge without approval.
7. **Call script (FaceTime live):** the voice bridge already plays a short “on it…” filler when approve is queued — do **not** add another thinking line.
   After the fix:
   1. `speak_to_caller`: `"Okay — it's fixed. Is there anything else you need?"` — wait `duration_s`
   2. `get_human_response(wait_seconds=8)` **once only** — never poll again after this
   3. On `none` / no / nothing else: `speak_to_caller` **once**: `"Sounds good. Hanging up — good luck with the run."` — wait `duration_s` — `end_escalation(outcome="resolved")` immediately
   Never repeat the goodbye. Never ask “are you still there?” Never say hang-up twice.
8. Only after `end_escalation` may you return control to the TUI user.

Long commands: run them in the background and poll `get_human_response` — MCP cannot interrupt mid-command.

## Example transcript (NaN loss) — tool calls only

```text
escalate_failure(
  title="Training run crashed: NaN loss at step 4200",
  summary="Loss became NaN at step 4200. Learning rate 5.0 looks too high.",
  failure_type="nan",
  severity="HIGH",
  details={"log_tail": ["step=4200 loss=nan lr=5.0"], "config": {"lr": 5.0}},
  proposed_fix="Lower lr to 3e-4 and resume from the step-4000 checkpoint.",
  share="window:PowerShell"
)

report_action(incident_id, "read_file", "configs/bad_lr.yaml", "started")
report_action(incident_id, "read_file", "configs/bad_lr.yaml", "done")
get_human_response(incident_id, wait_seconds=0)  # -> none

report_action(incident_id, "propose_edit", "lr 5.0 -> 3e-4", "started")
get_human_response(incident_id, wait_seconds=25)  # -> approve
report_action(incident_id, "edit_file", "configs/bad_lr.yaml", "done")

set_screen_view(incident_id, "image:runs/loss_after.png")
end_escalation(incident_id, "resolved", "Lowered lr to 3e-4; ready to resume.")
```
