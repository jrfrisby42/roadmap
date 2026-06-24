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

WHITE = (255, 255, 255, 255)
TRANSPARENT = (0, 0, 0, 0)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(ROOT, "server.py")
PREVIEW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preview")

# Source artwork: the Frazil "f" mark (same image as the favicon). It ships with a
# baked-in white background, so we flood-fill that backdrop to transparent from the
# four corners — clearing the surrounding white while preserving the "f"'s ENCLOSED
# interior white outline strokes (a corner flood-fill can't reach them).
SRC_LOGO = os.path.join(ROOT, "f-logo.png")


def _cutout() -> Image.Image:
    img = Image.open(SRC_LOGO).convert("RGBA")
    w, h = img.size
    for corner in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        ImageDraw.floodfill(img, corner, TRANSPARENT, thresh=60)
    return img


def _place(size: int, scale: float, bg) -> bytes:
    """Composite the cutout mark, centered at `scale` of the canvas, onto `bg`
    (an RGBA tuple; (0,0,0,0) = transparent tile). Single LANCZOS downscale."""
    img = Image.new("RGBA", (size, size), bg)
    logo = _cutout()
    lw, lh = logo.size
    target = int(size * scale)
    if lw >= lh:
        nw, nh = target, max(1, round(lh * target / lw))
    else:
        nw, nh = max(1, round(lw * target / lh)), target
    logo = logo.resize((nw, nh), Image.LANCZOS)
    img.alpha_composite(logo, ((size - nw) // 2, (size - nh) // 2))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# "any" icons (192/512): TRANSPARENT background — clean on white/light surfaces,
# browser tabs, and install prompts. Apple touch icon: OPAQUE white, because iOS fills
# transparency with BLACK on the home screen. (No `maskable` icon by design — it must be
# full-bleed opaque, which would re-introduce the white tile; Android falls back to
# plating the transparent mark on its own adaptive background.)
def transparent_icon(size: int) -> bytes:
    return _place(size, 0.86, TRANSPARENT)


def opaque_icon(size: int) -> bytes:
    return _place(size, 0.74, WHITE)


def main():
    icons = {
        "_PWA_ICON_192_B64":   transparent_icon(192),
        "_PWA_ICON_512_B64":   transparent_icon(512),
        "_PWA_ICON_APPLE_B64": opaque_icon(180),
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
