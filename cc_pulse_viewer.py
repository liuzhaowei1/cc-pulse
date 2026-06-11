#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CC Pulse — Windows 悬浮状态栏 (Viewer)  F2–F6
============================================

把每个 Claude Code 会话的三态（忙🟡 / 等你🔴 / 已完成🟢）瞄一眼即知，
无需切回终端。唯一输入是 F1 上报端写到共享 state 目录的 <session_id>.json
（格式见 PRD〇节，已是既成事实，本端只读不改格式）。

技术栈：Python 3 + tkinter（纯标准库）。
打包：PyInstaller --onefile --windowed（见 build/）。

实现范围：
  F2 置顶/半透明/可拖拽主窗体 + 会话列表（每≤1s 轮询，按 updated_at 降序，三态色点）
  F3 待机宠物（无会话时打盹小 Claude，1.5s 防抖）
  F4 标题栏：📌 置顶切换 / ⚙️ 设置 / ✕ 关闭
  F5 设置：透明度/宽/高 + 即时原子持久化 config.json
  F6 残留会话清理：每 5s 经 wsl.exe 对各 pid 做 kill -0，死进程文件删除；pid=0 走超时
"""

import glob
import json
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import tkinter as tk
import tkinter.font as tkfont

import pixelpet               # 程序化像素宠物（纯代码，无图片资源）

# ────────────────────────────────────────────────────────────────────────────
# 常量 / 配置
# ────────────────────────────────────────────────────────────────────────────

APP_NAME = "cc-pulse"

STATUS_DONE = "done"
STATUS_BUSY = "busy"
STATUS_NEEDS_YOU = "needs_you"

# 三态色点（PRD F2-3）
DOT_COLORS = {
    STATUS_NEEDS_YOU: "#E5484D",   # 🔴 等你操作（最扎眼）
    STATUS_BUSY: "#F5C518",        # 🟡 忙
    STATUS_DONE: "#46C28E",        # 🟢 已完成 / 空闲
}
# 排序时让红 > 黄 > 绿在同一 updated_at 下更靠前（次级权重）
STATUS_RANK = {STATUS_NEEDS_YOU: 0, STATUS_BUSY: 1, STATUS_DONE: 2}

# 视觉
BG = "#1E1E1E"
TITLE_BG = "#2A2A2A"
FG = "#FFFFFF"
FG_DIM = "#9A9A9A"
SEP = "#383838"
ACCENT = "#46C28E"

TITLEBAR_H = 26
GRIP_H = 8                  # 底边高度调节热区厚度（px）：与背景同色不可见，靠近即变拉伸光标
# 固定行高（px）。可视条数 = 可用高度 // ROW_H，随面板高度自适应（拉高 → 多排几条，
# 而非把固定几条拉胖）。40 = 紧凑单行（一行文字 + 舒适留白），偏紧/偏松改这个数即可。
ROW_H = 40
# 固定字号（px）。与行高解耦——调行高不再连带缩放字体；用户档位偏移叠加在此基础上。
# 9 = 原 13 基准下 -4 档的字号，定为新默认；设置面板「字号偏移」在此基础上 ±5 档微调。
BASE_FONT = 9
POLL_MS = 200              # 状态轮询周期（无变化时由 _last_sig 跳过重绘，CPU 开销可忽略）
REAP_MS = 5000             # 残留清理周期（5s）
ANIM_MS = 120              # 宠物动画帧间隔（程序化像素逐帧）
PET_DEBOUNCE_S = 1.5       # 进入宠物态防抖
STALE_DONE_S = 1800        # pid=0 / fallback：done 闲置 30 分钟判废
STALE_BUSY_S = 7200        # fallback：busy/needs_you 阈值更长，避免误删长任务

# 配置项 schema（默认值 + 范围）。范围用于 clamp。
CONFIG_DEFAULTS = {
    "opacity": 85,             # 20–100 %
    "width": 260,              # 160–600 px
    "height": 200,             # 120–800 px
    "pos_x": None,             # None → 主屏右上角
    "pos_y": None,
    "always_on_top": True,
    "font_level": 0,           # 字号档位：以默认字号为 0，-5..+5（每档 ±1px）
    "sound_enabled": True,     # 红点亮起时播放提示音（assets/notify.wav）
}
CONFIG_RANGES = {
    "opacity": (20, 100),
    "width": (160, 600),
    "height": (120, 800),
    "font_level": (-5, 5),
}

IS_WINDOWS = os.name == "nt"

# winsound 仅 Windows 可用；WSL/开发机导入失败时降级为静默（不出声、不报错）
try:
    import winsound
except ImportError:
    winsound = None

ALERT_THROTTLE_S = 1.0     # 红点提示音最短重复间隔，避免多会话同时变红或抖动连响


# ────────────────────────────────────────────────────────────────────────────
# 资源路径解析（区别于 base_dir：assets 随脚本/exe 走，不在 %LOCALAPPDATA%）
# 冻结态（PyInstaller onefile）→ sys._MEIPASS/assets；开发态 → 脚本同级 assets
# ────────────────────────────────────────────────────────────────────────────

def asset_path(name):
    root = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, "assets", name)


# 红点提示音：固定文件，换音效直接覆盖 assets/notify.wav 即可（须为 WAV）
ALERT_SOUND = asset_path("notify.wav")


# ────────────────────────────────────────────────────────────────────────────
# 路径解析（Windows 走 %LOCALAPPDATA%；WSL/开发机回退 /mnt 路径，便于本地自测）
# ────────────────────────────────────────────────────────────────────────────

def base_dir():
    la = os.environ.get("LOCALAPPDATA")
    if la:
        return os.path.join(la, APP_NAME)
    # 开发/自测回退：动态获取 WSL 路径
    users_dir = pathlib.Path("/mnt/c/Users")
    if users_dir.exists():
        system_users = {"Default", "Public", "All Users", "Default User", "desktop.ini"}
        for user_dir in users_dir.iterdir():
            if user_dir.is_dir() and user_dir.name not in system_users:
                return f"/mnt/c/Users/{user_dir.name}/AppData/Local/cc-pulse"
    # 最终回退
    return os.path.join(pathlib.Path.home(), ".cc-pulse")


def state_dir():
    return os.path.join(base_dir(), "state")


def config_path():
    return os.path.join(base_dir(), "config.json")


def log_path():
    return os.path.join(state_dir(), "reporter.log")


def log(msg):
    """复用 reporter 的日志文件，失败不抛。"""
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write("%s [viewer] %s\n" % (datetime.now().isoformat(), msg))
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────────────
# 纯逻辑（无 tk 依赖，便于 headless 单测）
# ────────────────────────────────────────────────────────────────────────────

def atomic_write_json(path, obj):
    """先写 .tmp 再 mv 覆盖，读端永不读到半成品。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def parse_iso(s):
    """ISO8601(带时区) → epoch 秒；解析失败返回 None。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def read_states(sdir):
    """
    读取 state 目录下所有有效会话，按 (updated_at 降序, 状态优先级) 排序。
    损坏 / 瞬时缺失的文件直接跳过（容忍原子替换/删除竞态），不抛。
    """
    states = []
    try:
        files = glob.glob(os.path.join(sdir, "*.json"))
    except OSError:
        return states
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except (OSError, ValueError):
            continue  # 半成品 / 损坏 / 刚被删 → 跳过，下轮重试
        if not isinstance(obj, dict):
            continue
        sid = obj.get("session_id") or os.path.splitext(os.path.basename(fp))[0]
        status = obj.get("status")
        if status not in DOT_COLORS:
            continue
        label = (obj.get("label") or "").strip() or "未命名会话"
        states.append({
            "session_id": sid,
            "status": status,
            "label": label,
            "cwd": obj.get("cwd", ""),
            "pid": int(obj.get("pid") or 0),
            "updated_at": obj.get("updated_at", ""),
            "_ts": parse_iso(obj.get("updated_at")) or 0.0,
            "_path": fp,
        })
    states.sort(key=lambda s: (-s["_ts"], STATUS_RANK.get(s["status"], 9)))
    return states


def clamp_config(cfg):
    """合并默认值并把数值项夹到合法范围；返回新 dict。"""
    out = dict(CONFIG_DEFAULTS)
    if isinstance(cfg, dict):
        for k in out:
            if k in cfg and cfg[k] is not None:
                out[k] = cfg[k]
    for k, (lo, hi) in CONFIG_RANGES.items():
        try:
            out[k] = max(lo, min(hi, int(out[k])))
        except (TypeError, ValueError):
            out[k] = CONFIG_DEFAULTS[k]
    out["always_on_top"] = bool(out["always_on_top"])
    out["sound_enabled"] = bool(out["sound_enabled"])
    for k in ("pos_x", "pos_y"):
        if out[k] is not None:
            try:
                out[k] = int(out[k])
            except (TypeError, ValueError):
                out[k] = None
    return out


def load_config(path):
    """读取并校正 config；缺失/损坏则用默认值并重写一份干净 config。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return clamp_config(raw)
    except (OSError, ValueError):
        cfg = clamp_config({})
        try:
            atomic_write_json(path, cfg)
        except Exception:
            pass
        return cfg


