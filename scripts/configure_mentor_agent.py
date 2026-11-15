"""Configure the ElevenLabs Mentor agent for the FaceTime voice loop.

Dry run (default) prints the planned changes; --apply writes them to the live
agent (main branch), backs up the previous config to runs/, and prints a
field-by-field diff of everything that changed so nothing is clobbered silently.

  python scripts/configure_mentor_agent.py            # dry run
  python scripts/configure_mentor_agent.py --apply    # write
  python scripts/configure_mentor_agent.py --restore runs/elevenlabs_agent_backup_<ts>.json
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

API = "https://api.elevenlabs.io"
AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID") or os.getenv("AGENT_ID") or ""
API_KEY = os.getenv("ELEVENLABS_API_KEY") or os.getenv("ELEVEN_API_KEY") or ""

# Keep in sync with escalation_mcp/voice/eleven_agent.py FIX_WAIT_S (75s).
SEND_TO_GROK_TIMEOUT_S = 80
TURN_TIMEOUT_S = 20.0
TEMPERATURE = 0.5

DECISION_DESCRIPTION = (
    "The caller's decision. approve: a clear yes to the proposed fix (yes, do it, go ahead). "
    "reject: a clear no to the fix. stop: halt everything and revert. "
    "instruction: ONLY an explicit request to make a different change (e.g. 'use one e minus "
    "three instead', 'switch to AdamW'). Questions are NOT instructions ('should we use another "
    "optimizer?', 'why that number?') - answer those yourself without calling this tool."
)

# Client tools handled in escalation_mcp/voice/eleven_agent.py (code camera).
CAMERA_TOOLS: dict[str, tuple[str, dict[str, str], list[str]]] = {
    "show_code": (
        "Zoom the caller's screen onto one highlighted line in their editor. Use when it "
        "helps the explanation or when they ask to see code or config.",
        {"query": "A 'path:line' from code_anchors in the incident context (e.g. "
                  "'demo/train.py:165'), or a short exact code snippet like 'lr:'."},
        ["query"],
    ),
    "show_full_screen": (
        "Zoom the caller's screen back out to their whole screen.",
        {"reason": "Optional short reason, e.g. 'done with code'."},
        [],
    ),
}
CAMERA_TOOL_TIMEOUT_S = 10

AGENT_PATCH: dict[str, Any] = {
    "conversation_config": {
        "turn": {"turn_timeout": TURN_TIMEOUT_S},
        "agent": {
            "prompt": {
                "temperature": TEMPERATURE,
                "built_in_tools": {
                    "end_call": {
                        "type": "system",
                        "name": "end_call",
                        "description": (
                            "End the call after the caller says they're done and you've "
                            "said one short goodbye. Never while Grok is still applying a change."
                        ),
                        "params": {"system_tool_type": "end_call"},
                    },
                    "skip_turn": {
                        "type": "system",
                        "name": "skip_turn",
                        "description": (
                            "Stay silent when the speech is a side conversation not "
                            "directed at you."
                        ),
                        "params": {"system_tool_type": "skip_turn"},
                    },
                },
            }
        },
    },
    "platform_settings": {
        "overrides": {
            "conversation_config_override": {
                "agent": {"prompt": {"prompt": True}, "first_message": True}
            }
        }
    },
}


def _req(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={"xi-api-key": API_KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:800]}") from None


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        out[prefix] = json.dumps(obj, sort_keys=True)[:200]
    else:
        out[prefix] = obj
    return out


def _diff(before: Any, after: Any, label: str) -> int:
    fb, fa = _flatten(before), _flatten(after)
    changed = [k for k in sorted(set(fb) | set(fa)) if fb.get(k) != fa.get(k)]
    ignore = ("version_id", "metadata.", "access_info", "updated_at")
    changed = [k for k in changed if not any(k.startswith(i) for i in ignore)]
    print(f"\n== {label}: {len(changed)} field(s) changed")
    for k in changed:
        print(f"  {k}: {str(fb.get(k))[:90]!s}  ->  {str(fa.get(k))[:90]!s}")
    return len(changed)


def _summary(agent: dict, tool: dict) -> None:
    cc = agent["conversation_config"]
    pr = cc["agent"]["prompt"]
    ov = (
        agent["platform_settings"].get("overrides", {})
        .get("conversation_config_override", {})
        .get("agent", {})
    )
    bit = pr.get("built_in_tools") or {}
    print(
        f"  prompt override={ov.get('prompt', {}).get('prompt')}  first_message override="
        f"{ov.get('first_message')}\n"
        f"  send_to_grok timeout={tool['tool_config'].get('response_timeout_secs')}s  "
        f"end_call={'on' if bit.get('end_call') else 'off'}  "
        f"skip_turn={'on' if bit.get('skip_turn') else 'off'}\n"
        f"  turn_timeout={cc['turn'].get('turn_timeout')}s  temperature={pr.get('temperature')}"
    )


def _tool_id(agent: dict) -> str:
    ids = agent["conversation_config"]["agent"]["prompt"].get("tool_ids") or []
    for tid in ids:
        t = _req("GET", f"/v1/convai/tools/{tid}")
        if t.get("tool_config", {}).get("name") == "send_to_grok":
            return tid
    raise SystemExit("send_to_grok tool not found on agent")


def _client_tool_config(template: dict, name: str, description: str,
                        props: dict[str, str], required: list[str]) -> dict:
    """Build a client tool config using send_to_grok's config for field shapes."""
    cfg = copy.deepcopy(template)
    cfg.update(name=name, description=description, response_timeout_secs=CAMERA_TOOL_TIMEOUT_S)
    params = copy.deepcopy(template.get("parameters") or {"type": "object"})
    tprops = params.get("properties") or {}
    base = copy.deepcopy(tprops.get("text") or next(iter(tprops.values()), {"type": "string"}))
    base.pop("enum", None)
    params["properties"] = {}
    for pname, pdesc in props.items():
        prop = copy.deepcopy(base)
        prop.update(type="string", description=pdesc)
        params["properties"][pname] = prop
    params["required"] = required
    cfg["parameters"] = params
    return cfg


