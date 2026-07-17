"""Generate app.ico (green status dot) for the packaged executable."""
from PIL import Image, ImageDraw

def render(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(1, int(size * 0.09))
    ring = max(1, int(size * 0.03))
    d.ellipse([pad - ring, pad - ring, size - pad + ring, size - pad + ring],
              fill=(40, 40, 40, 200))                       # dark ring
    d.ellipse([pad, pad, size - pad, size - pad], fill=(46, 204, 113, 255))
    hx0, hy0 = int(size * 0.30), int(size * 0.26)
    hx1, hy1 = int(size * 0.52), int(size * 0.46)
    d.ellipse([hx0, hy0, hx1, hy1], fill=(255, 255, 255, 110))  # highlight
    return img

base = render(256)
base.save("app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                            (64, 64), (128, 128), (256, 256)])
print("wrote app.ico")
