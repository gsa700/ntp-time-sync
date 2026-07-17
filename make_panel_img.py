"""Render docs/panel.png - a faithful illustration of the status panel UI."""
from PIL import Image, ImageDraw, ImageFont

S = 3                      # supersample
W, H = 322, 268
GREEN = (46, 204, 113)
BODY = (247, 247, 247)
BORDER = (200, 200, 200)
SEP = (223, 223, 223)
LABEL = (85, 85, 85)
VALUE = (26, 26, 26)
BTN = (240, 240, 240)
BTN_BD = (188, 188, 188)


def font(sz, bold=False):
    name = "segoeuib.ttf" if bold else "segoeui.ttf"
    try:
        return ImageFont.truetype(name, sz * S)
    except OSError:
        return ImageFont.load_default()


img = Image.new("RGB", (W * S, H * S), BODY)
d = ImageDraw.Draw(img)


def text(x, y, s, fnt, fill, anchor="la"):
    d.text((x * S, y * S), s, font=fnt, fill=fill, anchor=anchor)


def textw(s, fnt):
    b = d.textbbox((0, 0), s, font=fnt)
    return (b[2] - b[0]) / S


def button(x, y, w, h, label, fnt):
    d.rounded_rectangle([x * S, y * S, (x + w) * S, (y + h) * S], radius=3 * S,
                        fill=BTN, outline=BTN_BD, width=1 * S)
    d.text(((x + w / 2) * S, (y + h / 2) * S), label, font=fnt, fill=VALUE, anchor="mm")


# border + header
d.rectangle([0, 0, W * S - 1, H * S - 1], outline=BORDER, width=1 * S)
d.rectangle([1 * S, 1 * S, (W - 1) * S, 50 * S], fill=GREEN)
text(16, 25, "GREEN  —  healthy", font(14, bold=True), (255, 255, 255), anchor="lm")

# readout: aligned label / value columns
f = font(10)
rows = [("Offset:", "+0.004 s"), ("Server:", "pool.ntp.org"),
        ("Source:", "pool.ntp.org,0x8"), ("Last sync:", "14:26:52  (1 min ago)")]
LABELX = 16
valx = LABELX + max(textw(lbl, f) for lbl, _ in rows) + 14
y = 66
for lbl, val in rows:
    text(LABELX, y, lbl, f, LABEL)
    text(valx, y, val, f, VALUE)
    y += 22

# separator
d.line([14 * S, 158 * S, (W - 14) * S, 158 * S], fill=SEP, width=1 * S)

# actions: uniform 2-column grid (no Quit; it's in the right-click menu)
fb = font(9)
gap, mx = 6, 12
colw = (W - 2 * mx - gap) / 2
c0, c1 = mx, mx + colw + gap
bh, r0 = 26, 170
button(c0, r0, colw, bh, "Refresh", fb)
button(c1, r0, colw, bh, "Resync  (admin)", fb)
button(c0, r0 + bh + 6, colw, bh, "Configure…  (admin)", fb)
button(c1, r0 + bh + 6, colw, bh, "Open time.is", fb)
button(c1, r0 + 2 * (bh + 6), colw, bh, "Close", fb)

img = img.resize((W, H), Image.LANCZOS)
img.save(r"C:\Users\David Erickson\ntp-time-sync\docs\panel.png")
print("wrote docs/panel.png", img.size)
