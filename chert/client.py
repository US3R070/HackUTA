import json
import os
import urllib.error
import urllib.request
import uuid

FACETIME_BASE = "https://facetime.trychert.com/api/v1"


def _headers(idem: bool = False) -> dict:
    h = {"Authorization": f"Bearer {os.environ['CHERT_API_KEY']}",
         "X-Chert-Project-Id": os.environ["CHERT_PROJECT_ID"],
         "Content-Type": "application/json"}
    if idem:
        h["Idempotency-Key"] = f"call-{uuid.uuid4().hex}"
    return h


def place_call(destination: str, participant_token: str, agent_identity: str = "agent",
               video: bool = True) -> dict:
    is_phone = destination.startswith("+")
    body = {
        "direction": "outbound",
        "mode": "customer_managed",
        "line_id": os.environ["CHERT_LINE_ID"],
        "destination": destination,
        "destination_type": "phone" if is_phone else "email",
        "media": {"audio": True, "video": video},
        "livekit_url": os.environ["LIVEKIT_URL"],
        "participant_token": participant_token,
        "remote_participant_identity": agent_identity,
    }
    req = urllib.request.Request(f"{FACETIME_BASE}/calls", data=json.dumps(body).encode(),
                                 method="POST", headers=_headers(idem=True))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())["data"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Chert {e.code}: {e.read().decode()[:500]}") from None
