"""Draw app/icon.ico (BillCheck's own icon: blue tile, bank card, green check). Run: python tools/make_icon.py"""
import os
from PIL import Image, ImageDraw

S = 1024
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tile():
    # vertical gradient in the UI accent (app.css --accent / --accent-deep)
    grad = Image.new("RGBA", (S, S))
    top, bot = (26, 150, 205), (4, 88, 128)
    d = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1)
        d.line([(0, y), (S, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bot)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([40, 40, S - 40, S - 40], radius=220, fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def draw():
    im = tile()
    d = ImageDraw.Draw(im)
    # card, slightly lifted with a soft shadow
    x0, y0, x1, y1 = 170, 285, 830, 715
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([x0, y0 + 26, x1, y1 + 26], radius=70, fill=(0, 30, 50, 90))
    im = Image.alpha_composite(im, sh)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([x0, y0, x1, y1], radius=70, fill=(255, 255, 255, 255))
    d.rectangle([x0, 385, x1, 465], fill=(9, 72, 104, 255))          # magnetic stripe
    d.rounded_rectangle([235, 545, 455, 595], radius=25, fill=(176, 203, 219, 255))   # card number
    # green check badge, ringed in white, over the card's corner
    cx, cy, r = 735, 700, 175
    d.ellipse([cx - r - 30, cy - r - 30, cx + r + 30, cy + r + 30], fill=(255, 255, 255, 255))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(22, 163, 74, 255))
    d.line([(cx - 85, cy + 5), (cx - 22, cy + 70), (cx + 92, cy - 62)], fill=(255, 255, 255, 255), width=58, joint="curve")
    for px, py in ((cx - 85, cy + 5), (cx + 92, cy - 62)):
        d.ellipse([px - 29, py - 29, px + 29, py + 29], fill=(255, 255, 255, 255))
    return im


if __name__ == "__main__":
    big = draw()
    sizes = [16, 24, 32, 48, 64, 128, 256]
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(ROOT, "app", "icon.ico"), sizes=[(s, s) for s in sizes])
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(ROOT, "app", "ui", "icon.png"))
    print("ok")
