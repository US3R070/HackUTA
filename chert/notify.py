
import json
import os
import urllib.request
import urllib.error

from incident.models import Incident

BASE = os.getenv("CHERT_BASE_URL", "https://console.trychert.com/api/v1")
SEND_PATH = os.getenv("CHERT_SEND_PATH", "/messages")  # confirm from docs


def send_imessage(to: str, text: str) -> dict:
    body = {"to": to, "from": os.getenv("CHERT_LINE_ID"), "text": text}  # confirm field names
    req = urllib.request.Request(
        BASE + SEND_PATH, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {os.environ['CHERT_API_KEY']}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Chert {e.code}: {e.read().decode()[:500]}") from None


def format_alert(inc: Incident) -> str:
    t = inc.triage
    if not t:
        return f"⚠️ Training run {inc.run_id}: {inc.type} at step {inc.step}."
    fix = ", ".join(f"{k}→{v}" for k, v in t.fix.changes.items()) or "code fix drafted"
    return (f"⚠️ {t.summary}\nCause: {t.cause}\nProposed fix: {fix}\n\n"
            f"Reply FIX to apply, or CALL to talk it through.")


def notify(inc: Incident) -> None:
    try:
        send_imessage(os.environ["USER_PHONE"], format_alert(inc))
        print(f"[chert] alert sent for {inc.id}")
    except Exception as e:
        print(f"[chert] send failed: {e!r}")