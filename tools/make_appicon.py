#!/usr/bin/env python3
"""Draws the BatteryPilot app icon and writes app/AppIcon.icns. Needs Pillow and numpy (pip install pillow numpy).
Run it again after editing the colours or shapes below. A copy of the finished icon is already in the repo."""
import io, os, struct
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

A, B = (47, 107, 255), (39, 169, 255)   # blue gradient, same as the dashboard
K = 2                                    # supersampling
N = 1024 * K
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")


def sq(v):
    return int(v * K)


def draw():
    # gradient tile, macOS style margins
    yy, xx = np.mgrid[0:N, 0:N]
    t = ((xx + yy) / (2 * N))[..., None]
    grad = (np.array(A) * (1 - t) + np.array(B) * t).astype("uint8")
    tile = Image.fromarray(grad, "RGB").convert("RGBA")
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle([sq(100), sq(100), sq(924), sq(924)], sq(185), fill=255)
    shadow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([sq(100), sq(116), sq(924), sq(940)], sq(185), fill=(0, 0, 0, 90))
    img = Image.alpha_composite(Image.new("RGBA", (N, N), (0, 0, 0, 0)), shadow.filter(ImageFilter.GaussianBlur(sq(14))))
    tile.putalpha(mask)
    img = Image.alpha_composite(img, tile)
    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    d.rounded_rectangle([sq(212), sq(362), sq(772), sq(662)], sq(62), outline=white, width=sq(34))   # battery body
    d.rounded_rectangle([sq(792), sq(458), sq(832), sq(566)], sq(20), fill=white)                    # nub
    d.rounded_rectangle([sq(262), sq(412), sq(262 + 322), sq(612)], sq(28), fill=white)              # charge level, about 70%
    bolt = [(528, 420), (440, 520), (494, 520), (462, 604), (556, 498), (504, 498)]
    d.polygon([(sq(x), sq(y)) for x, y in bolt], fill=A + (255,))
    return img.resize((1024, 1024), Image.LANCZOS)


def icns(img, path):
    kinds = [(b"icp4", 16), (b"icp5", 32), (b"ic11", 32), (b"ic12", 64), (b"ic07", 128),
             (b"ic13", 256), (b"ic08", 256), (b"ic14", 512), (b"ic09", 512), (b"ic10", 1024)]
    body = b""
    for code, px in kinds:
        buf = io.BytesIO()
        img.resize((px, px), Image.LANCZOS).save(buf, "PNG")
        data = buf.getvalue()
        body += code + struct.pack(">I", len(data) + 8) + data
    with open(path, "wb") as f:
        f.write(b"icns" + struct.pack(">I", len(body) + 8) + body)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    im = draw()
    icns(im, os.path.join(OUT, "AppIcon.icns"))
    im.resize((256, 256), Image.LANCZOS).save("/tmp/appicon_preview.png")
    print("wrote", os.path.join(OUT, "AppIcon.icns"))
