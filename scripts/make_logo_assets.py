"""Rebuild the logo files from ``docs/assets/logo-source.webp`` (the artwork on a flat off-white background).

    python scripts/make_logo_assets.py

Writes into ``clipcrow/service/static/`` (served by the web UI) and ``docs/assets/`` (README):
  logo.png            the full logo, background removed
  logo-mark.png       the bird alone, background removed
  favicon.ico, favicon-32.png, apple-touch-icon.png, icon-512.png   the bird on a light rounded tile (a dark bird vanishes on a dark browser tab)
  docs/assets/logo-card.png   the full logo on the light background, for the README (a transparent dark wordmark is unreadable on GitHub's dark theme)

The background is removed by colour: its colour is read from the border, every pixel is made transparent in proportion to how close it is to it, and the
edge colour is un-mixed from the background so no pale halo is left. The generator's corner sparkle is blanked first.
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "assets" / "logo-source.webp"
STATIC = ROOT / "clipcrow" / "service" / "static"
TILE = (245, 244, 240)            # the artwork's own background, also the tile behind the favicon
D0, D1 = 14.0, 60.0               # colour distance: fully background below D0, fully logo above D1
MARK_BOTTOM, MARK_LEFT_BOTTOM, MARK_LEFT_X = 532, 518, 300      # the bird ends where the wordmark begins (rows of the cropped logo)


def cut(im: Image.Image) -> tuple[Image.Image, np.ndarray]:
    a = np.asarray(im.convert("RGB")).astype(np.float32)
    h, w, _ = a.shape
    border = np.concatenate([a[:8].reshape(-1, 3), a[-8:].reshape(-1, 3), a[:, :8].reshape(-1, 3), a[:, -8:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    d = np.sqrt(((a - bg) ** 2).sum(-1))
    d[int(h * 0.90):, int(w * 0.90):] = 0                         # the generator's sparkle in the corner
    alpha = np.clip((d - D0) / (D1 - D0), 0, 1)
    al = alpha[..., None]
    fg = np.clip(np.where(al > 0.02, (a - (1 - al) * bg) / np.maximum(al, 0.02), a), 0, 255)
    img = Image.fromarray(np.dstack([fg, alpha * 255]).astype(np.uint8), "RGBA")
    return img, bg


def crop(img: Image.Image, pad: int = 24) -> Image.Image:
    x0, y0, x1, y1 = img.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    return img.crop((max(0, x0 - pad), max(0, y0 - pad), min(img.width, x1 + pad), min(img.height, y1 + pad)))


def tile(mark: Image.Image, size: int, radius: float = 0.22, inset: float = 0.12) -> Image.Image:
    """The bird centred on a light rounded square."""
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(out).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * radius), fill=TILE + (255,))
    room = int(size * (1 - 2 * inset))
    m = mark.copy()
    m.thumbnail((room, room), Image.LANCZOS)
    out.alpha_composite(m, ((size - m.width) // 2, (size - m.height) // 2))
    return out


def main() -> None:
    src = Image.open(SRC)
    img, bg = cut(src)
    full = crop(img)
    web = full.copy()
    web.thumbnail((800, 800), Image.LANCZOS)                      # the page never shows it larger than this
    web.save(STATIC / "logo.png", optimize=True)
    arr = np.asarray(full).copy()
    arr[MARK_BOTTOM:] = 0                                          # below: the wordmark
    arr[MARK_LEFT_BOTTOM:, :MARK_LEFT_X] = 0                       # the tall "l" of "Clip"
    mark = crop(Image.fromarray(arr, "RGBA"), pad=6)
    small = mark.copy()
    small.thumbnail((400, 400), Image.LANCZOS)
    small.save(STATIC / "logo-mark.png", optimize=True)
    for size, name in ((512, "icon-512.png"), (180, "apple-touch-icon.png"), (32, "favicon-32.png")):
        tile(mark, size).save(STATIC / name, optimize=True)
    tile(mark, 256, radius=0.18, inset=0.08).save(STATIC / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    card = Image.new("RGB", (full.width + 120, full.height + 120), tuple(int(v) for v in bg))
    card.paste(full, (60, 60), full)
    card.save(ROOT / "docs" / "assets" / "logo-card.png", optimize=True)


if __name__ == "__main__":
    main()
