"""Make every icon file from one source image.

    python scripts/make_icons.py [source.png]

The source is a square image of the rounded-square app tile on a solid
background (no transparency).  The tile is cut out with an anti-aliased
rounded-rect mask, then written as:

  packaging/icon-tile.png   the cut-out tile, transparent corners (master)
  packaging/icon.png        1024 px on Apple's icon grid (824 px tile + shadow)
  packaging/icon.icns       macOS app icon
  packaging/icon.ico        Windows icon (tile fills the square)
  docs/icon.png             website header + favicon
  vcflite/ui/icon.png       welcome screen

Needs Pillow; the .icns step needs macOS (iconutil).
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = ROOT / "image_ideas" / "idea2.png"

INSET = 3  # trim the tile's soft edge so no dark halo remains


def find_tile(im: Image.Image):
    """Locate the rounded tile.  Scanning inward from each image border, its
    edge is the first clear brightness jump (the tile is slightly lighter
    than the background around it).  Returns (left, top, right, bottom, radius).

    The bottom edge is often blurred by a drop shadow, so the tile is taken
    to be square: bottom = top + width.
    """
    g = im.convert("L").filter(ImageFilter.GaussianBlur(2))
    w, h = g.size

    def first_jump(vals, step=3, thresh=4):
        for i in range(step + 2, len(vals) - step):
            if abs(vals[i + step] - vals[i - step]) >= thresh:
                # refine to the steepest point just around the crossing
                return max(range(i, i + 2 * step), key=lambda j: abs(vals[j + 2] - vals[j - 2]))
        return None

    med = lambda xs: sorted(x for x in xs if x is not None)[len([x for x in xs if x is not None]) // 2]
    band = range(int(h * 0.4), int(h * 0.6), 8)
    left = med([first_jump([g.getpixel((x, y)) for x in range(w)]) for y in band])
    right = w - 1 - med([first_jump([g.getpixel((w - 1 - x, y)) for x in range(w)]) for y in band])
    band = range(int(w * 0.4), int(w * 0.6), 8)
    top = med([first_jump([g.getpixel((x, y)) for y in range(h)]) for x in band])
    bottom = top + (right - left)
    # Corner radius: along the diagonal from the top-left corner the tile
    # starts at radius * (1 - 1/sqrt 2).
    diag = [g.getpixel((left + i, top + i)) for i in range(int((right - left) * 0.3))]
    enter = first_jump(diag)
    radius = round(enter / (1 - 2 ** -0.5))
    return left, top, right, bottom, radius


def rounded_mask(size, radius, scale=4):
    """Anti-aliased rounded-square mask (drawn big, then downsampled)."""
    big = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        (0, 0, size * scale - 1, size * scale - 1), radius=radius * scale, fill=255)
    return big.resize((size, size), Image.LANCZOS)


def cut_tile(src: Path) -> Image.Image:
    im = Image.open(src).convert("RGB")
    l, t, r, b, radius = find_tile(im)
    print(f"tile found at ({l}, {t})-({r}, {b}), corner radius {radius}px")
    size = min(r - l, b - t) - 2 * INSET
    tile = im.crop((l + INSET, t + INSET, l + INSET + size, t + INSET + size))
    out = tile.convert("RGBA")
    out.putalpha(rounded_mask(size, radius - INSET))
    return out


def apple_grid(tile: Image.Image, canvas=1024, body=824) -> Image.Image:
    """Apple's macOS icon layout: 824 px tile centred on 1024, soft shadow."""
    t = tile.resize((body, body), Image.LANCZOS)
    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    off = (canvas - body) // 2
    shadow = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 90), (off, off + 12), t.split()[3])
    out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(14)))
    out.alpha_composite(t, (off, off))
    return out


def main(src: Path):
    tile = cut_tile(src)
    pk = ROOT / "packaging"
    tile.save(pk / "icon-tile.png")

    mac = apple_grid(tile)
    mac.save(pk / "icon.png")

    if sys.platform == "darwin" and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as d:
            iconset = Path(d) / "icon.iconset"
            iconset.mkdir()
            for s in (16, 32, 128, 256, 512):
                mac.resize((s, s), Image.LANCZOS).save(iconset / f"icon_{s}x{s}.png")
                mac.resize((s * 2, s * 2), Image.LANCZOS).save(iconset / f"icon_{s}x{s}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(pk / "icon.icns")], check=True)
    else:
        print("skipping icon.icns (needs macOS iconutil)")

    # Windows and the web: the tile fills the square (no Apple margins).
    tile.resize((256, 256), Image.LANCZOS).save(
        pk / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    tile.resize((256, 256), Image.LANCZOS).save(ROOT / "docs" / "icon.png")
    tile.resize((256, 256), Image.LANCZOS).save(ROOT / "vcflite" / "ui" / "icon.png")
    print("icons written")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC)
