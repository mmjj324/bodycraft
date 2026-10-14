# -*- coding: utf-8 -*-
# Bodycraft · Copyright (c) 2026 @mmjj324 · PolyForm Noncommercial License 1.0.0 (no commercial use)
"""Draw the Bodycraft icon.

The design comes from the app itself: a daily schedule, a few rows of time blocks, and one row
in vermilion red for "now". No text and no gradients: text turns to mush at 16 px, and gradients
look muddy. Just blocks of color and white space.

Deep pine green with paper-colored blocks: most desktops are light, so a dark icon stands out.
Red appears in one row only, matching the app's own rule of "only one red thing per page".

Needs Pillow:  pip install pillow
Outputs, in this folder:
  bodycraft.ico      multi-size icon for Windows shortcuts
  bodycraft.png      256 px icon for the Linux app menu / desktop (and handy on macOS)
  favicon-32.png     small icon for the web page
  icon-preview.png   all sizes side by side, on light and dark backgrounds
"""
import os

try:
    from PIL import Image, ImageDraw
except ImportError:
    raise SystemExit("This script needs Pillow. Install it with:  pip install pillow")

PINE = (29, 107, 79)       # #1D6B4F deep pine green
PAPER = (242, 244, 241)    # #F2F4F1 paper
VERM = (192, 57, 47)       # #C0392F vermilion
DIM = (150, 182, 165)      # secondary rows: paper color, toned down

OUT = os.path.dirname(os.path.abspath(__file__))   # write straight into the app folder
os.makedirs(OUT, exist_ok=True)

try:
    LANCZOS = Image.Resampling.LANCZOS     # Pillow 9.1+
except AttributeError:
    LANCZOS = Image.LANCZOS


def draw_icon(size):
    # draw at 4x and scale down, so the edges come out clean
    S = size * 4
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    # rounded background
    r = int(S * 0.22)
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=PINE)

    # the vertical spine on the left = the timeline
    spine_x = int(S * 0.235)
    spine_w = max(1, int(S * 0.035))
    d.rounded_rectangle(
        [spine_x, int(S * 0.235), spine_x + spine_w, int(S * 0.765)],
        radius=spine_w // 2, fill=DIM)

    # four rows of time blocks; the third row is "now"
    rows = 4
    top = S * 0.245
    gap = S * 0.135
    bar_h = max(1, int(S * 0.072))
    bar_x = spine_x + int(S * 0.115)
    widths = [0.44, 0.30, 0.38, 0.34]   # uneven lengths, like a real schedule
    # The "now" row isn't made longer: the red already draws the eye, and making it
    # longer too would turn it into the main subject.
    for i in range(rows):
        y = int(top + i * gap)
        w = int(S * widths[i])
        now = (i == 2)
        color = VERM if now else PAPER
        d.rounded_rectangle([bar_x, y, bar_x + w, y + bar_h],
                            radius=bar_h // 2, fill=color)
        # the "now" row also gets a red dot on the spine
        if now:
            cy = y + bar_h // 2
            rr = int(S * 0.052)
            d.ellipse([spine_x + spine_w // 2 - rr, cy - rr,
                       spine_x + spine_w // 2 + rr, cy + rr], fill=VERM)

    return im.resize((size, size), LANCZOS)


sizes = [16, 24, 32, 48, 64, 128, 256]
imgs = [draw_icon(s) for s in sizes]

# .ico for Windows shortcuts: every size packed into one file
ico = os.path.join(OUT, "bodycraft.ico")
imgs[-1].save(ico, format="ICO", sizes=[(s, s) for s in sizes])
print("Created", ico, "(%.0f KB)" % (os.path.getsize(ico) / 1024))

# .png for Linux .desktop entries (and anywhere else a plain image icon is needed)
png = os.path.join(OUT, "bodycraft.png")
imgs[sizes.index(256)].save(png)
print("Created", png)

# Preview: every size side by side on a light background, to check the small ones aren't blurry
prev = Image.new("RGB", (900, 320), (242, 244, 241))
x = 20
for s, im in zip(sizes, imgs):
    prev.paste(im, (x, 30 + (256 - s) // 2), im)
    x += s + 18
# and once more on a dark background
dark = Image.new("RGB", (900, 90), (34, 34, 34))
x = 20
for s, im in zip(sizes, imgs):
    if s <= 64:
        dark.paste(im, (x, (90 - s) // 2), im)
        x += s + 18
prev.paste(dark, (0, 228))
prev.save(os.path.join(OUT, "icon-preview.png"))
print("Created preview: icon-preview.png")

# favicon for the web page
imgs[sizes.index(32)].save(os.path.join(OUT, "favicon-32.png"))
print("Sizes:", sizes)
