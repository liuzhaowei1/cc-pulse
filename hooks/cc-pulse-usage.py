#!/usr/bin/env python3
"""
CC Pulse 额度上报端（statusLine 脚本）
Claude Code 只在 statusLine 的 stdin 里提供账号额度 rate_limits（hook 与 transcript 均无），
本脚本收到后合并写入 usage.json，供 Viewer 在标题栏下显示「5h% · 重置时刻 · 周%」。

注册方式：~/.claude/settings.json
    "statusLine": {"type": "command", "command": "python3 ~/.claude/hooks/cc-pulse-usage.py"}
已有自己的 statusLine 时（statusLine 只能配一个命令）：在你的脚本里把收到的 stdin
原样转给本脚本即可，例如 bash：input=$(cat); echo "$input" | python3 ~/.claude/hooks/cc-pulse-usage.py
不向 stdout 输出任何内容 → 终端状态栏不多占一行。

合并规则（多会话并发写同一文件，旧会话可能带着过时数据触发）：
    每个窗口按 resets_at 判新旧：更晚或相同 = 采用最新值（相同窗口不再取较大值，
    否则重置卡清零用量后会被旧的 100% 卡住）；更早 = 过时数据，忽略。stdin 无 rate_limits（非订阅路由/首轮回复前）时不动文件。

设计铁律：任何异常都不得影响 claude 运行 —— 全程 try/except + exit 0。
"""
import sys, os, json, tempfile, time, importlib.util


def _base_dir():
    """与 F1 上报端（同目录 cc-pulse-report.py）用同一套目录探测：state 目录的上一级。"""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "cc_pulse_report", os.path.join(here, "cc-pulse-report.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return os.path.dirname(mod.STATE_DIR)


try:
    BASE_DIR = _base_dir()
except Exception:                   # 上报端缺失/异常也不能让状态栏报错
    BASE_DIR = os.path.join(os.path.expanduser("~"), ".cc-pulse")
USAGE_FILE = os.path.join(BASE_DIR, "usage.json")
WINDOWS = ("five_hour", "seven_day")


def pick_window(new, old):
    """返回应保留的窗口数据 {"used_percentage", "resets_at"} 或 None。"""
    try:
        n_reset = int(new["resets_at"])
        n = {"used_percentage": float(new["used_percentage"]), "resets_at": n_reset}
    except Exception:
        n = None
    if not old:
        return n
    if n is None:
        return old
    o_reset = old.get("resets_at", 0)
    if n_reset >= o_reset:      # 相同窗口也直接采用新值：重置卡只清零用量、不换 resets_at
        return n
    return old


def merge(rate_limits, cur):
    out = {}
    for w in WINDOWS:
        v = pick_window(rate_limits.get(w) or {}, (cur or {}).get(w))
        if v:
            out[w] = v
    return out


def load_usage():
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def replace_retry(src, dst, tries=5, delay=0.02):
    """Viewer 读文件的瞬间 NTFS 不允许替换，短暂重试（同 cc-pulse-report.py）。"""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(delay * (i + 1))


def atomic_write(payload):
    os.makedirs(BASE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=BASE_DIR, prefix="usage.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        replace_retry(tmp, USAGE_FILE)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def main():
    data = json.load(sys.stdin)
    rl = data.get("rate_limits")
    if not isinstance(rl, dict) or not any(rl.get(w) for w in WINDOWS):
        return
    cur = load_usage()
    new = merge(rl, cur)
    if new and new != cur:          # 没变化就不写，少一次与 Viewer 抢文件
        atomic_write(new)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
