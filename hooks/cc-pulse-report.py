#!/usr/bin/env python3
"""
CC Pulse 状态上报端 (F1)
由 Claude Code 的 6 个 hook 事件共同调用，依据 hook_event_name 计算会话三态，
原子写入 Windows 可读的 state 目录，供 Windows 悬浮窗 (Viewer) 渲染。

注册方式：在 ~/.claude/settings.json 的对应事件下挂
    {"type": "command", "command": "python3 ~/.claude/hooks/cc-pulse-report.py"}
事件 → 动作：
    SessionStart      -> 注册会话，status=done(空闲/就绪)
    UserPromptSubmit  -> status=busy
    PreToolUse        -> status=busy（含权限批准后续跑）
    Notification      -> 按 notification_type：权限/提问/子代理求助 -> needs_you；
                         用户已答完提问 -> busy；其余（闲置提醒等）不动。
                         无 notification_type（旧版 CC）时退回：busy -> needs_you
    Stop              -> status=done
    SessionEnd        -> 删除该会话 state 文件

设计铁律：任何异常都不得影响 claude 运行 —— 全程 try/except + exit 0。
"""
import sys, os, json, tempfile, datetime, time, glob, subprocess

CACHE_FILE = os.path.join(os.path.expanduser("~"), ".cache", "cc-pulse", "state_dir")


def _wsl_localappdata():
    """WSL 下向 Windows 要 %LOCALAPPDATA%，再用 wslpath 转成 /mnt/c/... 路径。失败返回 ""。"""
    cmd = "/mnt/c/Windows/System32/cmd.exe"
    if not os.path.exists(cmd):
        return ""
    try:
        win = subprocess.run([cmd, "/c", "echo %LOCALAPPDATA%"], cwd="/mnt/c",
                             capture_output=True, text=True, errors="replace",
                             timeout=3).stdout.strip()
        if not win or "%" in win:
            return ""
        return subprocess.run(["wslpath", "-u", win], capture_output=True,
                              text=True, timeout=3).stdout.strip()
    except Exception:
        return ""


def get_state_dir():
    """state 目录，须与 Windows 侧 Viewer 读的 %LOCALAPPDATA%\\cc-pulse\\state 一致：
      1. CC_PULSE_STATE_DIR 环境变量（手动指定，最高优先）
      2. LOCALAPPDATA 环境变量（Windows 原生运行，或经 WSLENV 透传）
      3. WSL：/mnt/c/Users/*/AppData/Local/cc-pulse 中已存在的那个（Viewer 首次启动即创建），
         多个时取最近修改的
      4. WSL：调 cmd.exe 取 %LOCALAPPDATA%（Viewer 还没启动过时）
      5. 回退 ~/.cc-pulse/state
    不能按「/mnt/c/Users 下第一个非系统目录」猜——Administrator 等目录会排在真实用户前面。
    3/4 的结果缓存到 CACHE_FILE（跨 /mnt/c 的 glob 每次约 25ms，hook 每次工具调用都跑），
    缓存的目录不存在了才重新探测。"""
    env = os.environ.get("CC_PULSE_STATE_DIR", "").strip()
    if env:
        return env
    la = os.environ.get("LOCALAPPDATA", "").strip()
    if la and os.path.isdir(la):
        return os.path.join(la, "cc-pulse", "state")
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cached = f.read().strip()
        if cached and os.path.isdir(os.path.dirname(cached)):
            return cached
    except Exception:
        pass
    found = glob.glob("/mnt/c/Users/*/AppData/Local/cc-pulse")
    if found:
        sdir = os.path.join(max(found, key=os.path.getmtime), "state")
    else:
        la = _wsl_localappdata()
        if not (la and os.path.isdir(la)):
            return os.path.join(os.path.expanduser("~"), ".cc-pulse", "state")
        sdir = os.path.join(la, "cc-pulse", "state")
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            f.write(sdir)
    except Exception:
        pass
    return sdir


STATE_DIR = get_state_dir()
LOG_FILE = os.path.join(STATE_DIR, "reporter.log")
LOG_MAX = 1024 * 1024          # 日志超 1MB 时只留尾部 256KB
LOG_KEEP = 256 * 1024

# 闲置提醒类 notification 不应把已完成的会话翻成 needs_you
STATUS_DONE = "done"
STATUS_BUSY = "busy"
STATUS_NEEDS_YOU = "needs_you"

