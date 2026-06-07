# -*- coding: utf-8 -*-
"""
CC Pulse — 程序化像素宠物引擎（纯代码，无图片资源）
====================================================
待机态（无活跃会话）时显示的像素小生物。共 6 个形象，参考用户提供的 6 张动图：
  0 idle      发呆：偶尔眨眼 + 轻微呼吸起伏
  1 thinking  思考：头顶白色气泡，蓝点 1→2→3 循环
  2 laptop    敲代码：趴在笔记本后，蓝屏闪烁 + 蓝点上飘
  3 happy     庆祝：闭眼笑 + 上下蹦 + 彩色纸屑飘落
  4 sparkle   星光：金色四角星在四周明灭闪烁
  5 sleep     睡觉：闭眼 + z z z 上升变大

设计为「逻辑像素网格」：每个形象按当前帧返回一串 (gx, gy, "#color") 像素，
由 Viewer 负责缩放居中、用 canvas 方块画出来。本模块不依赖 tkinter，可 headless 测。

待机时每 10–20 秒在 6 个形象间随机切换（PixelPet 负责计时与换形象）。
"""
import random

# ── 逻辑网格尺寸（角色 + 四周留白给气泡/纸屑/星光/z）──
LOGICAL_W = 20
LOGICAL_H = 22

# ── 调色板 ──
ORANGE = "#D9774F"      # Claude 珊瑚橙身体
ORANGE_D = "#B65C40"    # 暗部
DARK = "#3A2A22"        # 眼/嘴/腿
WHITE = "#F4EFEA"       # 气泡
BLUE = "#5BB0E6"        # 笔记本蓝屏 / 思考点
BLUE_D = "#2E7BB5"
GRAY = "#9097A0"        # 笔记本
GRAY_D = "#5C626C"
GOLD = "#F2C14E"        # 星光
ZCOL = "#A7C4DE"        # z 冷色
CONFETTI = ["#E5484D", "#F5C518", "#46C28E", "#5BB0E6", "#9B6DD6"]

# ── 角色模板（16 宽 × 11 高）。'O'=身体 'x'=深色 ' '=透明 ──
BODY_OPEN = [
    "    OOOOOOOO    ",
    "  OOOOOOOOOOOO  ",
    " OOOOOOOOOOOOOO ",
    "OOOOOOOOOOOOOOOO",
    "OOOxxOOOOOOxxOOO",
    "OOOxxOOOOOOxxOOO",
    "OOOOOOOOOOOOOOOO",
    "OOOOOOOOOOOOOOOO",
    " OOOOOOOOOOOOOO ",
    " OOOOOOOOOOOOOO ",
    "  xx  xx  xx    ",
]
# 闭眼（眯成一道横杠，安详/睡觉/庆祝通用）
BODY_CLOSED = [
    "    OOOOOOOO    ",
    "  OOOOOOOOOOOO  ",
    " OOOOOOOOOOOOOO ",
    "OOOOOOOOOOOOOOOO",
    "OOOOOOOOOOOOOOOO",
    "OOOxxxOOOOxxxOOO",
    "OOOOOOOOOOOOOOOO",
    "OOOOOOOOOOOOOOOO",
    " OOOOOOOOOOOOOO ",
    " OOOOOOOOOOOOOO ",
    "  xx  xx  xx    ",
]
BODY_W = len(BODY_OPEN[0])
BODY_H = len(BODY_OPEN)
BODY_OX = (LOGICAL_W - BODY_W) // 2      # 居中横坐标
BODY_OY = LOGICAL_H - BODY_H             # 贴底

CHAR_COLORS = {"O": ORANGE, "x": DARK, "o": ORANGE_D}


def _put(pixels, x, y, col):
    """带边界裁剪的落点：越界直接丢弃，保证不画到网格外。"""
    if 0 <= x < LOGICAL_W and 0 <= y < LOGICAL_H:
        pixels.append((x, y, col))