def _agent_tool_names(agent: dict) -> set[str]:
    tools = agent["conversation_config"]["agent"]["prompt"].get("tools") or []
    return {t.get("name") for t in tools}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore", help="backup JSON written by a previous --apply")
    args = ap.parse_args()
    if not AGENT_ID or not API_KEY:
        raise SystemExit("ELEVENLABS_AGENT_ID and ELEVENLABS_API_KEY must be set in .env")

    agent = _req("GET", f"/v1/convai/agents/{AGENT_ID}")
    branch = agent.get("main_branch_id") or agent.get("branch_id")
    q = f"?branch_id={branch}" if branch else ""
    tid = _tool_id(agent)
    tool = _req("GET", f"/v1/convai/tools/{tid}")
    print(f"agent: {agent.get('name')} ({AGENT_ID[:16]}…) branch={branch}")
    print("current:")
    _summary(agent, tool)

    if args.restore:
        backup = json.loads(Path(args.restore).read_text())
        _req("PATCH", f"/v1/convai/tools/{tid}", {"tool_config": backup["tool"]["tool_config"]})
        _req(
            "PATCH",
            f"/v1/convai/agents/{AGENT_ID}{q}",
            {
                "conversation_config": {
                    "turn": backup["agent"]["conversation_config"]["turn"],
                    "agent": {"prompt": {
                        k: backup["agent"]["conversation_config"]["agent"]["prompt"][k]
                        for k in ("temperature", "built_in_tools", "tool_ids")
                    }},
                },
                "platform_settings": {"overrides": backup["agent"]["platform_settings"]["overrides"]},
            },
        )
        print("restored; now:")
        _summary(_req("GET", f"/v1/convai/agents/{AGENT_ID}"), _req("GET", f"/v1/convai/tools/{tid}"))
        return

    new_tool_cfg = copy.deepcopy(tool["tool_config"])
    new_tool_cfg["response_timeout_secs"] = SEND_TO_GROK_TIMEOUT_S
    # Always speak before the tool runs: the agent narrates the fix while Grok works.
    new_tool_cfg["force_pre_tool_speech"] = True
    props = new_tool_cfg.get("parameters", {}).get("properties", {})
    if "decision" in props:
        props["decision"]["description"] = DECISION_DESCRIPTION

    print("\nplanned:")
    print(f"  send_to_grok timeout -> {SEND_TO_GROK_TIMEOUT_S}s; force pre-tool speech; decision description tightened")
    print(f"  turn_timeout -> {TURN_TIMEOUT_S}s; temperature -> {TEMPERATURE}")
    print("  end_call + skip_turn -> on; prompt + first_message overrides -> allowed")
    missing_camera = [n for n in CAMERA_TOOLS if n not in _agent_tool_names(agent)]
    print(f"  client tools to create + attach: {missing_camera or 'none (already on agent)'}")
    if not args.apply:
        print("\n(dry run; pass --apply to write)")
        return

    backup_path = ROOT / "runs" / f"elevenlabs_agent_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
    backup_path.parent.mkdir(exist_ok=True)
    backup_path.write_text(json.dumps({"agent": agent, "tool": tool}, indent=2))
    print(f"\nbackup: {backup_path.relative_to(ROOT)}")

    _req("PATCH", f"/v1/convai/tools/{tid}", {"tool_config": new_tool_cfg})

    patch = copy.deepcopy(AGENT_PATCH)
    if missing_camera:
        tool_ids = list(agent["conversation_config"]["agent"]["prompt"].get("tool_ids") or [])
        for name in missing_camera:
            desc, props, required = CAMERA_TOOLS[name]
            created = _req("POST", "/v1/convai/tools", {"tool_config": _client_tool_config(
                new_tool_cfg, name, desc, props, required)})
            print(f"created client tool {name}: {created.get('id')}")
            tool_ids.append(created["id"])
        patch["conversation_config"]["agent"]["prompt"]["tool_ids"] = tool_ids
    _req("PATCH", f"/v1/convai/agents/{AGENT_ID}{q}", patch)

    agent_after = _req("GET", f"/v1/convai/agents/{AGENT_ID}")
    tool_after = _req("GET", f"/v1/convai/tools/{tid}")
    _diff(tool, tool_after, "send_to_grok tool")
    _diff(agent, agent_after, "agent")
    print("\nnow (live main branch):")
    _summary(agent_after, tool_after)


if __name__ == "__main__":
    sys.exit(main())
