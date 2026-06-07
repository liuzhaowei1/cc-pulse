#!/usr/bin/env python3
"""
生成 CC Pulse 待机宠物 (F3) 的默认动图 assets/claude_sleep.gif。

一只打盹的像素小 Claude（Claude 品牌珊瑚橙的小方块生物），闭眼 + 呼吸起伏 +
头顶飘起的 "z z z"。64x64、透明背景、8 帧循环。

仅在准备资源时用 Pillow 跑一次；运行期 Viewer 不依赖 Pillow（缺图时回退 😴）。
    python3 tools/make_pet_gif.py
"""
import os
from PIL import Image, ImageDraw

W = H = 64
SCALE = 4           # 16x16 逻辑像素放大到 64，得到方块像素感
LW = W // SCALE     # 逻辑画布 16x16
FRAMES = 8
DURATION = 150      # 每帧毫秒

CORAL = (217, 119, 87)      # Claude 珊瑚橙身体
CORAL_DK = (179, 92, 64)    # 暗部
CHEEK = (240, 168, 140)     # 腮红
DARK = (60, 40, 32)         # 眼/嘴线条
Z = (180, 200, 220)         # z z z 冷色调


def lroundrect(d, box, r, fill):
    d.rounded_rectangle(box, radius=r, fill=fill)


def render_frame(i):
    """在 16x16 逻辑画布上画一帧，再放大。"""
    img = Image.new("RGBA", (LW, LW), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 呼吸：身体在竖直方向轻微起伏（±1 逻辑像素）
    breathe = [0, 0, 1, 1, 1, 1, 0, 0][i]
    top = 5 + breathe
    # 身体（圆角方块）
    lroundrect(d, [3, top, 12, 14], r=3, fill=CORAL)
    # 底部暗部
    lroundrect(d, [3, 12, 12, 14], r=2, fill=CORAL_DK)
    lroundrect(d, [3, top, 12, 13], r=3, fill=CORAL)
    # 腮红
    d.point((4, top + 4), fill=CHEEK)
    d.point((11, top + 4), fill=CHEEK)
    # 闭着的眼睛 ^ ^（两短弧用像素点近似）
    ey = top + 3
    d.point((5, ey), fill=DARK)
    d.point((6, ey - 1), fill=DARK)
    d.point((7, ey), fill=DARK)
    d.point((8, ey), fill=DARK)
    d.point((9, ey - 1), fill=DARK)
    d.point((10, ey), fill=DARK)
    # 小嘴（睡得香的小三角）
    d.point((7, top + 6), fill=DARK)
    d.point((8, top + 6), fill=DARK)

    # 头顶飘 z：三个 z 随帧上升 + 渐隐
    phase = i / FRAMES
    zs = [
        (10, 4, 0.0, 1),   # x, y_base, 出场相位, 大小档
        (12, 2, 0.33, 1),
        (13, 0, 0.66, 0),
    ]
    for zx, zy, ph, big in zs:
        t = (phase + ph) % 1.0          # 0..1 生命周期
        rise = int(t * 3)               # 越久飘越高
        y = zy - rise
        if y < -1:
            continue
        col = Z if t < 0.7 else (Z[0], Z[1], Z[2])  # 末段本应渐隐，GIF 无 alpha 渐变，靠上升表现
        if big:
            # 大 z：3x3 笔画
            d.line([(zx, y), (zx + 2, y)], fill=col)
            d.line([(zx + 2, y), (zx, y + 2)], fill=col)
            d.line([(zx, y + 2), (zx + 2, y + 2)], fill=col)
        else:
            # 小 z：2x2
            d.line([(zx, y), (zx + 1, y)], fill=col)
            d.point((zx + 1, y), fill=col)
            d.point((zx, y + 1), fill=col)
            d.line([(zx, y + 1), (zx + 1, y + 1)], fill=col)

    return img.resize((W, H), Image.NEAREST)


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(here, "assets")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "claude_sleep.gif")

    frames = [render_frame(i) for i in range(FRAMES)]
    # GIF 不支持半透明 alpha，用调色板 + 透明色索引保留镂空背景
    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=DURATION,
        loop=0,
        disposal=2,
        transparency=0,
        optimize=False,
    )
    print("wrote", out, os.path.getsize(out), "bytes")


if __name__ == "__main__":
    main()