def _blit(pixels, template, ox, oy, colors=CHAR_COLORS):
    """把字符模板按颜色映射追加到 pixels 列表。"""
    for r, row in enumerate(template):
        for c, ch in enumerate(row):
            col = colors.get(ch)
            if col:
                _put(pixels, ox + c, oy + r, col)


def _creature(pixels, body, dy=0):
    _blit(pixels, body, BODY_OX, BODY_OY + dy)


def _smile(pixels, dy=0):
    """嘴角小笑（庆祝时用）。"""
    y = BODY_OY + 7 + dy
    for x in (BODY_OX + 6, BODY_OX + 7, BODY_OX + 8, BODY_OX + 9):
        _put(pixels, x, y, DARK)


# ── z 字形（3×3 / 2×2）──
Z_BIG = ["zzz", "  z", "zzz"]
Z_SMALL = ["zz", " z", "zz"]


def _draw_z(pixels, glyph, ox, oy, col):
    for r, row in enumerate(glyph):
        for c, ch in enumerate(row):
            if ch == "z":
                _put(pixels, ox + c, oy + r, col)


# ── 四角星（明灭）──
def _star(pixels, cx, cy, col, big):
    _put(pixels, cx, cy, col)
    _put(pixels, cx - 1, cy, col); _put(pixels, cx + 1, cy, col)
    _put(pixels, cx, cy - 1, col); _put(pixels, cx, cy + 1, col)
    if big:
        _put(pixels, cx - 2, cy, col); _put(pixels, cx + 2, cy, col)
        _put(pixels, cx, cy - 2, col); _put(pixels, cx, cy + 2, col)


# ════════════════════════════════════════════════════════════════════════════
# 6 个形象的逐帧渲染（输入 frame，输出像素列表）
# ════════════════════════════════════════════════════════════════════════════

