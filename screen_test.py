"""Smoke test: agent publishes silent audio + 1280x720 H.264 video (screen or pattern), places Chert call,
then prints Chert's media status.

  python3 screen_test.py           # stream your screen
  python3 screen_test.py pattern   # stream cycling colors (pipeline test)
"""
import asyncio
import json
import os
import sys
import urllib.request
from datetime import timedelta

import mss
import numpy as np
from dotenv import load_dotenv
from livekit import api, rtc

from chert.client import place_call

load_dotenv()
ROOM = "screen-test"
URL = os.environ["LIVEKIT_URL"]
PATTERN = len(sys.argv) > 1 and sys.argv[1] == "pattern"

W, H, FPS = 1280, 720, 15
SR, CH, FRAME_MS = 48000, 1, 20
SAMPLES = SR * FRAME_MS // 1000


def token(identity: str) -> str:
    return (api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
            .with_identity(identity)
            .with_grants(api.VideoGrants(room_join=True, room=ROOM, can_publish=True, can_subscribe=True))
            .with_ttl(timedelta(hours=1)).to_jwt())


def resize_nn(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """Nearest-neighbor resize, no extra deps."""
    ys = (np.arange(h) * img.shape[0] / h).astype(int)
    xs = (np.arange(w) * img.shape[1] / w).astype(int)
    return img[ys][:, xs]


def grab(sct, mon) -> np.ndarray:
    img = resize_nn(np.array(sct.grab(mon)), W, H)
    img = np.ascontiguousarray(img)
    img[:, :, 3] = 255
    return img


def pattern(t: int) -> np.ndarray:
    img = np.zeros((H, W, 4), dtype=np.uint8)
    img[:, :, (t // FPS) % 3] = 255
    img[:, :, 3] = 255
    return img


async def pump_silence(source: rtc.AudioSource):
    t = 0
    while True:
        n = np.arange(t, t + SAMPLES)
        tone = (0.1 * 32767 * np.sin(2 * np.pi * 440 * n / SR)).astype(np.int16)
        await source.capture_frame(rtc.AudioFrame(tone.tobytes(), SR, CH, SAMPLES))
        t += SAMPLES


async def pump_video(source: rtc.VideoSource, sct, mon):
    t = 0
    while True:
        img = pattern(t) if PATTERN else grab(sct, mon)
        source.capture_frame(rtc.VideoFrame(W, H, rtc.VideoBufferType.BGRA, img.tobytes()))
        t += 1
        await asyncio.sleep(1 / FPS)


def call_status(call_id: str) -> None:
    req = urllib.request.Request(
        f"https://facetime.trychert.com/api/v1/calls/{call_id}",
        headers={"Authorization": "Bearer " + os.environ["CHERT_API_KEY"],
                 "X-Chert-Project-Id": os.environ["CHERT_PROJECT_ID"]})
    d = json.loads(urllib.request.urlopen(req).read())["data"]
    print("\n--- chert status ---")
    print("status:", d["status"], "| reason:", d["terminal_reason"], d["terminal_detail"])
    print("media_status:", json.dumps(d["media_status"], indent=2))
    for e in d["facetime_call_events"]:
        print(" ", e["event_type"], e["payload"])


async def report(call_id: str):
    await asyncio.sleep(20)  # answer the call within this window
    try:
        call_status(call_id)
    except Exception as e:
        print("status check failed:", e)


async def main():
    room = rtc.Room()
    await room.connect(URL, token("agent"))

    a_src = rtc.AudioSource(SR, CH)
    a_track = rtc.LocalAudioTrack.create_audio_track("agent-audio", a_src)
    await room.local_participant.publish_track(
        a_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))

    v_src = rtc.VideoSource(W, H)
    v_track = rtc.LocalVideoTrack.create_video_track("agent-video", v_src)
    await room.local_participant.publish_track(
        v_track, rtc.TrackPublishOptions(
            source=rtc.TrackSource.SOURCE_CAMERA,
            video_codec=rtc.VideoCodec.H264,
            simulcast=False))

    with mss.MSS() as sct:
        mon = sct.monitors[1]
        print(f"publishing audio + {'pattern' if PATTERN else 'screen'} {W}x{H} H.264")
        tasks = [asyncio.create_task(pump_silence(a_src)),
                 asyncio.create_task(pump_video(v_src, sct, mon))]
        await asyncio.sleep(1)
        call = place_call(os.environ["USER_PHONE"], token("chert-bridge"), agent_identity="agent")
        print("call id:", call["id"])
        tasks.append(asyncio.create_task(report(call["id"])))
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass