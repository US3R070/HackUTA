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

1. Call `escalate_failure` **once** with a speakable `summary` (<15s), `severity` (`LOW`|`HIGH`), `details`, and `proposed_fix` if any. Prefer `share="window:<terminal or IDE title>"` for privacy.
2. Narrate with `report_action` **before and after every meaningful step** (read file, run command, edit, test).
3. **Between every step**, call `get_human_response(wait_seconds=0)`.
   - On `stop`: halt immediately, revert uncommitted edits made during this incident, then `end_escalation(outcome="aborted")`.
   - On `instruction`: follow it if safe; otherwise report why not via `report_action`.
4. **Before any destructive or irreversible action**, call `get_human_response(wait_seconds=25)` in a loop until `approve` or `reject`. Never treat silence as approval.
5. Use `set_screen_view` so the human sees what they need (traceback terminal, diff, rendered plot via `image:<path>`).
6. **Repo boundary:** training fixes belong in the **train** repo (`C:\Users\USER\HackUTA\train`), on branch `agent/fix-<incident_id>`. Never create training fix branches or commit train patches inside the HackUTA (`escalation_mcp`) repo. Never push or merge without approval.
7. Finish with `end_escalation` (`resolved` / `aborted` / `handed_off`). A real `get_human_response` decision may auto-reset preview on `:8765`.

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
