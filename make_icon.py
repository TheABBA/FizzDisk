#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Generate the FizzDisk brand icon (a two-tone "disc" circle).

Uses the app's two chosen accent colors:
  * blue  #3b82f6  (primary accent -- progress bar, highlighted labels)
  * amber #f59e0b  (secondary accent -- status/guidance)

Outputs (into assets/):
  fizzdisk.ico          multi-size .ico for the exe / installer / web favicon
  fizzdisk_icon.png     512x512 transparent PNG (generic/general use)
  fizzdisk_icon_512.png 512x512 PNG (alias)
"""
from PIL import Image, ImageDraw

BLUE = (0x3B, 0x82, 0xF6, 255)
AMBER = (0xF5, 0x9E, 0x0B, 255)

MASTER = 1024  # supersampled render size for smooth edges


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = MASTER // 2
    # outer "disc" ring in blue
    r_out = int(MASTER * 0.47)
    d.ellipse([cx - r_out, cy - r_out, cx + r_out, cy + r_out], fill=BLUE)
    # transparent gap -- reads as the disc edge/hub separation
    r_gap = int(MASTER * 0.30)
    d.ellipse([cx - r_gap, cy - r_gap, cx + r_gap, cy + r_gap], fill=(0, 0, 0, 0))
    # inner hub in amber
    r_in = int(MASTER * 0.27)
    d.ellipse([cx - r_in, cy - r_in, cx + r_in, cy + r_in], fill=AMBER)
    if size == MASTER:
        return img
    return img.resize((size, size), Image.LANCZOS)


def main():
    import os
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    os.makedirs(out, exist_ok=True)
    # PNG at 512
    png = render(512)
    png.save(os.path.join(out, "fizzdisk_icon.png"))
    png.save(os.path.join(out, "fizzdisk_icon_512.png"))
    # multi-size .ico (favicon + windows executable icon)
    master = render(MASTER)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    master.save(os.path.join(out, "fizzdisk.ico"),
                format="ICO", sizes=[(s, s) for s in sizes])
    print("Generated assets/fizzdisk.ico, assets/fizzdisk_icon.png, "
          "assets/fizzdisk_icon_512.png")


if __name__ == "__main__":
    main()
