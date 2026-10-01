"""极简 AI 面试图标：浅粉圆角底 + 对话气泡 + 星芒（AI）。
每个尺寸单独绘制，小尺寸简化，避免缩放糊成一团。"""
from PIL import Image, ImageDraw

PINK = (255, 224, 244, 255)
MAUVE = (170, 97, 160, 255)
WHITE = (255, 255, 255, 255)


def sparkle(d, cx, cy, r, col):
    k = 0.28
    d.polygon([
        (cx, cy - r), (cx + r * k, cy - r * k), (cx + r, cy), (cx + r * k, cy + r * k),
        (cx, cy + r), (cx - r * k, cy + r * k), (cx - r, cy), (cx - r * k, cy - r * k),
    ], fill=col)


def render(px):
    """在 4x 超采样画布上画，再缩到目标尺寸。"""
    S = px * 4
    u = S / 256.0  # 以 256 为设计基准的缩放系数
    small = px <= 32
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    mg = 6 * u if small else 10 * u
    rad = (40 if small else 52) * u
    d.rounded_rectangle([mg, mg, S - mg, S - mg], radius=rad, fill=PINK)

    if small:
        bx0, by0, bx1, by1 = 30 * u, 34 * u, 226 * u, 188 * u
        brad = 34 * u
    else:
        bx0, by0, bx1, by1 = 40 * u, 46 * u, 216 * u, 172 * u
        brad = 38 * u
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=brad, fill=MAUVE)
    # 尾巴
    tx = bx0 + (bx1 - bx0) * 0.28
    d.polygon([(tx, by1 - 2 * u), (tx, by1 + (34 if not small else 40) * u),
               (tx + (34 if not small else 30) * u, by1 - 2 * u)], fill=MAUVE)

    cx = (bx0 + bx1) / 2
    cy = (by0 + by1) / 2
    if small:
        sparkle(d, cx, cy, (bx1 - bx0) * 0.30, WHITE)
    else:
        sparkle(d, cx - 14 * u, cy + 3 * u, (bx1 - bx0) * 0.24, WHITE)
        sparkle(d, cx + 46 * u, cy - 34 * u, (bx1 - bx0) * 0.085, WHITE)

    return img.resize((px, px), Image.LANCZOS)


sizes = [16, 32, 48, 64, 128, 256]
frames = [render(s) for s in sizes]
out = r"E:\ailearning\interview_web\面试模拟器.ico"
frames[-1].save(out, sizes=[(s, s) for s in sizes],
                append_images=frames[:-1])
frames[-1].save(r"E:\ailearning\interview_web\面试模拟器_预览.png")

# 拼一张各尺寸对照图便于检查
strip = Image.new("RGBA", (sum(s for s in sizes) + 10 * len(sizes), 260), (250, 250, 250, 255))
x = 5
for f in frames:
    strip.paste(f, (x, 5), f)
    x += f.width + 10
strip.save(r"C:\Users\never\AppData\Local\Temp\claude\C--Users-never\3964517e-10f9-4d29-bd25-824d61b2be28\scratchpad\ico_strip.png")
print("ok")