def reaper_decide(states, alive_pids, now_ts,
                  stale_done=STALE_DONE_S, stale_busy=STALE_BUSY_S):
    """
    判定哪些 session 应被回收（返回 session_id 列表）。
      - alive_pids 为 set → wsl 判活可用：pid>0 且不在存活集合 → 删。
      - alive_pids 为 None → wsl 不可用：纯 updated_at 超时（busy 阈值更长）。
      - pid==0（取不到）一律走 done 超时（30 分钟）判废。
      - 时钟异常导致 age<0 时不删。
    """
    dead = []
    for s in states:
        pid = s.get("pid", 0) or 0
        ts = s.get("_ts") or 0.0
        age = now_ts - ts if ts else 0.0
        if pid > 0 and alive_pids is not None:
            if pid not in alive_pids:
                dead.append(s["session_id"])
            continue
        # pid==0，或 wsl 不可用：超时判废
        if pid == 0:
            thr = stale_done
        else:
            thr = stale_done if s.get("status") == STATUS_DONE else stale_busy
        if age > thr:
            dead.append(s["session_id"])
    return dead


def query_alive_pids(pids, timeout=4.0):
    """
    经 wsl.exe 一次性批量 kill -0 判活。返回存活 pid 的 set；
    wsl 不可用 / 异常 / 无哨兵 → 返回 None（调用方退化为超时判废）。
    """
    pids = [int(p) for p in pids if int(p) > 0]
    if not pids:
        return set()
    # 哨兵确认脚本真的跑过；逐个 echo 存活 pid
    inner = "echo __WSLOK__; " + "; ".join(
        "kill -0 %d 2>/dev/null && echo %d" % (p, p) for p in pids
    )
    kwargs = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(
            ["wsl.exe", "bash", "-c", inner],
            capture_output=True, text=True, timeout=timeout, **kwargs
        )
    except Exception:
        return None
    # 不能用 returncode 判成败：bash -c 的退出码 = 最后一条命令的退出码，最后一个
    # 探测 pid 若已死，"kill -0 && echo" 短路会让整体退出码为 1，把这份本来正确的
    # 判活结果整个误丢成 None（退回超时判废）。改以 __WSLOK__ 哨兵确认脚本确实跑过；
    # 各 pid 存活与否由它自己有没有 echo 出来表达，与整体退出码无关。
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    if "__WSLOK__" not in lines:
        return None
    alive = set()
    for ln in lines:
        if ln == "__WSLOK__":
            continue
        try:
            alive.add(int(ln))
        except ValueError:
            pass
    return alive


