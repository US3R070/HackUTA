"""Draw live incident banners onto captured frames."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:  # noqa: BLE001
        try:
            return ImageFont.truetype("DejaVuSans.ttf", size)
        except Exception:  # noqa: BLE001
            return ImageFont.load_default()


def _status_icon(status: str) -> str:
    return {"started": "…", "done": "✓", "failed": "✗"}.get(status, "·")


def _fmt_ts(ts: str) -> str:
    try:
        # Accept ISO; show local-ish HH:MM:SS from the string if possible
        if "T" in ts:
            time_part = ts.split("T", 1)[1]
            return time_part[:8]
    except Exception:  # noqa: BLE001
        pass
    return datetime.now().strftime("%H:%M:%S")


def draw_overlay(
    frame: Image.Image,
    *,
    title: str,
    severity: str,
    actions: Sequence[object],
    stop_from: str | None = None,
    call_line: str | None = None,
) -> Image.Image:
    img = frame.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = img.size
    top_h = max(28, h // 18)
    bottom_h = max(54, h // 8)

    sev = (severity or "LOW").upper()
    top_color = (180, 40, 40, 200) if sev == "HIGH" else (200, 140, 20, 200)
    draw.rectangle([0, 0, w, top_h], fill=top_color)
    title_font = _font(max(12, top_h - 10))
    label = f"● LIVE · {title}"
    draw.text((10, max(2, (top_h - 14) // 2)), label, fill=(255, 255, 255, 255), font=title_font)

    if call_line:
        # Call status strip under the title: "WATCHING · You: approve · auto-hangup 1:42"
        strip_h = max(22, top_h * 3 // 4)
        strip_font = _font(max(11, strip_h - 8))
        text_w = int(draw.textlength(call_line, font=strip_font)) + 20
        x0 = max(0, w - text_w)
        draw.rectangle([x0, top_h, w, top_h + strip_h], fill=(15, 15, 18, 215))
        draw.text((x0 + 10, top_h + max(2, (strip_h - 14) // 2)), call_line,
                  fill=(255, 255, 255, 255), font=strip_font)

    draw.rectangle([0, h - bottom_h, w, h], fill=(20, 20, 24, 210))
    line_font = _font(max(11, bottom_h // 5))
    y = h - bottom_h + 4
    shown = list(actions)[:3]
    for action in shown:
        # duck-typed ActionEvent
        ts = getattr(action, "ts", "")
        status = getattr(action, "status", "")
        name = getattr(action, "action", "")
        detail = getattr(action, "detail", "") or ""
        line = f"{_fmt_ts(ts)}  {_status_icon(status)}  {name}: {detail}"
        if len(line) > 110:
            line = line[:107] + "…"
        draw.text((10, y), line, fill=(230, 230, 230, 255), font=line_font)
        y += max(14, bottom_h // 3)

    if stop_from:
        bar_h = max(36, h // 12)
        cy = (h - bar_h) // 2
        draw.rectangle([0, cy, w, cy + bar_h], fill=(160, 0, 0, 230))
        stop_font = _font(max(14, bar_h - 14))
        msg = f"STOPPED BY {stop_from}"
        draw.text((16, cy + max(4, (bar_h - 16) // 2)), msg, fill=(255, 255, 255, 255), font=stop_font)

    composed = Image.alpha_composite(img, overlay)
    return composed.convert("RGB")
