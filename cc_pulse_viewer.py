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
  F5 设置：透明度/宽/高/深浅主题 + 即时原子持久化 config.json
  F6 残留会话清理：后台线程每 5s 经 wsl.exe 对各 pid 做 kill -0，死进程文件删除；pid=0 走超时
"""

import glob
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
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

# 三态色点（PRD F2-3）。深色主题的色值，也用作合法 status 的白名单
DOT_COLORS = {
    STATUS_NEEDS_YOU: "#E5484D",   # 🔴 等你操作（最扎眼）
    STATUS_BUSY: "#F5C518",        # 🟡 忙
    STATUS_DONE: "#46C28E",        # 🟢 已完成 / 空闲
}
# 排序时让红 > 黄 > 绿在同一 updated_at 下更靠前（次级权重）
STATUS_RANK = {STATUS_NEEDS_YOU: 0, STATUS_BUSY: 1, STATUS_DONE: 2}

# 视觉：两套主题调色板，config["theme"] 选其一
THEMES = {
    "dark": {
        "bg": "#1E1E1E", "title_bg": "#2A2A2A", "fg": "#FFFFFF", "fg_dim": "#9A9A9A",
        "sep": "#383838", "accent": "#46C28E", "thumb": "#555555",
        "tip_bg": "#000000", "tip_fg": "#EEEEEE",
        "section": "#525252", "hint": "#3A3A3A", "trough": "#2E2E2E",
        "input_bg": "#272727", "btn_bg": "#333333",
        "border": None,              # 深色面板与桌面反差足够，不描边
        "dots": dict(DOT_COLORS),
        "pet_remap": {},
    },
    "light": {
        "bg": "#F7F7F5", "title_bg": "#EAEAE7", "fg": "#1F1F1F", "fg_dim": "#6E6E6E",
        "sep": "#DDDDD9", "accent": "#1F9D6B", "thumb": "#BDBDB8",
        "tip_bg": "#333333", "tip_fg": "#F5F5F5",
        "section": "#9A9A96", "hint": "#B8B8B3", "trough": "#DCDCD8",
        "input_bg": "#FFFFFF", "btn_bg": "#E2E2DE",
        "border": "#C4C4BF",         # 浅色面板叠在白色窗口上会没有边界，描 1px 边
        # 白底上黄色太浅、绿色偏亮，各压暗一档；红色不变
        "dots": {STATUS_NEEDS_YOU: "#E5484D", STATUS_BUSY: "#E0A800",
                 STATUS_DONE: "#2FA876"},
        # 宠物里为深色背景设计的浅色像素（气泡/z/星光）在白底上看不清，渲染时替换
        "pet_remap": {pixelpet.WHITE: "#CFC7BE", pixelpet.ZCOL: "#6F95BA",
                      pixelpet.GOLD: "#E3A92A"},
    },
}

TITLEBAR_H = 26
USAGE_H = 20                # 标题栏下的额度行高度（px）；无额度数据时整行隐藏、不占高度
USAGE_WARN = 60             # 额度已用 ≥60% 黄、≥85% 红（与 ctx-band 同阈值）
USAGE_CRIT = 85
GRIP_H = 8                 # 底边高度调节热区厚度（px）：与背景同色不可见，靠近即变拉伸光标
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
    "theme": "dark",           # "dark" / "light"
    "dock_edge": None,         # 贴边隐藏：None=自由浮动；"left"/"right"/"top"=贴在该边（pos 存展开时的坐标）
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

# 贴边隐藏：拖到工作区左/右/上边缘松手即吸附；鼠标离开后滑出屏幕，只留一条带色点的窄边
DOCK_EDGES = ("left", "right", "top")    # 下边是任务栏，不吸附
DOCK_SNAP = 20             # 松手时离边缘 ≤ 此距离(px)即吸附
DOCK_STRIP = 12            # 收起后露出的窄边厚度(px)，每个会话画一个色点
DOCK_DOT = 6               # 窄边色点直径(px)
DOCK_DOT_GAP = 5           # 窄边色点间距(px)
DOCK_EXPAND_S = 0.15       # 鼠标在窄边上停留多久才展开（防路过误触）
DOCK_COLLAPSE_S = 0.6      # 鼠标离开面板多久后收起
DOCK_POP_S = 2.0           # 贴边收起时有会话变红：自动弹出的时长
DOCK_ANIM_MS = 180         # 滑入/滑出动画总时长
DOCK_FRAME_MS = 20         # 动画帧间隔
DOCK_POLL_MS = 50          # 贴边时轮询鼠标位置的周期（tk 的 Enter/Leave 在子控件间会抖，不用）
DOCK_BLINK_S = 0.5         # 窄边红点闪烁半周期


# ────────────────────────────────────────────────────────────────────────────
# 资源路径解析（区别于 base_dir：assets 随脚本/exe 走，不在 %LOCALAPPDATA%）
# 冻结态（PyInstaller onefile）→ sys._MEIPASS/assets；开发态 → 脚本同级 assets
# ────────────────────────────────────────────────────────────────────────────

def apply_border(win, theme):
    """按主题给无边框窗口描 1px 边（border 为 None 则不描）。"""
    b = theme["border"]
    win.configure(highlightthickness=1 if b else 0,
                  highlightbackground=b or theme["bg"], highlightcolor=b or theme["bg"])


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
    # 开发/自测回退（WSL 下直接跑）：找 Viewer 已创建过的 cc-pulse 目录，与 F1 reporter 同一规则
    found = glob.glob("/mnt/c/Users/*/AppData/Local/" + APP_NAME)
    if found:
        return max(found, key=os.path.getmtime)
    return os.path.join(os.path.expanduser("~"), ".cc-pulse")


def state_dir():
    return os.path.join(base_dir(), "state")


def config_path():
    return os.path.join(base_dir(), "config.json")


def usage_path():
    # 由 statusLine 脚本 cc-pulse-usage.py 写入（不放 state/，免得被当成会话文件扫描）
    return os.path.join(base_dir(), "usage.json")


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


def read_usage(path):
    """读额度文件；不存在/损坏返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            u = json.load(f)
        return u if isinstance(u, dict) else None
    except Exception:
        return None


