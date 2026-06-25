# -*- coding: utf-8 -*-
"""
CC Pulse 应用图标生成器
=======================
参考用户给的 "claude code" 显示器图标风格：
  圆角层叠卡片 + 橙色描边显示器 + 奶白屏幕。
本图标在屏幕里画一条心电/脉搏波形，呼应 "Pulse"。

矢量超采样：在 1024 画布上用 PIL 绘制 → LANCZOS 降采样到各 ico 尺寸。
产物：
  assets/app.ico            多尺寸图标 (256/128/64/48/32/16)
  assets/app_preview.png    深色桌面背景下的预览拼图（供肉眼审查）
"""
from PIL import Image, ImageDraw

S = 1024  # 主画布边长

# ── 调色板（取自参考图的 Claude 橙）──
ORANGE      = (244, 124, 32, 255)   # 主橙
ORANGE_PALE = (248, 211, 174, 255)  # 底卡浅橙
CREAM       = (252, 244, 235, 255)  # 卡片奶白
WHITE       = (255, 255, 255, 255)


def rr(d, box, r, **kw):
    d.rounded_rectangle(box, radius=r, **kw)


def thick_polyline(d, pts, width, color):
    """带圆角端点/拐点的折线（PIL line 默认是方头）。"""
    d.line(pts, fill=color, width=width, joint="curve")
    r = width // 2
    for (x, y) in pts:
        d.ellipse((x - r, y - r, x + r, y + r), fill=color)


def render(size_canvas=S):
    img = Image.new("RGBA", (size_canvas, size_canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # ── 层叠卡片（底浅橙 + 上奶白，形成橙色外框）──
    rr(d, (95, 95, 929, 929), 200, fill=ORANGE_PALE)
    rr(d, (130, 130, 894, 894), 178, fill=CREAM)

    # ── 显示器外框（橙）+ 屏幕（白）──
    rr(d, (280, 300, 744, 628), 64, fill=ORANGE)          # 外框
    rr(d, (320, 340, 704, 588), 34, fill=WHITE)           # 屏幕
    # 顶部摄像头小凸起
    rr(d, (470, 286, 554, 312), 13, fill=ORANGE)

    # ── 底座（脖子梯形 + 圆角底条）──
    d.polygon([(476, 628), (548, 628), (566, 690), (458, 690)], fill=ORANGE)
    rr(d, (404, 686, 620, 724), 18, fill=ORANGE)

    # ── 屏幕里的脉搏 / 心电波形 ──
    pulse = [
        (332, 470), (404, 470), (430, 470),
        (452, 438), (470, 506),
        (496, 330), (520, 560),
        (548, 470), (574, 470),
        (610, 448), (628, 492), (648, 470),
        (692, 470),
    ]
    thick_polyline(d, pulse, width=22, color=ORANGE)

    return img


def render_small(size_canvas=S):
    """小尺寸专用：放大显示器、加粗、简化波形，去掉摄像头凸起，
    保证 16/32px 下仍利落可辨。"""
    img = Image.new("RGBA", (size_canvas, size_canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    rr(d, (40, 40, 984, 984), 170, fill=ORANGE_PALE)
    rr(d, (78, 78, 946, 946), 150, fill=CREAM)

    rr(d, (170, 250, 854, 720), 80, fill=ORANGE)
    rr(d, (230, 310, 794, 660), 46, fill=WHITE)
    d.polygon([(450, 720), (574, 720), (602, 800), (422, 800)], fill=ORANGE)
    rr(d, (330, 796, 694, 846), 24, fill=ORANGE)

    pulse = [
        (250, 500), (420, 500), (470, 500),
        (510, 360), (560, 640),
        (610, 500), (700, 500), (774, 500),
    ]
    thick_polyline(d, pulse, width=40, color=ORANGE)
    return img


def main():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assets = os.path.join(here, "assets")
    os.makedirs(assets, exist_ok=True)

    master = render()
    small = render_small()

    # 按尺寸分别嵌入：大尺寸用细节版，<=32 用加粗简化版
    def pick(s):
        src = master if s >= 48 else small
        return src.resize((s, s), Image.LANCZOS)

    sizes = [256, 128, 64, 48, 32, 16]
    imgs = [pick(s) for s in sizes]
    ico_path = os.path.join(assets, "app.ico")
    imgs[0].save(ico_path, format="ICO",
                 sizes=[(s, s) for s in sizes], append_images=imgs[1:])
    print("written", ico_path)

    # ── 深色桌面预览拼图：实际嵌入的各尺寸位图并排（底对齐）──
    bg = (32, 33, 36, 255)
    pad = 28
    prev_sizes = [256, 64, 48, 32, 16]
    W = sum(prev_sizes) + pad * (len(prev_sizes) + 1)
    H = 256 + pad * 2
    canvas = Image.new("RGBA", (W, H), bg)
    x = pad
    for s in prev_sizes:
        ic = pick(s)
        canvas.alpha_composite(ic, (x, pad + (256 - s)))
        x += s + pad
    prev = os.path.join(assets, "app_preview.png")
    canvas.convert("RGB").save(prev)
    print("written", prev)


if __name__ == "__main__":
    main()