def form_idle(f):
    """发呆：每 ~2.5s 眨一次眼，身体缓慢起伏 1px。"""
    px = []
    bob = 0 if (f // 4) % 2 == 0 else 1
    blink = (f % 24) in (0, 1)        # 偶尔闭眼一两帧
    _creature(px, BODY_CLOSED if blink else BODY_OPEN, dy=bob)
    return px


def form_thinking(f):
    """思考：头顶气泡 + 蓝点 1→2→3 循环。"""
    px = []
    bob = 0 if (f // 5) % 2 == 0 else 1
    _creature(px, BODY_OPEN, dy=bob)
    # 气泡（白色圆角块）右上方
    bx, by = 11, 1
    bubble = [
        " www ",
        "wwwww",
        "wwwww",
        " www ",
    ]
    _blit(px, bubble, bx, by, {"w": WHITE})
    # 气泡小尾巴
    _put(px, bx + 1, by + 4, WHITE)
    # 蓝点：数量随帧 1→2→3
    n = (f // 4) % 3 + 1
    for i in range(n):
        _put(px, bx + 1 + i, by + 2, BLUE)
    return px


def form_laptop(f):
    """敲代码：趴笔记本后，蓝屏闪烁 + 蓝点上飘。"""
    px = []
    _creature(px, BODY_OPEN, dy=0)
    # 笔记本盖住身体下半（屏幕朝我们）
    lap_top = BODY_OY + 5
    screen = [
        "gggggggggg",
        "gbbbbbbbbg",
        "gbbbbbbbbg",
        "gbbbbbbbbg",
        "gggggggggg",
    ]
    bright = (f // 3) % 2 == 0
    cmap = {"g": GRAY, "b": BLUE if bright else BLUE_D}
    _blit(px, screen, BODY_OX + 3, lap_top, cmap)
    # 笔记本底座（键盘）
    _blit(px, ["GGGGGGGGGGGG"], BODY_OX + 2, lap_top + 5, {"G": GRAY_D})
    # 蓝点从屏幕上飘
    for i in range(3):
        t = (f + i * 5) % 15
        y = lap_top - 1 - t // 3
        _put(px, BODY_OX + 11 + (i % 2), y, BLUE)
    return px


def form_happy(f):
    """庆祝：闭眼笑 + 上下蹦 + 彩色纸屑飘落。"""
    px = []
    bounce = (-2, -1, 0, -1)[(f // 2) % 4]
    _creature(px, BODY_CLOSED, dy=bounce)
    _smile(px, dy=bounce)
    # 纸屑：固定若干片，按帧下落并循环
    pieces = [(2, 2, 0), (5, 3, 7), (8, 2, 3), (12, 4, 1), (15, 3, 9),
              (17, 2, 5), (3, 4, 12), (10, 3, 6), (14, 2, 10)]
    for (cx, spd, off) in pieces:
        y = (f * spd + off * 2) % (LOGICAL_H + 2) - 1
        col = CONFETTI[(cx + off) % len(CONFETTI)]
        _put(px, cx, y, col)
    return px


def form_sparkle(f):
    """星光：金色四角星在四周明灭闪烁。"""
    px = []
    bob = 0 if (f // 5) % 2 == 0 else 1
    _creature(px, BODY_OPEN, dy=bob)
    stars = [(2, 4, 0), (17, 6, 2), (4, 14, 4), (16, 15, 1),
             (10, 1, 3), (1, 9, 5)]
    for (sx, sy, off) in stars:
        phase = (f + off * 3) % 12
        if phase < 8:                     # 出现 8 帧、隐 4 帧
            _star(px, sx, sy, GOLD, big=(phase < 4))
    return px


def form_sleep(f):
    """睡觉：闭眼 + z z z 上升变大。"""
    px = []
    # 轻微呼吸
    bob = 0 if (f // 6) % 2 == 0 else 1
    _creature(px, BODY_CLOSED, dy=bob)
    # 两个 z 接力上升
    head_x = BODY_OX + BODY_W - 4
    head_y = BODY_OY - 1
    for i, period_off in enumerate((0, 8)):
        t = (f + period_off) % 16
        rise = t           # 0..15
        y = head_y - rise // 2
        if y < -2:
            continue
        big = t >= 8
        glyph = Z_BIG if big else Z_SMALL
        _draw_z(px, glyph, head_x + i, y, ZCOL)
    return px


# (renderer, caption)
FORMS = [
    (form_idle,     "发呆中…"),
    (form_thinking, "想事情…"),
    (form_laptop,   "敲代码中…"),
    (form_happy,    "完成啦，耶！"),
    (form_sparkle,  "闪闪发光～"),
    (form_sleep,    "打盹中… z z z"),
]


class PixelPet:
    """
    待机宠物控制器：管理当前形象、帧计数、10–20s 随机换形象。
    时间由调用方传入（time.time()），便于测试。
    """
    SWITCH_MIN = 10.0
    SWITCH_MAX = 20.0

    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.idx = self.rng.randrange(len(FORMS))
        self.frame = 0
        self.switch_at = 0.0
        self._active = False

    def activate(self, now):
        """进入待机态时调用：随机选一个形象并排定下次切换时间。"""
        self._active = True
        self.idx = self.rng.randrange(len(FORMS))
        self.frame = 0
        self._schedule(now)

    def deactivate(self):
        self._active = False

    def _schedule(self, now):
        self.switch_at = now + self.rng.uniform(self.SWITCH_MIN, self.SWITCH_MAX)

    def step(self, now):
        """每个动画 tick 调用：推进帧；到点则换一个不同形象。"""
        self.frame += 1
        if now >= self.switch_at:
            if len(FORMS) > 1:
                nxt = self.rng.randrange(len(FORMS) - 1)
                if nxt >= self.idx:
                    nxt += 1
                self.idx = nxt
            self.frame = 0
            self._schedule(now)

    def render(self):
        """返回 (caption, pixels)。pixels 为当前形象当前帧的 (gx,gy,color) 列表。"""
        renderer, caption = FORMS[self.idx]
        return caption, renderer(self.frame)