def usage_segments(u, now_ts):
    """额度 → [(文本, 级别)]，依次为 5h 已用% · 5h 重置时刻 · 周已用%。
    级别 normal/warn/crit/sep。窗口已过 resets_at 视为失效显示 --；两个窗口都失效/缺失返回 None（整行隐藏）。"""
    def live(key):
        w = (u or {}).get(key)
        try:
            if w and float(w["resets_at"]) > now_ts:
                return float(w["used_percentage"]), float(w["resets_at"])
        except Exception:
            pass
        return None

    def pct(v):
        if v is None:
            return ("--", "normal")
        p = int(round(v[0]))
        lv = "crit" if p >= USAGE_CRIT else "warn" if p >= USAGE_WARN else "normal"
        return ("%d%%" % p, lv)

    five, week = live("five_hour"), live("seven_day")
    if five is None and week is None:
        return None
    reset = datetime.fromtimestamp(five[1]).strftime("%H:%M") if five else "--"
    sep = (" · ", "sep")
    return [pct(five), sep, (reset, "normal"), sep, pct(week)]


def format_tokens(t):
    """上下文占用 token → 紧凑显示。<10k 保留一位小数(4.6k)，≥10k 取整(35k/235k)，
    与 /context 风格一致。0/缺失 → 空串（不显示）。"""
    try:
        t = int(t)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    if t >= 10000:
        return "%dk" % round(t / 1000)
    if t >= 1000:
        return "%.1fk" % (t / 1000)
    return str(t)


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
            "ctx_tokens": int(obj.get("ctx_tokens") or 0),
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
    if out["theme"] not in THEMES:
        out["theme"] = CONFIG_DEFAULTS["theme"]
    if out["dock_edge"] not in DOCK_EDGES:
        out["dock_edge"] = None
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
        # wsl.exe 自身报错（如实例未就绪）输出 UTF-16，按本地码页解码会失败，
        # 读线程异常后 stdout 为 None —— 用 errors="replace" 并兜底空串
        r = subprocess.run(
            ["wsl.exe", "bash", "-c", inner],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, **kwargs
        )
    except Exception:
        return None
    # 不能用 returncode 判成败：bash -c 的退出码 = 最后一条命令的退出码，最后一个
    # 探测 pid 若已死，"kill -0 && echo" 短路会让整体退出码为 1，把这份本来正确的
    # 判活结果整个误丢成 None（退回超时判废）。改以 __WSLOK__ 哨兵确认脚本确实跑过；
    # 各 pid 存活与否由它自己有没有 echo 出来表达，与整体退出码无关。
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
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


def work_area(screen_w, screen_h):
    """主屏工作区 (left, top, right, bottom)，即扣掉任务栏的部分。
    Windows 走 SPI_GETWORKAREA；取不到（或非 Windows）退回整屏。
    进程未声明 DPI 感知，tk 坐标与该 API 返回值同为系统缩放后的逻辑坐标，可直接比较。"""
    if IS_WINDOWS:
        try:
            import ctypes
            import ctypes.wintypes as wintypes
            r = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
                return r.left, r.top, r.right, r.bottom
        except Exception as e:
            log("work_area failed: %r" % e)
    return 0, 0, screen_w, screen_h