def primary_geometry(width, height, screen_w, screen_h):
    """计算默认落点（主屏右上角，留 16px 边距）。"""
    x = max(0, screen_w - width - 16)
    y = 48
    return x, y


def is_offscreen(x, y, width, height, screen_w, screen_h):
    """落点是否大体落在主屏之外（容忍多屏负坐标的简单判定）。"""
    if x is None or y is None:
        return True
    # 至少要有一部分可见
    if x + width < 0 or y + height < 0:
        return True
    if x > screen_w - 24 or y > screen_h - 24:
        return True
    return False


def truncate_to_width(font, text, max_px):
    """按像素宽截断并加省略号。"""
    if font.measure(text) <= max_px:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + ell) <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo] + ell) if lo > 0 else ell


# ────────────────────────────────────────────────────────────────────────────
# 简易 tooltip
# ────────────────────────────────────────────────────────────────────────────

class Tooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, _e=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry("+%d+%d" % (x, y))
        self.tip.attributes("-topmost", True)
        tk.Label(self.tip, text=self.text, bg="#000000", fg="#EEEEEE",
                 font=("Segoe UI", 8), padx=6, pady=2).pack()

    def _hide(self, _e=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# ────────────────────────────────────────────────────────────────────────────
# 主 Viewer
# ────────────────────────────────────────────────────────────────────────────

class CCPulseViewer:
    def __init__(self):
        self.cfg = load_config(config_path())
        os.makedirs(state_dir(), exist_ok=True)

        self.root = tk.Tk()
        self.root.title("CC Pulse")
        self.root.configure(bg=BG)
        self.root.overrideredirect(True)              # 无边框
        self.root.attributes("-topmost", self.cfg["always_on_top"])
        self.root.attributes("-alpha", self.cfg["opacity"] / 100.0)

        self._place_window()

        # 状态
        self.scroll_off = 0          # 列表竖向滚动偏移(px)
        self.empty_since = None      # 会话归零的时刻（宠物防抖）
        self.in_pet_mode = False
        self.pixel_pet = pixelpet.PixelPet()    # 程序化像素宠物
        self.settings_win = None
        self._drag = (0, 0)
        self._last_sig = None        # 列表渲染签名，避免无变化重绘
        self._last_pet_sig = None    # 宠物渲染签名
        self._prev_status = {}       # session_id → 上一轮 status，用于红点边沿检测
        self._last_alert_t = 0.0     # 上次播放提示音的时刻（节流）

        # 系统托盘（仅 Windows）。托盘线程经队列把动作回交主线程消费（tkinter 非线程安全）。
        self._tray_q = queue.Queue()
        self.tray = None
        if IS_WINDOWS:
            try:
                import tray as _tray
                self.tray = _tray.TrayIcon(
                    tip="CC Pulse",
                    on_show=lambda: self._tray_q.put("show"),
                    on_exit=lambda: self._tray_q.put("exit"))
                self.tray.start()
                self.tray.add()       # 常驻：启动即显示托盘图标，不随窗口显隐增删
            except Exception as e:
                log("tray init failed: %r" % e)
                self.tray = None

        self._build_titlebar()
        self._build_resize_grip()      # 底边拖拽热区（须在主体之前 pack，占住底部）
        self._build_body()

        self.root.bind("<Configure>", self._on_configure, add="+")
        # 启动各周期循环
        self.root.after(50, self._tick)
        self.root.after(REAP_MS, self._reap)
        self.root.after(ANIM_MS, self._animate)
        self.root.after(150, self._poll_tray)    # 消费托盘线程的动作

    # ── 窗口放置 ────────────────────────────────────────────────────────────
    def _place_window(self):
        w, h = self.cfg["width"], self.cfg["height"]
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x, y = self.cfg["pos_x"], self.cfg["pos_y"]
        if is_offscreen(x, y, w, h, sw, sh):
            x, y = primary_geometry(w, h, sw, sh)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _resize(self):
        w, h = self.cfg["width"], self.cfg["height"]
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self._last_sig = None  # 强制重绘

    # ── 标题栏 (F4) ──────────────────────────────────────────────────────────
    def _build_titlebar(self):
        bar = tk.Frame(self.root, bg=TITLE_BG, height=TITLEBAR_H)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)
        self.titlebar = bar

        # 右侧四控件：✕ ⚙ — 📌（从右往左 pack → 视觉 📌 — ⚙ ✕）
        self.btn_close = self._titlebar_btn(bar, "✕", self.close, "退出")
        self.btn_settings = self._titlebar_btn(bar, "⚙", self.toggle_settings, "设置")
        self.btn_min = self._titlebar_btn(bar, "—", self.minimize_to_tray, "最小化到托盘")
        self.btn_pin = self._titlebar_btn(bar, "📌", self.toggle_pin, "置顶/取消置顶")
        self._refresh_pin()

        # 拖拽：标题栏空白区（排除按钮，避免拖拽+按钮动作同时触发）
        btns = {self.btn_close, self.btn_settings, self.btn_min, self.btn_pin}
        for w in (bar,) + tuple(bar.winfo_children()):
            if w not in btns:
                w.bind("<Button-1>", self._drag_start, add="+")
                w.bind("<B1-Motion>", self._drag_move, add="+")
                w.bind("<ButtonRelease-1>", self._drag_end, add="+")

    def _titlebar_btn(self, parent, glyph, cmd, tip):
        lbl = tk.Label(parent, text=glyph, bg=TITLE_BG, fg=FG_DIM,
                       font=("Segoe UI Emoji", 10), padx=6, cursor="hand2")
        lbl.pack(side="right")
        lbl.bind("<Button-1>", lambda e: cmd())
        lbl.bind("<Enter>", lambda e: lbl.config(fg=FG))
        lbl.bind("<Leave>", lambda e: self._restore_btn_color(lbl))
        Tooltip(lbl, tip)
        return lbl

    def _restore_btn_color(self, lbl):
        if lbl is self.btn_pin and self.cfg["always_on_top"]:
            lbl.config(fg=ACCENT)
        else:
            lbl.config(fg=FG_DIM)

    def _refresh_pin(self):
        # 置顶 ON → 高亮强调色；OFF → 暗色
        self.btn_pin.config(fg=ACCENT if self.cfg["always_on_top"] else FG_DIM)

    # ── 主体 (Canvas: 列表 / 宠物共用) ────────────────────────────────────────
    def _build_body(self):
        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(side="top", fill="both", expand=True)
        # 列表滚动
        self.canvas.bind("<MouseWheel>", self._on_wheel, add="+")       # Windows
        self.canvas.bind("<Button-4>", lambda e: self._wheel(-1), add="+")  # X11 上
        self.canvas.bind("<Button-5>", lambda e: self._wheel(1), add="+")   # X11 下
        # 在宠物态时也可拖拽窗口（点身体）
        self.canvas.bind("<Button-1>", self._drag_start, add="+")
        self.canvas.bind("<B1-Motion>", self._drag_move, add="+")
        self.canvas.bind("<ButtonRelease-1>", self._drag_end, add="+")

    # ── 底边高度调节（拖拽热区，仿系统窗口下边缘）─────────────────────────────
    def _build_resize_grip(self):
        # 与背景同色的薄条铺在最底部：平时看不见，鼠标靠近即显示上下拉伸光标，
        # 按住上下拖动直接改高度。手动在设置里填「高(px)」的方式不受影响。
        grip = tk.Frame(self.root, bg=BG, height=GRIP_H,
                        cursor="sb_v_double_arrow")
        grip.pack(side="bottom", fill="x")
        grip.pack_propagate(False)
        self.resize_grip = grip
        self._grip0 = (0, 0)
        grip.bind("<Button-1>", self._grip_start, add="+")
        grip.bind("<B1-Motion>", self._grip_move, add="+")
        grip.bind("<ButtonRelease-1>", self._grip_end, add="+")

    def _grip_start(self, e):
        self._grip0 = (e.y_root, self.root.winfo_height())

    def _grip_move(self, e):
        start_y, start_h = self._grip0
        lo, hi = CONFIG_RANGES["height"]
        new_h = max(lo, min(hi, start_h + (e.y_root - start_y)))
        self.root.geometry("%dx%d+%d+%d" % (
            self.root.winfo_width(), new_h,
            self.root.winfo_x(), self.root.winfo_y()))
        self.cfg["height"] = new_h
        self._last_sig = None       # 立即按新高度重排可见条数
        # 设置面板若开着，同步「高(px)」输入框
        if self.settings_win is not None and hasattr(self, "var_h"):
            try:
                self.var_h.set(new_h)
            except tk.TclError:
                pass

    def _grip_end(self, _e):
        self._save_config()

    # ── 拖拽 ──────────────────────────────────────────────────────────────────
    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(),
                      e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        x = e.x_root - self._drag[0]
        y = e.y_root - self._drag[1]
        self.root.geometry("+%d+%d" % (x, y))

    def _drag_end(self, _e):
        self.cfg["pos_x"] = self.root.winfo_x()
        self.cfg["pos_y"] = self.root.winfo_y()
        self._save_config()

    def _on_configure(self, _e):
        pass

    # ── 滚动 ──────────────────────────────────────────────────────────────────
    def _on_wheel(self, e):
        self._wheel(-1 if e.delta > 0 else 1)

    def _wheel(self, direction):
        self.scroll_off = max(0, self.scroll_off + direction * self._row_h())
        self._last_sig = None  # 下一轮 _tick 会带着新 scroll_off 重绘

    # ── 度量 ────────────────────────────────────────────────────────────────
    def _body_h(self):
        return max(40, self.root.winfo_height() - TITLEBAR_H - GRIP_H)

    def _row_h(self):
        # 固定行高：拉高面板时不放大每行，而是多容纳几行（放不下走滚动）。
        return ROW_H

    def _font_size(self):
        # 固定字号（与行高解耦），再叠加用户档位偏移（每档 ±1px）
        return max(6, min(28, BASE_FONT + int(self.cfg.get("font_level", 0))))

    # ── 红点提示音 (F2-3 听觉增强) ────────────────────────────────────────────
    def _check_alerts(self, states):
        """边沿检测：任意会话 非needs_you → needs_you 的那一刻播一次提示音。
        每轮用当前快照整体替换 _prev_status，自然清理已消失的会话。
        新出现且已是 needs_you 的会话（prev 无记录）也视为刚亮起。"""
        cur = {s["session_id"]: s["status"] for s in states}
        rose = any(
            st == STATUS_NEEDS_YOU and self._prev_status.get(sid) != STATUS_NEEDS_YOU
            for sid, st in cur.items()
        )
        self._prev_status = cur
        if rose:
            self._play_alert()

    def _play_alert(self):
        """异步播放 assets/notify.wav（不阻塞轮询）。带节流；任何失败仅记日志。"""
        if not self.cfg.get("sound_enabled", True) or winsound is None:
            return
        now = time.time()
        if now - self._last_alert_t < ALERT_THROTTLE_S:
            return
        self._last_alert_t = now
        try:
            winsound.PlaySound(
                ALERT_SOUND, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            log("play alert failed: %r" % e)

    # ── 主轮询：决定列表态 / 宠物态并渲染 (F2/F3) ─────────────────────────────
    def _tick(self):
        try:
            states = read_states(state_dir())
            self._check_alerts(states)
            if states:
                self.empty_since = None
                if self.in_pet_mode:
                    self.in_pet_mode = False
                    self._last_sig = None
                self._render_list(states)
            else:
                # 防抖：归零后等 1.5s 才进宠物态，避免 0↔1 抖动闪烁
                now = time.time()
                if self.empty_since is None:
                    self.empty_since = now
                if self.in_pet_mode or (now - self.empty_since) >= PET_DEBOUNCE_S:
                    if not self.in_pet_mode:
                        self.in_pet_mode = True
                        self._last_pet_sig = None
                        self.pixel_pet.activate(now)
                    self._render_pet()
                else:
                    # 防抖窗口内：维持上一帧（保留列表残影或空），不强切
                    pass
        except Exception as e:
            log("tick error: %r" % e)
        finally:
            self.root.after(POLL_MS, self._tick)

    # ── 列表渲染 ──────────────────────────────────────────────────────────────
    def _render_list(self, states):
        row_h = self._row_h()
        fsize = self._font_size()
        w = self.root.winfo_width()
        body_h = self._body_h()
        total_h = len(states) * row_h

        # 限制滚动范围
        max_off = max(0, total_h - body_h)
        self.scroll_off = min(self.scroll_off, max_off)

        # 渲染签名：内容/尺寸/滚动无变化则跳过重绘
        sig = (w, body_h, self.scroll_off,
               tuple((s["session_id"], s["status"], s["label"]) for s in states))
        if sig == self._last_sig:
            return
        self._last_sig = sig

        c = self.canvas
        c.delete("all")
        font = tkfont.Font(family="Microsoft YaHei UI", size=fsize)
        dot_r = max(4, int(row_h * 0.14))
        pad = 12

        for idx, s in enumerate(states):
            y0 = idx * row_h - self.scroll_off
            yc = y0 + row_h / 2
            if y0 + row_h < 0 or y0 > body_h:
                continue  # 视口外不画
            # 分隔线
            if idx > 0:
                c.create_line(pad, y0, w - pad, y0, fill=SEP)
            # 色点（右侧）
            cx = w - pad - dot_r
            color = DOT_COLORS.get(s["status"], FG_DIM)
            c.create_oval(cx - dot_r, yc - dot_r, cx + dot_r, yc + dot_r,
                          fill=color, outline="")
            # 标签（左侧，超宽省略号）
            max_text = (cx - dot_r - 6) - pad
            text = truncate_to_width(font, s["label"], max(10, max_text))
            c.create_text(pad, yc, text=text, fill=FG, font=font, anchor="w")

        # 溢出滚动指示（细 thumb）
        if total_h > body_h:
            frac_h = body_h / total_h
            thumb_h = max(16, body_h * frac_h)
            track = body_h - thumb_h
            ty = (self.scroll_off / max_off) * track if max_off else 0
            c.create_rectangle(w - 3, ty, w - 1, ty + thumb_h,
                               fill="#555555", outline="")

    # ── 宠物渲染 (F3)：程序化像素，按逻辑网格缩放居中用方块画出 ─────────────────
    def _render_pet(self):
        _, pixels = self.pixel_pet.render()
        w = self.root.winfo_width()
        body_h = self._body_h()

        # 内容/尺寸无变化则跳过重绘
        sig = (w, body_h, self.pixel_pet.idx, self.pixel_pet.frame)
        if sig == self._last_pet_sig:
            return
        self._last_pet_sig = sig

        c = self.canvas
        c.delete("all")

        # 整数像素缩放，保持方块感；留点边距
        scale = max(1, min((w - 8) // pixelpet.LOGICAL_W,
                           (max(8, body_h) - 4) // pixelpet.LOGICAL_H))
        draw_w = pixelpet.LOGICAL_W * scale
        draw_h = pixelpet.LOGICAL_H * scale
        ox = (w - draw_w) // 2
        oy = (body_h - draw_h) // 2
        for (gx, gy, col) in pixels:
            x0 = ox + gx * scale
            y0 = oy + gy * scale
            c.create_rectangle(x0, y0, x0 + scale, y0 + scale,
                               fill=col, outline="")

    def _animate(self):
        if self.in_pet_mode:
            self.pixel_pet.step(time.time())
            self._render_pet()
        self.root.after(ANIM_MS, self._animate)

    # ── 残留清理 (F6) ─────────────────────────────────────────────────────────
    def _reap(self):
        try:
            states = read_states(state_dir())
            if states:
                pids = [s["pid"] for s in states if s["pid"] > 0]
                alive = query_alive_pids(pids) if pids else set()
                dead = reaper_decide(states, alive, time.time())
                for sid in dead:
                    for s in states:
                        if s["session_id"] == sid:
                            try:
                                os.remove(s["_path"])
                                log("reaped %s (pid=%d status=%s)"
                                    % (sid, s["pid"], s["status"]))
                            except OSError:
                                pass
                            break
                if dead:
                    self._last_sig = None
        except Exception as e:
            log("reap error: %r" % e)
        finally:
            self.root.after(REAP_MS, self._reap)

    # ── 标题栏动作 ────────────────────────────────────────────────────────────
    def toggle_pin(self):
        self.cfg["always_on_top"] = not self.cfg["always_on_top"]
        self.root.attributes("-topmost", self.cfg["always_on_top"])
        self._refresh_pin()
        self._save_config()

    def close(self):
        self.cfg["pos_x"] = self.root.winfo_x()
        self.cfg["pos_y"] = self.root.winfo_y()
        self._save_config()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    # ── 最小化到系统托盘 ──────────────────────────────────────────────────────
    def minimize_to_tray(self):
        if self.tray is None:
            log("minimize ignored: tray unavailable")
            return
        try:
            self.root.withdraw()        # 仅隐藏主面板；托盘图标常驻不动
        except Exception as e:
            log("minimize failed: %r" % e)

    def _restore_from_tray(self):
        # 点托盘图标 → 显示主面板。图标常驻，不在此移除。
        try:
            self.root.deiconify()
            self.root.overrideredirect(True)    # 部分环境 deiconify 会丢无边框，重置
            self.root.attributes("-topmost", self.cfg["always_on_top"])
            self.root.lift()
        except Exception as e:
            log("restore failed: %r" % e)

    def _poll_tray(self):
        """主线程消费托盘线程投递的动作（show/exit）。"""
        try:
            while True:
                action = self._tray_q.get_nowait()
                if action == "show":
                    self._restore_from_tray()
                elif action == "exit":
                    self.close()
                    return
        except queue.Empty:
            pass
        self.root.after(150, self._poll_tray)

    # ── 设置面板 (F5) ─────────────────────────────────────────────────────────
    def toggle_settings(self):
        if self.settings_win and tk.Toplevel.winfo_exists(self.settings_win):
            self.settings_win.destroy()
            self.settings_win = None
            return
        self._open_settings()

    def _open_settings(self):
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.configure(bg=BG)
        win.overrideredirect(True)          # 无边框，与主面板风格一致
        win.attributes("-topmost", True)
        win.resizable(False, False)

        # ── 自定义标题栏（含拖拽 + 关闭）────────────────────────────────────────
        hdr = tk.Frame(win, bg=TITLE_BG, height=TITLEBAR_H)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="⚙  设置", bg=TITLE_BG, fg=FG,
                 font=("Microsoft YaHei UI", 9, "bold"), padx=10).pack(side="left")
        btn_x = tk.Label(hdr, text="✕", bg=TITLE_BG, fg=FG_DIM,
                         font=("Segoe UI", 10), padx=8, cursor="hand2")
        btn_x.pack(side="right")
        btn_x.bind("<Button-1>", lambda e: self._close_settings())
        btn_x.bind("<Enter>", lambda e: btn_x.config(fg=FG))
        btn_x.bind("<Leave>", lambda e: btn_x.config(fg=FG_DIM))

        _sd = [0, 0]
        def _sw_drag_start(e):
            _sd[0] = e.x_root - win.winfo_x()
            _sd[1] = e.y_root - win.winfo_y()
        def _sw_drag_move(e):
            win.geometry("+%d+%d" % (e.x_root - _sd[0], e.y_root - _sd[1]))
        for widget in (hdr,) + tuple(hdr.winfo_children()):
            if widget is not btn_x:
                widget.bind("<Button-1>", _sw_drag_start, add="+")
                widget.bind("<B1-Motion>", _sw_drag_move, add="+")

        # ── 内容区 ────────────────────────────────────────────────────────────
        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True)

        def _mk_sep():
            tk.Frame(body, bg=SEP, height=1).pack(fill="x", padx=14, pady=(0, 4))

        def _mk_section(text):
            tk.Label(body, text=text, bg=BG, fg="#525252",
                     font=("Microsoft YaHei UI", 7), anchor="w",
                     padx=14).pack(fill="x", pady=(8, 2))

        def _mk_slider(label, var, lo, hi, cmd):
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", padx=14, pady=(4, 0))
            tk.Label(row, text=label, bg=BG, fg=FG,
                     font=("Microsoft YaHei UI", 9), anchor="w").pack(side="left")
            tk.Label(row, textvariable=var, bg=BG, fg=ACCENT,
                     font=("Microsoft YaHei UI", 9, "bold"),
                     width=4, anchor="e").pack(side="right")
            tk.Scale(body, from_=lo, to=hi, orient="horizontal",
                     variable=var, showvalue=False, bg=BG, fg=FG,
                     troughcolor="#2E2E2E", highlightthickness=0,
                     activebackground=ACCENT, command=cmd).pack(
                         fill="x", padx=14, pady=(2, 4))

        def _mk_spinrow(label, var, lo, hi):
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", padx=14, pady=3)
            tk.Label(row, text=label, bg=BG, fg=FG,
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
            sp = tk.Spinbox(row, from_=lo, to=hi, textvariable=var, width=7,
                            command=self._on_size, bg="#272727", fg=FG,
                            buttonbackground="#333333", relief="flat",
                            highlightthickness=1, highlightcolor=ACCENT,
                            highlightbackground=SEP, insertbackground=FG)
            sp.pack(side="right")
            sp.bind("<Return>", lambda e: self._on_size())
            sp.bind("<FocusOut>", lambda e: self._on_size())

        def _mk_check(label, var, cmd):
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", padx=14, pady=3)
            tk.Label(row, text=label, bg=BG, fg=FG,
                     font=("Microsoft YaHei UI", 9), anchor="w").pack(side="left")
            tk.Checkbutton(row, variable=var, command=cmd, bg=BG, fg=FG,
                           activebackground=BG, selectcolor="#272727",
                           highlightthickness=0, bd=0, takefocus=0).pack(side="right")

        _mk_section("显 示")
        _mk_sep()
        self.var_op = tk.IntVar(value=self.cfg["opacity"])
        _mk_slider("透明度 (%)", self.var_op, *CONFIG_RANGES["opacity"], self._on_opacity)

        self.var_font = tk.IntVar(value=self.cfg["font_level"])
        _mk_slider("字号偏移", self.var_font, *CONFIG_RANGES["font_level"], self._on_font)

        _mk_section("尺 寸")
        _mk_sep()
        self.var_w = tk.IntVar(value=self.cfg["width"])
        self.var_h = tk.IntVar(value=self.cfg["height"])
        _mk_spinrow("宽 (px)", self.var_w, *CONFIG_RANGES["width"])
        _mk_spinrow("高 (px)", self.var_h, *CONFIG_RANGES["height"])

        _mk_section("提 示")
        _mk_sep()
        self.var_sound = tk.BooleanVar(value=self.cfg["sound_enabled"])
        _mk_check("红点亮起播放提示音", self.var_sound, self._on_sound)

        tk.Label(body, text="改动即时生效", bg=BG, fg="#3A3A3A",
                 font=("Microsoft YaHei UI", 7)).pack(pady=(6, 10))

        # ── 定位：贴紧主面板上方（不够则退到下方）────────────────────────────────
        win.update_idletasks()
        win_h = win.winfo_reqheight()
        mx, my = self.root.winfo_x(), self.root.winfo_y()
        x = max(0, mx)
        y = my - win_h - 6
        if y < 0:
            y = my + self.root.winfo_height() + 6
        win.geometry("+%d+%d" % (x, y))
        win.focus_force()

        win.protocol("WM_DELETE_WINDOW", self._close_settings)

    def _close_settings(self):
        if self.settings_win:
            self.settings_win.destroy()
            self.settings_win = None

    def _on_sound(self, _v=None):
        self.cfg["sound_enabled"] = bool(self.var_sound.get())
        self._save_config()

    def _on_opacity(self, _v=None):
        v = max(CONFIG_RANGES["opacity"][0],
                min(CONFIG_RANGES["opacity"][1], int(self.var_op.get())))
        self.cfg["opacity"] = v
        self.root.attributes("-alpha", v / 100.0)
        self._save_config()

    def _on_font(self, _v=None):
        lo, hi = CONFIG_RANGES["font_level"]
        lv = max(lo, min(hi, int(self.var_font.get())))
        self.cfg["font_level"] = lv
        self._last_sig = None          # 强制重绘
        if not self.in_pet_mode:
            self._render_list(read_states(state_dir()))
        self._save_config()

    def _on_size(self, _v=None):
        try:
            w = int(self.var_w.get())
            h = int(self.var_h.get())
        except (tk.TclError, ValueError):
            return
        lo, hi = CONFIG_RANGES["width"]
        w = max(lo, min(hi, w))
        lo, hi = CONFIG_RANGES["height"]
        h = max(lo, min(hi, h))
        self.var_w.set(w)
        self.var_h.set(h)
        self.cfg["width"], self.cfg["height"] = w, h
        self._resize()
        self._save_config()

    # ── 持久化 ────────────────────────────────────────────────────────────────
    def _save_config(self):
        try:
            atomic_write_json(config_path(), clamp_config(self.cfg))
        except Exception as e:
            log("save config failed: %r" % e)

    # ── 运行 ──────────────────────────────────────────────────────────────────
    def run(self):
        self.root.mainloop()


def main():
    CCPulseViewer().run()


if __name__ == "__main__":
    main()
