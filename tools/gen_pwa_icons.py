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

from PIL import Image

WHITE = (255, 255, 255, 255)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(ROOT, "server.py")
PREVIEW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview")

# Source artwork for the app icon: the Frazil "f" mark (same image as the favicon).
SRC_LOGO = os.path.join(ROOT, "f-logo.png")
# Background tile colour. Opaque is REQUIRED — the 512 icon doubles as the manifest
# `maskable` icon, which must be full-bleed with no transparent corners or the OS
# mask shows gaps. White matches how the "f" reads on a browser tab.
BG = (255, 255, 255, 255)
# Fraction of the canvas the logo's longest side occupies. Kept modest so a circular/
# squircle maskable mask doesn't clip the mark (the safe zone is the central ~80%).
LOGO_SCALE = 0.72


def make_icon(size: int) -> bytes:
    """Composite the f-logo, centered, onto an opaque white square tile.

    Single LANCZOS downscale of the source for a crisp, un-softened mark. Works for
    `any` (square tile) and `maskable` (OS applies its own rounding) purposes.
    """
    img = Image.new("RGBA", (size, size), BG)

    logo = Image.open(SRC_LOGO).convert("RGBA")
    lw, lh = logo.size
    target = int(size * LOGO_SCALE)
    if lw >= lh:
        nw, nh = target, max(1, round(lh * target / lw))
    else:
        nw, nh = max(1, round(lw * target / lh)), target
    logo = logo.resize((nw, nh), Image.LANCZOS)

    img.alpha_composite(logo, ((size - nw) // 2, (size - nh) // 2))

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
