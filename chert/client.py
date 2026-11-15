import json
import os
import urllib.error
import urllib.request
import uuid

from pathlib import Path

QUEUE_FILE = Path("runs/.chert_queue_id")


def cancel_stale_queue() -> None:
    if not QUEUE_FILE.exists():
        return
    qid = QUEUE_FILE.read_text().strip()
    req = urllib.request.Request(f"{FACETIME_BASE}/calls/queue/{qid}", method="DELETE", headers=_headers())
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"[chert] cancelled stale queue entry {qid}")
    except urllib.error.HTTPError:
        pass
    QUEUE_FILE.unlink(missing_ok=True)

FACETIME_BASE = "https://facetime.trychert.com/api/v1"


def _headers(idem: bool = False) -> dict:
    h = {"Authorization": f"Bearer {os.environ['CHERT_API_KEY']}",
         "X-Chert-Project-Id": os.environ["CHERT_PROJECT_ID"],
         "Content-Type": "application/json"}
    if idem:
        h["Idempotency-Key"] = f"call-{uuid.uuid4().hex}"
    return h


import time


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{FACETIME_BASE}{path}", headers=_headers())
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["data"]


def place_call(destination: str, participant_token: str, agent_identity: str = "agent",
               video: bool = True, max_wait_s: float = 120) -> dict:
    is_phone = destination.startswith("+")
    cancel_stale_queue()
    body = json.dumps({
        "direction": "outbound",
        "mode": "customer_managed",
        "line_id": os.environ["CHERT_LINE_ID"],
        "destination": destination,
        "destination_type": "phone" if is_phone else "email",
        "media": {"audio": True, "video": video},
        "livekit_url": os.environ["LIVEKIT_URL"],
        "participant_token": participant_token,
        "remote_participant_identity": agent_identity,
        "when_busy": "queue",
    }).encode()
    headers = _headers(idem=True)  # SAME key reused across queue retries
    deadline = time.time() + max_wait_s

    while True:
        req = urllib.request.Request(f"{FACETIME_BASE}/calls", data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())["data"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Chert {e.code}: {e.read().decode()[:500]}") from None

        if not data.get("queued"):
            QUEUE_FILE.unlink(missing_ok=True)
            call = data.get("call") or data
            if "id" not in call and (data.get("queue") or {}).get("call_id"):
                call = _get(f"/calls/{data['queue']['call_id']}")
            if "id" not in call:
                raise RuntimeError(f"unexpected Chert response: {json.dumps(data)[:400]}")
            return call
        if (data.get("queue") or {}).get("call_id"):          # queue already turned into a call
            QUEUE_FILE.unlink(missing_ok=True)
            return _get(f"/calls/{data['queue']['call_id']}")

        q = data.get("queue") if isinstance(data.get("queue"), dict) else {}
        queue_id = data.get("queue_id") or q.get("id") or q.get("queue_id")
        if not queue_id:
            raise RuntimeError(f"Chert queue response missing id: {data!r}")

        QUEUE_FILE.parent.mkdir(exist_ok=True)
        QUEUE_FILE.write_text(str(queue_id))
        print(f"[chert] line busy, queued at position {q.get('position')} ...")
        while time.time() < deadline:
            time.sleep(3)
            # Keep queue_id; status payloads often omit id after the first poll.
            status = _get(f"/calls/queue/{queue_id}")
            if status.get("call_id"):
                QUEUE_FILE.unlink(missing_ok=True)
                return _get(f"/calls/{status['call_id']}")
            if status.get("state") in ("ready", "turn"):
                break
            if status.get("state") in ("expired", "cancelled", "failed"):
                raise RuntimeError(f"Chert queue {status.get('state')}")
            if status.get("position") is not None:
                print(f"[chert] still queued at position {status.get('position')} ...")
        else:
            raise RuntimeError("Chert queue wait timed out")
        # loop: re-POST with the same Idempotency-Key to claim our turn


def end_call(call_id: str) -> None:
    req = urllib.request.Request(f"{FACETIME_BASE}/calls/{call_id}/end", data=b"{}", method="POST",
                                 headers=_headers(idem=True))
    try:
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        if e.code not in (404, 409):
            raise RuntimeError(f"Chert {e.code}: {e.read().decode()[:300]}") from None
