import os
from datetime import timedelta

from dotenv import load_dotenv
from livekit import api

from chert.client import place_call

load_dotenv()
ROOM = "smoke-test"


def token(identity: str) -> str:
    return (api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
            .with_identity(identity)
            .with_grants(api.VideoGrants(room_join=True, room=ROOM, can_publish=True, can_subscribe=True))
            .with_ttl(timedelta(hours=1))
            .to_jwt())


print("LIVEKIT_URL:", os.environ["LIVEKIT_URL"])
print("\nBROWSER (agent) TOKEN:\n" + token("agent"))
input("\nJoin the room in the browser with that token, then press Enter to place the call...")
print(place_call(os.environ["USER_PHONE"], token("chert-bridge"), agent_identity="agent"))