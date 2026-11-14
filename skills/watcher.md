# Watcher skill (Grok IDE agent) — Owner: Binoy

Loaded by the Grok coding agent when the Python watcher detects a failure.

## Input
An Incident JSON (schema: MVP.md section 4) without `triage` filled in.

## Do
1. Read traceback, log tail, metrics tail, config, git diff, and relevant source files.
2. Diagnose: what broke, why (one sentence each).
3. Propose ONE minimal fix (config change preferred over code change).
4. Set severity: crash/oom/nan -> HIGH, plateau/overfit -> LOW (upgrade if justified).
5. Draft the fix on branch `agent/fix-<incident_id>`. Do NOT merge, resume, or run destructive commands.

## Output (strict JSON)
{"summary": "...", "cause": "...", "fix": {"kind": "config|code", "changes": {...}, "patch": "unified diff or null"}, "severity": "LOW|HIGH"}

## Rules
- Never apply the fix to the running job without explicit user approval (comes from the call agent).
- Keep summary speakable in under 15 seconds.
