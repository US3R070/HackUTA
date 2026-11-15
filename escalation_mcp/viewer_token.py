"""Print a LiveKit viewer token so a teammate can join meet.livekit.io.

Usage:
  python -m escalation_mcp.viewer_token <incident_id>
"""

from __future__ import annotations

import json
import sys

from escalation_mcp.config import load_settings
from escalation_mcp.livekit_room import mint_token, room_name_for


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 1:
        print(
            "Usage: python -m escalation_mcp.viewer_token <incident_id>",
            file=sys.stderr,
        )
        return 2

    incident_id = args[0]
    settings = load_settings(require_live=True)
    room = room_name_for(incident_id)
    token = mint_token(
        settings,
        identity="viewer-dev",
        room=room,
        can_publish=False,
        can_subscribe=True,
        can_publish_data=True,
    )
    print(
        json.dumps(
            {
                "livekit_url": settings.livekit_url,
                "room": room,
                "identity": "viewer-dev",
                "token": token,
                "meet_hint": "Open https://meet.livekit.io , custom server, paste URL + token",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