# Notification 的 notification_type（见官方 hooks 文档）
NOTIFY_NEEDS_YOU = {"permission_prompt", "elicitation_dialog",
                    "elicitation_url_dialog", "agent_needs_input"}
NOTIFY_ANSWERED = {"elicitation_response", "elicitation_complete"}


def trim_log():
    try:
        if os.path.getsize(LOG_FILE) <= LOG_MAX:
            return
        with open(LOG_FILE, "rb") as f:
            f.seek(-LOG_KEEP, os.SEEK_END)
            tail = f.read()
        tail = tail[tail.find(b"\n") + 1:]      # 丢掉被截断的首行
        with open(LOG_FILE, "wb") as f:
            f.write(tail)
    except Exception:
        pass


def log(msg):
    trim_log()
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.datetime.now().isoformat(), msg))
    except Exception:
        pass


def now_iso():
    # 带本地时区的 ISO8601
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def read_proc_field(pid, name):
    """从 /proc/<pid>/status 读 PPid；cmdline 单独读。"""
    try:
        with open("/proc/%d/status" % pid, "r") as f:
            for line in f:
                if line.startswith(name + ":"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        return None
    return None


def proc_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except Exception:
        return ""


def find_claude_pid():
    """从本脚本进程往上走进程树，找到 claude 主进程 PID（与会话同生命周期），
    供 Viewer 端 reaper 用 kill -0 判活。找不到返回 0（回退到超时判废）。

    必须按可执行文件名匹配（argv[0] 的 basename == 'claude'），不能用 'claude'
    子串——否则会被 /home/<用户>/.claude/... 路径、以及跑 hook 的 /bin/bash 临时
    包装进程误命中，记到一个几秒就死的 pid，导致 pid 判活永远失效。"""
    try:
        pid = os.getppid()
        for _ in range(20):
            if pid <= 1:
                break
            argv0 = proc_cmdline(pid).split(" ", 1)[0]
            if os.path.basename(argv0) == "claude":
                return pid
            ppid = read_proc_field(pid, "PPid")
            if not ppid:
                break
            pid = int(ppid)
    except Exception:
        pass
    return 0


def title_from_transcript(transcript_path, cache=None):
    """
    从 transcript 读会话标题。返回 (custom_title, ai_title, offset)，取各自最后一条。
      /rename → {"type":"custom-title","customTitle":...}
      自动     → {"type":"ai-title","aiTitle":...}
    增量扫描：cache 为上次的 (custom, ai, offset)，只读 offset 之后新增的完整行
    （transcript 只追加；文件变短则从头重扫）。长会话 transcript 可达几十 MB，
    整文件重扫每次 hook 要 100ms+。只对含关键串的行做 json 解析。
    任何异常返回已知结果。
    """
    custom, ai, off = cache or ("", "", 0)
    if not transcript_path:
        return custom, ai, off
    try:
        if os.path.getsize(transcript_path) < off:
            custom, ai, off = "", "", 0
        with open(transcript_path, "rb") as f:
            f.seek(off)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return custom, ai, off          # 没有新的完整行
        for line in data[:end].split(b"\n"):
            if b"custom-title" not in line and b"ai-title" not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            if t == "custom-title" and d.get("customTitle"):
                custom = str(d["customTitle"]).strip()
            elif t == "ai-title" and d.get("aiTitle"):
                ai = str(d["aiTitle"]).strip()
        off += end + 1
    except Exception:
        pass
    return custom, ai, off


def context_tokens_from_transcript(transcript_path):
    """当前上下文占用 token 数 = 最后一条非 sidechain 的 assistant 消息的
    input_tokens + cache_creation_input_tokens + cache_read_input_tokens
    （这正是 API 每轮返回的完整输入账单，与 /context 同源；与模型窗口无关，
    所以是 200k / 1m 都通用的绝对值）。

    只读文件尾部约 1MB 倒序扫，避免整文件重扫；从中间截断的首行可能不完整，丢弃。
    读不到/异常返回 0（Viewer 端不显示）。"""
    if not transcript_path:
        return 0
    try:
        size = os.path.getsize(transcript_path)
        start = max(0, size - 1024 * 1024)
        with open(transcript_path, "rb") as f:
            f.seek(start)
            data = f.read()
        lines = data.split(b"\n")
        if start > 0 and len(lines) > 1:
            lines = lines[1:]          # 丢弃被截断的不完整首行
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw or b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if d.get("isSidechain") or d.get("type") != "assistant":
                continue
            u = (d.get("message") or {}).get("usage") or {}
            tot = (u.get("input_tokens", 0)
                   + u.get("cache_creation_input_tokens", 0)
                   + u.get("cache_read_input_tokens", 0))
            if tot:
                return int(tot)
    except Exception:
        pass
    return 0


def resolve_label(data, custom, ai):
    """label 解析优先级：CC_LABEL 环境变量 > /rename 自定义标题 > 自动标题 > cwd 目录名。"""
    env_label = os.environ.get("CC_LABEL", "").strip()
    if env_label:
        return env_label[:40]
    if custom:
        return custom[:40]
    if ai:
        return ai[:40]
    cwd = data.get("cwd") or ""
    base = os.path.basename(cwd.rstrip("/")) if cwd else ""
    return (base or "未命名会话")[:40]


def state_path(session_id):
    return os.path.join(STATE_DIR, session_id + ".json")


def load_state(session_id):
    try:
        with open(state_path(session_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def replace_retry(src, dst, tries=5, delay=0.02):
    """Windows 侧 Viewer 每 200ms 读一次 state 文件，恰好撞上它打开文件的瞬间，
    覆盖会报 PermissionError(13)（NTFS 不允许替换被打开的文件）。短暂重试即可；
    不重试时这次状态直接丢失，丢的是 Stop 就会一直卡在「忙」。"""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(delay * (i + 1))


def atomic_write(session_id, payload):
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=session_id + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        replace_retry(tmp, state_path(session_id))  # 同目录同 fs，原子替换
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def write_status(data, status, cur=None):
    """cur：已读出的当前 state（省一次读），为 None 时自行读取。"""
    session_id = data.get("session_id")
    if not session_id:
        return
    if cur is None:
        cur = load_state(session_id) or {}
    # 标题增量扫描的游标存在 state 里（下划线字段，Viewer 不读）
    cache = None
    if cur.get("_title_path") == data.get("transcript_path"):
        cache = (cur.get("_title_custom", ""), cur.get("_title_ai", ""),
                 int(cur.get("_title_off") or 0))
    custom, ai, off = title_from_transcript(data.get("transcript_path"), cache)
    payload = {
        "session_id": session_id,
        "status": status,
        "label": resolve_label(data, custom, ai),
        "cwd": data.get("cwd", ""),
        "pid": find_claude_pid(),
        "updated_at": now_iso(),
        "wt_session": os.environ.get("WT_SESSION", ""),
        "ctx_tokens": context_tokens_from_transcript(data.get("transcript_path")),
        "_title_path": data.get("transcript_path") or "",
        "_title_off": off,
        "_title_custom": custom,
        "_title_ai": ai,
    }
    atomic_write(session_id, payload)


def delete_state(session_id):
    try:
        os.remove(state_path(session_id))
    except FileNotFoundError:
        pass
    except Exception:
        pass


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return  # 读不到/解析不了，直接放过
    try:
        event = data.get("hook_event_name", "")
        session_id = data.get("session_id", "")
        if not session_id:
            return

        if event == "SessionStart":
            write_status(data, STATUS_DONE)
        elif event in ("UserPromptSubmit", "PreToolUse"):
            write_status(data, STATUS_BUSY)
        elif event == "Notification":
            cur = load_state(session_id)
            cur_status = cur.get("status") if cur else None
            ntype = data.get("notification_type")
            if ntype:
                if ntype in NOTIFY_NEEDS_YOU:
                    write_status(data, STATUS_NEEDS_YOU, cur)
                elif ntype in NOTIFY_ANSWERED and cur_status == STATUS_NEEDS_YOU:
                    write_status(data, STATUS_BUSY, cur)   # 用户已答完，Claude 接着跑
                # idle_prompt / agent_completed / auth_success 等：不动
            elif cur_status == STATUS_BUSY or cur is None:
                # 旧版 CC 无 notification_type：busy → needs_you（任务中途阻塞）；
                # 没有记录时保守置 needs_you；done/needs_you 时不动（闲置提醒）
                write_status(data, STATUS_NEEDS_YOU, cur)
        elif event == "Stop":
            write_status(data, STATUS_DONE)
        elif event == "SessionEnd":
            delete_state(session_id)
    except Exception as e:
        log("ERROR event=%s %r" % (data.get("hook_event_name", "?"), e))
    # 永远成功退出，绝不阻塞 claude


if __name__ == "__main__":
    main()
    sys.exit(0)
