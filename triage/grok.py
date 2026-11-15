"""Grok (xAI) triage: Incident + skills/watcher.md -> Triage. Owner: Binoy.

xAI OpenAI-compatible chat completions. Env: XAI_API_KEY, XAI_MODEL.
Falls back to rule-based triage if no key / request fails.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

from incident.models import Fix, Incident, Triage
from triage.prompts import system_prompt, user_prompt

XAI_URL = "https://api.x.ai/v1/chat/completions"


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in model output")
    return json.loads(m.group(0))


def fallback_triage(inc: Incident) -> Triage:
    lr = inc.config.get("lr")
    if inc.type == "nan":
        changes = {"lr": lr / 10} if isinstance(lr, (int, float)) else {}
        return Triage(summary=f"Loss went NaN at step {inc.step}.",
                      cause="Learning rate likely too high, causing divergence.",
                      fix=Fix(kind="config", changes=changes), severity="HIGH")
    if inc.type == "oom":
        bs = inc.config.get("batch_size")
        changes = {"batch_size": max(1, bs // 2)} if isinstance(bs, int) else {}
        return Triage(summary=f"Out of memory at step {inc.step}.",
                      cause="Batch too large for available GPU memory.",
                      fix=Fix(kind="config", changes=changes), severity="HIGH")
    if inc.type == "crash":
        last = (inc.traceback or "").strip().splitlines()[-1:] or ["unknown error"]
        return Triage(summary=f"Training crashed at step {inc.step}.", cause=last[0],
                      fix=Fix(kind="code"), severity="HIGH")
    if inc.type == "plateau":
        changes = {"lr": lr * 0.5} if isinstance(lr, (int, float)) else {}
        return Triage(summary="Loss has stopped improving.", cause="Learning rate may be too high to settle.",
                      fix=Fix(kind="config", changes=changes), severity="LOW")
    return Triage(summary="Validation loss rising while training loss falls.", cause="Overfitting.",
                  fix=Fix(kind="config", changes={}), severity="LOW")


def grok_triage(inc: Incident, timeout: float = 30.0) -> Triage:
    key = os.getenv("XAI_API_KEY")
    if not key:
        return fallback_triage(inc)
    body = {
        "model": os.getenv("XAI_MODEL", "grok-4"),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": user_prompt(inc)},
        ],
    }
    req = urllib.request.Request(XAI_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        content = data["choices"][0]["message"]["content"]
        t = Triage.model_validate(_extract_json(content))
        if inc.default_severity() == "HIGH":
            t.severity = "HIGH"  # never downgrade crash/nan/oom
        return t
    except Exception as e:
        print(f"[triage] Grok failed ({e!r}); using fallback")
        return fallback_triage(inc)
