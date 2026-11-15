"""PCM16 mono resampling helpers (LiveKit 48 kHz ↔ ElevenLabs 16 kHz)."""

from __future__ import annotations

import numpy as np


def resample_pcm16(pcm: bytes, src_hz: int, dst_hz: int) -> bytes:
    if not pcm or src_hz == dst_hz:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return b""
    n_dst = max(1, int(round(samples.size * (dst_hz / float(src_hz)))))
    x_old = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_dst, endpoint=False)
    out = np.interp(x_new, x_old, samples)
    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()
