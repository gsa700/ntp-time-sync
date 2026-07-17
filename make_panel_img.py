"""Render docs/panel.png - a faithful illustration of the status panel UI."""
from PIL import Image, ImageDraw, ImageFont

S = 3                      # supersample
W, H = 322, 288
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


def button(x, y, w, h, label, fnt):
    d.rounded_rectangle([x * S, y * S, (x + w) * S, (y + h) * S], radius=3 * S,
                        fill=BTN, outline=BTN_BD, width=1 * S)
    d.text(((x + w / 2) * S, (y + h / 2) * S), label, font=fnt, fill=VALUE, anchor="mm")


# border + header
d.rectangle([0, 0, W * S - 1, H * S - 1], outline=BORDER, width=1 * S)
d.rectangle([1 * S, 1 * S, (W - 1) * S, 50 * S], fill=GREEN)
text(15, 25, "GREEN  —  healthy", font(14, bold=True), (255, 255, 255), anchor="lm")

# readout rows
f_lbl, f_val = font(10), font(10)
rows = [("Offset:", "+0.004 s"), ("Server:", "pool.ntp.org"),
        ("Source:", "pool.ntp.org,0x8"), ("Last sync:", "14:26:52  (1 min ago)")]
y = 64
for lbl, val in rows:
    text(15, y, lbl, f_lbl, LABEL)
    text(92, y, val, f_val, VALUE)
    y += 22

# separator
d.line([12 * S, 156 * S, (W - 12) * S, 156 * S], fill=SEP, width=1 * S)

# action buttons
fb = font(10)
button(12, 166, 92, 26, "Refresh", fb)
button(110, 166, 140, 26, "Resync  (admin)", fb)
button(12, 198, 150, 26, "Configure…  (admin)", fb)
button(168, 198, 100, 26, "Open time.is", fb)

# checkbox + quit/close
box = 13
by = 238
d.rounded_rectangle([15 * S, by * S, (15 + box) * S, (by + box) * S], radius=2 * S,
                    fill=(255, 255, 255), outline=(120, 120, 120), width=1 * S)
d.line([(17) * S, (by + 7) * S, (20) * S, (by + 10) * S], fill=GREEN, width=2 * S)
d.line([(20) * S, (by + 10) * S, (26) * S, (by + 3) * S], fill=GREEN, width=2 * S)
text(34, by + int(box / 2), "Start at logon", fb, VALUE, anchor="lm")
button(196, 236, 54, 24, "Quit", fb)
button(256, 236, 54, 24, "Close", fb)

img = img.resize((W, H), Image.LANCZOS)
img.save(r"C:\Users\David Erickson\ntp-time-sync\docs\panel.png")
print("wrote docs/panel.png", img.size)
