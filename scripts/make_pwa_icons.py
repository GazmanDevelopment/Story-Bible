"""
Regenerates the installable-app icons (app/static/assets/icon-192.png,
icon-512.png, icon-maskable-512.png) from the same book glyph as the 128px
Word icon. Drawn at 4x and downsampled so the edges stay crisp.

    pip install pillow   # dev-only; not a runtime or CI dependency
    python scripts/make_pwa_icons.py

The glyph is defined on the 128px grid of icon-128.png. "any" icons keep the
rounded-square look; the maskable one is full-bleed with the glyph shrunk into
the central safe zone, because the OS applies its own mask.
"""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "assets"
MAROON = (138, 47, 79, 255)
WHITE = (255, 255, 255, 255)

# Left page on the 128px grid; the right page is its mirror about x=64.
LEFT_PAGE = [(24, 38), (34, 38), (62, 46), (62, 100), (34, 94), (24, 91)]


def render(size: int, maskable: bool) -> Image.Image:
    ss = 4
    big = size * ss
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if maskable:
        d.rectangle([0, 0, big, big], fill=MAROON)
        scale = 0.62   # glyph inside the 80% safe zone
    else:
        d.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 0.16), fill=MAROON)
        scale = 1.0
    k = big / 128 * scale
    off = (big - 128 * k) / 2

    def pt(x, y):
        return (off + x * k, off + y * k)

    d.polygon([pt(x, y) for x, y in LEFT_PAGE], fill=WHITE)
    d.polygon([pt(128 - x, y) for x, y in LEFT_PAGE], fill=WHITE)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    render(192, False).save(OUT / "icon-192.png")
    render(512, False).save(OUT / "icon-512.png")
    render(512, True).save(OUT / "icon-maskable-512.png")
    print("wrote icon-192.png, icon-512.png, icon-maskable-512.png")
