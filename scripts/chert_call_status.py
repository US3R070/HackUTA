"""Print Chert media/terminal status for a call id (or latest from mcp.log)."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    call_id = sys.argv[1] if len(sys.argv) > 1 else None
    if not call_id:
        logs = sorted(Path("runs").glob("inc_*/mcp.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for log in logs[:5]:
            text = log.read_text(errors="ignore")
            m = re.search(r"chert call ([0-9a-f-]+) placed", text)
            if m:
                call_id = m.group(1)
                print(f"using {call_id} from {log}")
                break
    if not call_id:
        raise SystemExit("usage: python scripts/chert_call_status.py <call_id>")

    req = urllib.request.Request(
        f"https://facetime.trychert.com/api/v1/calls/{call_id}",
        headers={
            "Authorization": f"Bearer {os.environ['CHERT_API_KEY']}",
            "X-Chert-Project-Id": os.environ["CHERT_PROJECT_ID"],
        },
    )
    d = json.loads(urllib.request.urlopen(req, timeout=15).read())["data"]
    print("status:", d.get("status"))
    print("terminal:", d.get("terminal_reason"), d.get("terminal_detail"))
    print("media_status:", json.dumps(d.get("media_status"), indent=2))
    for e in d.get("facetime_call_events") or []:
        print(" ", e.get("event_type"), e.get("payload"))


if __name__ == "__main__":
    main()
