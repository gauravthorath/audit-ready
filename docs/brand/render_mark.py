"""Render docs/brand/auditready-mark.svg to the PNGs that need a bitmap.

Run: uv run python docs/brand/render_mark.py
Same geometry as the SVG, drawn at 4x and downsampled. Pillow only.
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
GREEN = "#0F6E56"


def render(size: int, out: Path) -> None:
    ss = 4
    big = size * ss
    k = big / 24
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([1 * k, 1 * k, 23 * k, 23 * k], radius=5.5 * k, fill=GREEN)
    w = 2.3 * k
    pts = [(7 * k, 12.4 * k), (10.3 * k, 15.7 * k), (17.4 * k, 8.6 * k)]
    d.line(pts, fill="white", width=int(w), joint="curve")
    for x, y in (pts[0], pts[2]):
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill="white")
    w2, y = 1.7 * k, 18.6 * k
    rule = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rule)
    rd.line([(7.5 * k, y), (16.5 * k, y)], fill=(255, 255, 255, 153), width=int(w2))
    for x in (7.5 * k, 16.5 * k):
        rd.ellipse([x - w2 / 2, y - w2 / 2, x + w2 / 2, y + w2 / 2], fill=(255, 255, 255, 153))
    im.alpha_composite(rule)
    im.resize((size, size), Image.LANCZOS).save(out)
    print(f"wrote {out.relative_to(ROOT)} ({size}px)")


if __name__ == "__main__":
    render(512, ROOT / "docs" / "brand" / "auditready-mark.png")
    render(180, ROOT / "web" / "app" / "apple-icon.png")
