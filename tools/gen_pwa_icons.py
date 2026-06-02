"""
Generate the PWA app icons and embed them (base64) into server.py.

Dev-only utility — Pillow is NOT a runtime dependency. This bakes the PNG bytes
into the `_PWA_ICON_*_B64` constants in server.py so the app still deploys as a
two-file scp (server.py + roadmap.html).

Usage:
    pip install pillow      # if not already present
    python tools/gen_pwa_icons.py

Re-run after changing the icon design. It rewrites the three constants in place
and (optionally) drops preview PNGs under tools/preview/ for eyeballing.
"""
import base64
import io
import os
import re

from PIL import Image, ImageDraw

BRAND = (91, 79, 255, 255)   # #5b4fff
WHITE = (255, 255, 255, 255)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(ROOT, "server.py")
PREVIEW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview")


def make_icon(size: int) -> bytes:
    """Draw a rounded-square brand-purple tile with a centered white map-pin.

    Supersampled 4x then downscaled for clean antialiased edges. The pin sits
    well within the central 80% so the same art works as a maskable icon too.
    """
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded-square background.
    radius = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=BRAND)

    # Map-pin (white) — teardrop head + tail, with a punched-out center.
    pin_h = s * 0.52
    top = (s - pin_h) / 2.0
    cx = s / 2.0
    R = pin_h * 0.34          # head radius
    head_cy = top + R
    tip_y = top + pin_h

    # Head (circle).
    d.ellipse([cx - R, head_cy - R, cx + R, head_cy + R], fill=WHITE)
    # Tail (triangle from head shoulders down to the tip).
    shoulder = head_cy + R * 0.35
    d.polygon([(cx - R * 0.82, shoulder), (cx + R * 0.82, shoulder), (cx, tip_y)],
              fill=WHITE)
    # Punched-out inner circle (shows the purple background through the head).
    r_hole = R * 0.42
    d.ellipse([cx - r_hole, head_cy - r_hole, cx + r_hole, head_cy + r_hole],
              fill=BRAND)

    img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def main():
    icons = {
        "_PWA_ICON_192_B64":   make_icon(192),
        "_PWA_ICON_512_B64":   make_icon(512),
        "_PWA_ICON_APPLE_B64": make_icon(180),
    }

    # Optional previews for visual inspection.
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    for name, raw in icons.items():
        fn = name.replace("_PWA_ICON_", "icon-").replace("_B64", "").lower() + ".png"
        with open(os.path.join(PREVIEW_DIR, fn), "wb") as f:
            f.write(raw)

    with open(SERVER_PY, encoding="utf-8") as f:
        src = f.read()

    for name, raw in icons.items():
        b64 = base64.b64encode(raw).decode()
        # Replace the string literal that immediately follows `NAME   = `.
        pattern = re.compile(rf'({re.escape(name)}\s*=\s*)"[^"]*"')
        new_src, n = pattern.subn(rf'\g<1>"{b64}"', src)
        if n != 1:
            raise SystemExit(f"Expected exactly one match for {name}, found {n}")
        src = new_src
        print(f"{name}: {len(raw):>6} bytes PNG -> {len(b64):>6} chars base64")

    with open(SERVER_PY, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"Embedded into {SERVER_PY}")
    print(f"Previews written to {PREVIEW_DIR}")


if __name__ == "__main__":
    main()