def dock_edge_for(x, y, width, height, area, snap=DOCK_SNAP):
    """松手位置离哪条可吸附边 ≤ snap（越过边缘也算）；都不靠则 None。左右优先于上。"""
    left, top, right, _bottom = area
    if x - left <= snap:
        return "left"
    if right - (x + width) <= snap:
        return "right"
    if y - top <= snap:
        return "top"
    return None


def dock_geometry(edge, x, y, width, height, area, collapsed, strip=DOCK_STRIP):
    """贴边时窗口左上角坐标。展开＝整窗贴齐该边；收起＝滑出屏幕只露 strip 宽。
    沿边方向的坐标夹在工作区内（面板比工作区还长时贴齐起点）。"""
    left, top, right, bottom = area
    if edge in ("left", "right"):
        y = max(top, min(bottom - height, y))
        if edge == "left":
            x = left - (width - strip) if collapsed else left
        else:
            x = right - strip if collapsed else right - width
    elif edge == "top":
        x = max(left, min(right - width, x))
        y = top - (height - strip) if collapsed else top
    return x, y


def strip_dots(statuses, length, dot=DOCK_DOT, gap=DOCK_DOT_GAP):
    """窄边色点沿边排布：按给定顺序居中排开，返回 [(偏移, status)]（偏移=色点起点）。
    放不下时截掉多余的，但保证最紧急的状态一定在内（先按紧急度挑，再按原顺序排）。"""
    cap = max(0, (length - 2 * gap + gap) // (dot + gap))
    if not statuses or cap == 0:
        return []
    if len(statuses) > cap:
        keep = sorted(range(len(statuses)),
                      key=lambda i: (STATUS_RANK.get(statuses[i], 9), i))[:cap]
        statuses = [statuses[i] for i in sorted(keep)]
    span = len(statuses) * dot + (len(statuses) - 1) * gap
    start = (length - span) // 2
    return [(start + i * (dot + gap), st) for i, st in enumerate(statuses)]


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
    def __init__(self, widget, text, theme):
        self.widget = widget
        self.text = text
        self.theme = theme           # 无参可调用，返回当前调色板（显示时才取，跟随主题切换）
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
        t = self.theme()
        tk.Label(self.tip, text=self.text, bg=t["tip_bg"], fg=t["tip_fg"],
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
        self.T = THEMES[self.cfg["theme"]]    # 当前调色板
        self.root.configure(bg=self.T["bg"])
        apply_border(self.root, self.T)
        self.root.overrideredirect(True)              # 无边框
        self.root.attributes("-topmost", self.cfg["always_on_top"])
        self.root.attributes("-alpha", self.cfg["opacity"] / 100.0)

        # 贴边隐藏状态（_place_window 会按 config 恢复贴边，须先就位）
        self.dock_edge = None        # None=自由浮动；否则为贴住的边
        self.dock_collapsed = False  # 贴边时是否已收起成窄边
        self._dock_xy = (0, 0)       # 贴边展开时的窗口坐标（沿边位置以此为准，持久化也存它）
        self._dock_anim = None       # 进行中的滑动动画 after id
        self._hover_since = None     # 收起态：鼠标进入窄边的时刻
        self._leave_since = None     # 展开态：鼠标离开面板的时刻
        self._pop_until = 0.0        # 变红自动弹出的截止时刻
        self._dock_strip_sig = None  # 窄边渲染签名
        self._dragging = False       # 拖拽/拉伸中不做收起判定
        self._states = []            # 最近一轮会话快照（窄边色点用）

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
        self._usage = None           # 额度文件内容缓存（按 mtime 失效）
        self._usage_mtime = None
        self._usage_sig = None       # 额度行渲染签名
        self._usage_shown = False    # 额度行是否已 pack（影响主体可用高度）
        self._prev_status = {}      # session_id → 上一轮 status，用于红点边沿检测
        self._last_alert_t = 0.0     # 上次播放提示音的时刻（节流）
        self._stop_evt = threading.Event()   # 通知后台 reaper 线程退出

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

        self._build_ui()

        self.root.bind("<Configure>", self._on_configure, add="+")
        # 启动各周期循环
        self.root.after(50, self._tick)
        # reaper 走后台线程：wsl.exe 每次 ~150ms、最坏 4s 超时，放主线程会卡动画和拖拽
        threading.Thread(target=self._reap_loop, name="reaper", daemon=True).start()
        self.root.after(ANIM_MS, self._animate)
        self.root.after(150, self._poll_tray)    # 消费托盘线程的动作
        self.root.after(DOCK_POLL_MS, self._dock_poll)

    # ── 窗口放置 ────────────────────────────────────────────────────────────
    def _place_window(self):
        w, h = self.cfg["width"], self.cfg["height"]
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x, y = self.cfg["pos_x"], self.cfg["pos_y"]
        if is_offscreen(x, y, w, h, sw, sh):
            x, y = primary_geometry(w, h, sw, sh)
        edge = self.cfg["dock_edge"]
        if edge:
            # 上次退出时贴着边：先展开贴好，鼠标不在上面的话 _dock_poll 随即收起
            self.dock_edge = edge
            x, y = dock_geometry(edge, x, y, w, h, self._work_area(), collapsed=False)
            self._dock_xy = (x, y)
            self.root.attributes("-topmost", True)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _resize(self):
        w, h = self.cfg["width"], self.cfg["height"]
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        if self.dock_edge:
            # 贴边时改尺寸：按新尺寸重新贴齐（右边/上边贴住的话坐标随尺寸变）
            x, y = dock_geometry(self.dock_edge, self._dock_xy[0], self._dock_xy[1],
                                 w, h, self._work_area(), collapsed=False)
            self._dock_xy = (x, y)
            if self.dock_collapsed:
                x, y = dock_geometry(self.dock_edge, x, y, w, h, self._work_area(),
                                     collapsed=True)
            self._dock_strip_sig = None
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self._last_sig = None  # 强制重绘

    def _build_ui(self):
        self._build_titlebar()
        self._build_resize_grip()      # 底边拖拽热区（须在主体之前 pack，占住底部）
        self._build_usage_bar()        # 先建不 pack，有数据时插到主体之前
        self._build_body()
        self._build_dock_strip()       # 贴边收起时才 place 到露出的那一侧

    # ── 主题切换：整体重建主面板控件（颜色散在各控件上，逐个改色不如重建可靠）──────
    def _apply_theme(self):
        self.T = THEMES[self.cfg["theme"]]
        self.root.configure(bg=self.T["bg"])
        apply_border(self.root, self.T)
        for w in (self.titlebar, self.resize_grip, self.usage_bar, self.canvas,
                  self.dock_strip):
            w.destroy()
        self._build_ui()
        self._last_sig = None
        self._last_pet_sig = None
        self._usage_sig = None
        self._usage_shown = False
        if self.dock_edge and self.dock_collapsed:
            self._show_dock_strip()
        # 设置面板开着的话按新配色重开，留在原位置
        if self.settings_win is not None:
            pos = (self.settings_win.winfo_x(), self.settings_win.winfo_y())
            self._close_settings()
            self._open_settings()
            self.settings_win.geometry("+%d+%d" % pos)

    # ── 标题栏 (F4) ──────────────────────────────────────────────────────────
    def _build_titlebar(self):
        T = self.T
        bar = tk.Frame(self.root, bg=T["title_bg"], height=TITLEBAR_H)
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
        lbl = tk.Label(parent, text=glyph, bg=self.T["title_bg"], fg=self.T["fg_dim"],
                       font=("Segoe UI Emoji", 10), padx=6, cursor="hand2")
        lbl.pack(side="right")
        lbl.bind("<Button-1>", lambda e: cmd())
        lbl.bind("<Enter>", lambda e: lbl.config(fg=self.T["fg"]))
        lbl.bind("<Leave>", lambda e: self._restore_btn_color(lbl))
        Tooltip(lbl, tip, lambda: self.T)
        return lbl

    def _restore_btn_color(self, lbl):
        if lbl is self.btn_pin and self.cfg["always_on_top"]:
            lbl.config(fg=self.T["accent"])
        else:
            lbl.config(fg=self.T["fg_dim"])

    def _refresh_pin(self):
        # 置顶 ON → 高亮强调色；OFF → 暗色
        self.btn_pin.config(
            fg=self.T["accent"] if self.cfg["always_on_top"] else self.T["fg_dim"])

    # ── 额度行：5h 已用% · 5h 重置时刻 · 周已用%（数据来自 statusLine 脚本）────────
    def _build_usage_bar(self):
        bar = tk.Canvas(self.root, bg=self.T["bg"], height=USAGE_H,
                        highlightthickness=0, bd=0)
        bar.bind("<Button-1>", self._drag_start, add="+")
        bar.bind("<B1-Motion>", self._drag_move, add="+")
        bar.bind("<ButtonRelease-1>", self._drag_end, add="+")
        self.usage_bar = bar

    def _update_usage(self):
        """每轮调用：文件变了才重读；过期判定每轮做（重置时刻一过即变 --/隐藏）。
        返回 True 表示额度行显隐变化（主体可用高度变了，调用方需强制重绘）。"""
        try:
            mtime = os.stat(usage_path()).st_mtime
        except OSError:
            mtime = None
        if mtime != self._usage_mtime:
            self._usage_mtime = mtime
            self._usage = read_usage(usage_path()) if mtime else None
        segs = usage_segments(self._usage, time.time())

        changed = (segs is not None) != self._usage_shown
        if changed:
            if segs is None:
                self.usage_bar.pack_forget()
            else:
                self.usage_bar.pack(side="top", fill="x", before=self.canvas)
            self._usage_shown = segs is not None
        if segs is None:
            return changed

        fsize = max(6, self._font_size() - 1)
        sig = (tuple(segs), fsize, self._body_w())
        if sig == self._usage_sig:
            return changed
        self._usage_sig = sig

        T = self.T
        color = {"normal": T["fg_dim"], "sep": T["fg_dim"],
                 "warn": T["dots"][STATUS_BUSY], "crit": T["dots"][STATUS_NEEDS_YOU]}
        font = tkfont.Font(family="Microsoft YaHei UI", size=fsize)
        c = self.usage_bar
        c.delete("all")
        # 逐段左对齐排开，不换行；窗口太窄时右侧自然被画布裁掉
        x, pad, yc = 12, 12, USAGE_H / 2
        for text, lv in segs:
            c.create_text(x, yc, text=text, fill=color[lv], font=font, anchor="w")
            x += font.measure(text)
        c.create_line(pad, USAGE_H - 1, self._body_w() - pad, USAGE_H - 1, fill=T["sep"])
        return changed

    # ── 主体 (Canvas: 列表 / 宠物共用) ────────────────────────────────────────
    def _build_body(self):
        self.canvas = tk.Canvas(self.root, bg=self.T["bg"], highlightthickness=0, bd=0)
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
        grip = tk.Frame(self.root, bg=self.T["bg"], height=GRIP_H,
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
        self._dragging = True
        self._drag = (e.x_root - self.root.winfo_x(),
                      e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if self.dock_edge:
            self._undock()          # 从边上拖开即回到自由浮动；松手时再判要不要重新吸附
        x = e.x_root - self._drag[0]
        y = e.y_root - self._drag[1]
        self.root.geometry("+%d+%d" % (x, y))

    def _drag_end(self, e):
        self._dragging = False
        # 用事件坐标而非 winfo_x：最后一次 geometry 可能还没在空闲时刻生效，winfo 会落后一拍
        x = e.x_root - self._drag[0]
        y = e.y_root - self._drag[1]
        if self.dock_edge:
            x, y = self._dock_xy        # 只是点了一下没拖动：仍贴着边，存展开坐标
        else:
            edge = dock_edge_for(x, y, self.root.winfo_width(), self.root.winfo_height(),
                                 self._work_area())
            if edge:
                self._dock(edge, x, y)
                x, y = self._dock_xy
        self.cfg["pos_x"], self.cfg["pos_y"] = x, y
        self.cfg["dock_edge"] = self.dock_edge
        self._save_config()

    # ── 贴边隐藏 ──────────────────────────────────────────────────────────────
    def _work_area(self):
        return work_area(self.root.winfo_screenwidth(), self.root.winfo_screenheight())

    def _want_topmost(self):
        # 贴边时强制置顶（否则窄边被别的窗口盖住）；自由浮动时听 📌
        return bool(self.dock_edge) or self.cfg["always_on_top"]

    def _dock(self, edge, x, y):
        """从 (x, y) 吸附到 edge：先展开贴齐，鼠标离开后由 _dock_poll 收起。"""
        x, y = dock_geometry(edge, x, y,
                             self.root.winfo_width(), self.root.winfo_height(),
                             self._work_area(), collapsed=False)
        self.dock_edge = edge
        self.dock_collapsed = False
        self._dock_xy = (x, y)
        self._hover_since = self._leave_since = None
        self.root.attributes("-topmost", True)
        self._slide_to(x, y)

    def _undock(self):
        self._cancel_slide()
        self.dock_edge = None
        self.dock_collapsed = False
        self._hide_dock_strip()
        self.root.attributes("-topmost", self._want_topmost())

    def _set_collapsed(self, collapsed):
        if not self.dock_edge or collapsed == self.dock_collapsed:
            return
        self.dock_collapsed = collapsed
        self._hover_since = self._leave_since = None
        x, y = dock_geometry(self.dock_edge, self._dock_xy[0], self._dock_xy[1],
                             self.root.winfo_width(), self.root.winfo_height(),
                             self._work_area(), collapsed)
        if not collapsed:
            self._hide_dock_strip()     # 展开：先撤窄边再滑出，露出完整面板
        self._slide_to(x, y, on_done=self._show_dock_strip if collapsed else None)

    def _cancel_slide(self):
        if self._dock_anim is not None:
            self.root.after_cancel(self._dock_anim)
            self._dock_anim = None

    def _slide_to(self, tx, ty, on_done=None):
        """ease-out 滑动到 (tx, ty)；新动画会接管旧动画，从当前位置继续。"""
        self._cancel_slide()
        self.root.update_idletasks()    # 先让挂起的 geometry 生效，起点才准
        sx, sy = self.root.winfo_x(), self.root.winfo_y()
        n = max(1, DOCK_ANIM_MS // DOCK_FRAME_MS)

        def step(i):
            t = i / n
            k = 1 - (1 - t) ** 3
            self.root.geometry("+%d+%d" % (round(sx + (tx - sx) * k),
                                           round(sy + (ty - sy) * k)))
            if i < n:
                self._dock_anim = self.root.after(DOCK_FRAME_MS, step, i + 1)
            else:
                self._dock_anim = None
                if on_done:
                    on_done()
        step(1)

    def _pop_dock(self, seconds=DOCK_POP_S):
        """贴边收起时临时弹出 seconds 秒（变红提醒 / 托盘点击）。鼠标移上去则按悬停逻辑接管。"""
        if not self.dock_edge:
            return
        self._pop_until = time.time() + seconds
        self._set_collapsed(False)

    def _pointer_inside(self):
        """鼠标是否在面板屏幕内可见的部分上（收起时即窄边）。"""
        px, py = self.root.winfo_pointerxy()
        l, t, r, b = self._work_area()
        x0, y0 = max(l, self.root.winfo_x()), max(t, self.root.winfo_y())
        x1 = min(r, self.root.winfo_x() + self.root.winfo_width())
        y1 = min(b, self.root.winfo_y() + self.root.winfo_height())
        return x0 <= px < x1 and y0 <= py < y1

    def _dock_poll(self):
        try:
            if self.dock_edge and self._dock_anim is None and not self._dragging \
                    and self.root.state() != "withdrawn":
                now = time.time()
                inside = self._pointer_inside()
                if self.dock_collapsed:
                    if inside:
                        if self._hover_since is None:
                            self._hover_since = now
                        elif now - self._hover_since >= DOCK_EXPAND_S:
                            self._set_collapsed(False)
                    else:
                        self._hover_since = None
                    self._draw_dock_strip()
                else:
                    # 设置面板开着不收（它贴在主面板旁边，收起会把人晾在原地）
                    if inside or self.settings_win is not None:
                        self._leave_since = None
                    elif self._leave_since is None:
                        self._leave_since = now
                    # 离开计时在弹出期间照走，弹出一结束且已离开够久即收
                    if self._leave_since is not None and now >= self._pop_until \
                            and now - self._leave_since >= DOCK_COLLAPSE_S:
                        self._set_collapsed(True)
        except Exception as e:
            log("dock poll error: %r" % e)
        finally:
            self.root.after(DOCK_POLL_MS, self._dock_poll)

    def _build_dock_strip(self):
        self.dock_strip = tk.Canvas(self.root, bg=self.T["title_bg"],
                                    highlightthickness=0, bd=0, cursor="hand2")
        # 点击窄边立即展开（悬停之外的兜底）
        self.dock_strip.bind("<Button-1>", lambda e: self._set_collapsed(False), add="+")
        self._dock_strip_sig = None

    def _show_dock_strip(self):
        """把窄边盖到面板仍露在屏幕内的那一侧。"""
        s = self.dock_strip
        if self.dock_edge == "right":
            s.place(x=0, y=0, width=DOCK_STRIP, relheight=1)
        elif self.dock_edge == "left":
            s.place(relx=1, y=0, anchor="ne", width=DOCK_STRIP, relheight=1)
        else:
            s.place(x=0, rely=1, anchor="sw", relwidth=1, height=DOCK_STRIP)
        tk.Misc.lift(s)        # Canvas.lift 是图元置顶，这里要的是控件层级
        self._dock_strip_sig = None
        self._draw_dock_strip()

    def _hide_dock_strip(self):
        self.dock_strip.place_forget()

    def _draw_dock_strip(self):
        if not (self.dock_edge and self.dock_collapsed):
            return
        vertical = self.dock_edge != "top"
        length = self.root.winfo_height() if vertical else self.root.winfo_width()
        blink_on = int(time.time() / DOCK_BLINK_S) % 2 == 0
        statuses = [s["status"] for s in self._states]
        dots = strip_dots(statuses, length)
        sig = (self.dock_edge, length, tuple(dots),
               blink_on and STATUS_NEEDS_YOU in statuses)
        if sig == self._dock_strip_sig:
            return
        self._dock_strip_sig = sig
        c = self.dock_strip
        c.delete("all")
        a = (DOCK_STRIP - DOCK_DOT) // 2       # 色点在窄边厚度方向居中
        for off, st in dots:
            if st == STATUS_NEEDS_YOU and not blink_on:
                continue                       # 红点闪烁：灭的半周期不画
            color = self.T["dots"].get(st, self.T["fg_dim"])
            x0, y0 = (a, off) if vertical else (off, a)
            c.create_oval(x0, y0, x0 + DOCK_DOT, y0 + DOCK_DOT, fill=color, outline="")

    def _on_configure(self, _e):
        pass

    # ── 滚动 ──────────────────────────────────────────────────────────────────
    def _on_wheel(self, e):
        self._wheel(-1 if e.delta > 0 else 1)

    def _wheel(self, direction):
        self.scroll_off = max(0, self.scroll_off + direction * self._row_h())
        self._last_sig = None  # 下一轮 _tick 会带着新 scroll_off 重绘

    # ── 度量 ────────────────────────────────────────────────────────────────
    def _border_px(self):
        return 2 if self.T["border"] else 0     # 两侧各 1px 描边

    def _body_w(self):
        return self.root.winfo_width() - self._border_px()

    def _body_h(self):
        usage_h = USAGE_H if self._usage_shown else 0
        return max(40, self.root.winfo_height() - TITLEBAR_H - usage_h - GRIP_H
                   - self._border_px())

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
            if self.dock_collapsed:
                self._pop_dock()

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
            if self._update_usage():
                self._last_sig = None       # 额度行显隐改变了主体高度，按新高度重排
                self._last_pet_sig = None
            states = read_states(state_dir())
            self._states = states
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
        w = self._body_w()
        body_h = self._body_h()
        total_h = len(states) * row_h

        # 限制滚动范围
        max_off = max(0, total_h - body_h)
        self.scroll_off = min(self.scroll_off, max_off)

        # 渲染签名：内容/尺寸/滚动无变化则跳过重绘
        sig = (w, body_h, self.scroll_off,
               tuple((s["session_id"], s["status"], s["label"], s.get("ctx_tokens", 0))
                     for s in states))
        if sig == self._last_sig:
            return
        self._last_sig = sig

        T = self.T
        c = self.canvas
        c.delete("all")
        font = tkfont.Font(family="Microsoft YaHei UI", size=fsize)
        ctx_font = tkfont.Font(family="Microsoft YaHei UI", size=max(6, fsize - 1))
        dot_r = max(4, int(row_h * 0.14))
        pad = 12
        # 第三列（最右）固定宽度：按最宽 token 串预留，保证色点纵向对齐——
        # 即使某些会话还没上报 ctx_tokens，色点列位置也不随之左右跳动。
        ctx_col_w = ctx_font.measure("888k")
        ctx_gap = 8

        for idx, s in enumerate(states):
            y0 = idx * row_h - self.scroll_off
            yc = y0 + row_h / 2
            if y0 + row_h < 0 or y0 > body_h:
                continue  # 视口外不画
            # 分隔线
            if idx > 0:
                c.create_line(pad, y0, w - pad, y0, fill=T["sep"])
            # 第三列：context 占用 token 数（最右，右对齐，暗色）
            ctx_str = format_tokens(s.get("ctx_tokens", 0))
            if ctx_str:
                c.create_text(w - pad, yc, text=ctx_str, fill=T["fg_dim"],
                              font=ctx_font, anchor="e")
            # 第二列：色点（在固定宽 ctx 列的左侧）
            cx = w - pad - ctx_col_w - ctx_gap - dot_r
            color = T["dots"].get(s["status"], T["fg_dim"])
            c.create_oval(cx - dot_r, yc - dot_r, cx + dot_r, yc + dot_r,
                          fill=color, outline="")
            # 第一列：标签（左侧，超宽省略号）
            max_text = (cx - dot_r - 6) - pad
            text = truncate_to_width(font, s["label"], max(10, max_text))
            c.create_text(pad, yc, text=text, fill=T["fg"], font=font, anchor="w")

        # 溢出滚动指示（细 thumb）
        if total_h > body_h:
            frac_h = body_h / total_h
            thumb_h = max(16, body_h * frac_h)
            track = body_h - thumb_h
            ty = (self.scroll_off / max_off) * track if max_off else 0
            c.create_rectangle(w - 3, ty, w - 1, ty + thumb_h,
                               fill=T["thumb"], outline="")

    # ── 宠物渲染 (F3)：程序化像素，按逻辑网格缩放居中用方块画出 ─────────────────
    def _render_pet(self):
        _, pixels = self.pixel_pet.render()
        w = self._body_w()
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
        remap = self.T["pet_remap"]
        for (gx, gy, col) in pixels:
            x0 = ox + gx * scale
            y0 = oy + gy * scale
            c.create_rectangle(x0, y0, x0 + scale, y0 + scale,
                               fill=remap.get(col, col), outline="")

    def _animate(self):
        if self.in_pet_mode:
            self.pixel_pet.step(time.time())
            self._render_pet()
        self.root.after(ANIM_MS, self._animate)

    # ── 残留清理 (F6)：后台线程，只碰文件与子进程，不碰 tk ─────────────────────
    def _reap_loop(self):
        while not self._stop_evt.wait(REAP_MS / 1000.0):
            self._reap_once()

    def _reap_once(self):
        # 删掉的文件由主线程下一轮 _tick 重读时自然反映（渲染签名会变），无需通知主线程
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
        except Exception as e:
            log("reap error: %r" % e)

    # ── 标题栏动作 ────────────────────────────────────────────────────────────
    def toggle_pin(self):
        self.cfg["always_on_top"] = not self.cfg["always_on_top"]
        self.root.attributes("-topmost", self._want_topmost())
        self._refresh_pin()
        self._save_config()

    def close(self):
        if self.dock_edge:
            self.cfg["pos_x"], self.cfg["pos_y"] = self._dock_xy   # 收起态坐标在屏外，存展开坐标
        else:
            self.cfg["pos_x"] = self.root.winfo_x()
            self.cfg["pos_y"] = self.root.winfo_y()
        self.cfg["dock_edge"] = self.dock_edge
        self._save_config()
        self._stop_evt.set()
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
            self.root.attributes("-topmost", True)    # 临时顶到最前，压过被 Win+D 顶起的桌面层
            self.root.lift()
            if not self._want_topmost():
                self.root.after(100, lambda: self.root.attributes("-topmost", False))
            if self.dock_collapsed:
                self._pop_dock()          # 贴边收起中：滑出来亮一下，鼠标不碰则自动收回
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
        T = self.T
        BG, FG, FG_DIM, ACCENT = T["bg"], T["fg"], T["fg_dim"], T["accent"]
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.configure(bg=BG)
        apply_border(win, T)
        win.overrideredirect(True)          # 无边框，与主面板风格一致
        win.attributes("-topmost", True)
        win.resizable(False, False)

        # ── 自定义标题栏（含拖拽 + 关闭）────────────────────────────────────────
        hdr = tk.Frame(win, bg=T["title_bg"], height=TITLEBAR_H)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="⚙  设置", bg=T["title_bg"], fg=FG,
                 font=("Microsoft YaHei UI", 9, "bold"), padx=10).pack(side="left")
        btn_x = tk.Label(hdr, text="✕", bg=T["title_bg"], fg=FG_DIM,
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
            tk.Frame(body, bg=T["sep"], height=1).pack(fill="x", padx=14, pady=(0, 4))

        def _mk_section(text):
            tk.Label(body, text=text, bg=BG, fg=T["section"],
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
                     troughcolor=T["trough"], highlightthickness=0,
                     activebackground=ACCENT, command=cmd).pack(
                         fill="x", padx=14, pady=(2, 4))

        def _mk_spinrow(label, var, lo, hi):
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", padx=14, pady=3)
            tk.Label(row, text=label, bg=BG, fg=FG,
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
            sp = tk.Spinbox(row, from_=lo, to=hi, textvariable=var, width=7,
                            command=self._on_size, bg=T["input_bg"], fg=FG,
                            buttonbackground=T["btn_bg"], relief="flat",
                            highlightthickness=1, highlightcolor=ACCENT,
                            highlightbackground=T["sep"], insertbackground=FG)
            sp.pack(side="right")
            sp.bind("<Return>", lambda e: self._on_size())
            sp.bind("<FocusOut>", lambda e: self._on_size())

        def _mk_check(label, var, cmd):
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", padx=14, pady=3)
            tk.Label(row, text=label, bg=BG, fg=FG,
                     font=("Microsoft YaHei UI", 9), anchor="w").pack(side="left")
            tk.Checkbutton(row, variable=var, command=cmd, bg=BG, fg=FG,
                           activebackground=BG, activeforeground=FG,
                           selectcolor=T["input_bg"],
                           highlightthickness=0, bd=0, takefocus=0).pack(side="right")

        _mk_section("显 示")
        _mk_sep()
        self.var_op = tk.IntVar(value=self.cfg["opacity"])
        _mk_slider("透明度 (%)", self.var_op, *CONFIG_RANGES["opacity"], self._on_opacity)

        self.var_font = tk.IntVar(value=self.cfg["font_level"])
        _mk_slider("字号偏移", self.var_font, *CONFIG_RANGES["font_level"], self._on_font)

        self.var_light = tk.BooleanVar(value=self.cfg["theme"] == "light")
        _mk_check("浅色主题", self.var_light, self._on_theme)

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

        tk.Label(body, text="改动即时生效", bg=BG, fg=T["hint"],
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

    def _on_theme(self, _v=None):
        self.cfg["theme"] = "light" if self.var_light.get() else "dark"
        self._save_config()
        # 重建会销毁设置面板（含正在回调的这个勾选框），推迟到回调返回后再做
        self.root.after_idle(self._apply_theme)

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
