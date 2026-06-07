#!/usr/bin/env python3
"""
CC Pulse 状态上报端 (F1)
由 Claude Code 的 6 个 hook 事件共同调用，依据 hook_event_name 计算会话三态，
原子写入 Windows 可读的 state 目录，供 Windows 悬浮窗 (Viewer) 渲染。

注册方式：在 ~/.claude/settings.json 的对应事件下挂
    {"type": "command", "command": "python3 /home/leo/.claude/hooks/cc-pulse-report.py"}
事件 → 动作：
    SessionStart      -> 注册会话，status=done(空闲/就绪)
    UserPromptSubmit  -> status=busy
    PreToolUse        -> status=busy（含权限批准后续跑）
    Notification      -> 若当前 busy 则 needs_you，否则保持（区分阻塞 vs 闲置提醒）
    Stop              -> status=done
    SessionEnd        -> 删除该会话 state 文件

设计铁律：任何异常都不得影响 claude 运行 —— 全程 try/except + exit 0。
"""
import sys, os, json, tempfile, datetime, glob, pathlib

# 动态获取 Windows 用户名（支持 WSL、Linux、macOS）
def get_state_dir():
    """获取 state 目录，优先使用环境变量，否则动态获取用户名"""
    # 方法 1：使用环境变量（最可靠）
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return os.path.join(local_app_data, "cc-pulse", "state")

    # 方法 2：从 /mnt/c/ 动态获取用户名（WSL）
    users_dir = pathlib.Path("/mnt/c/Users")
    if users_dir.exists():
        # 过滤掉 Default、Public 等系统用户
        system_users = {"Default", "Public", "All Users", "Default User", "desktop.ini"}
        for user_dir in users_dir.iterdir():
            if user_dir.is_dir() and user_dir.name not in system_users:
                return f"/mnt/c/Users/{user_dir.name}/AppData/Local/cc-pulse/state"

    # 方法 3：使用 HOME 目录回退
    home = os.environ.get("HOME", os.path.expanduser("~"))
    return os.path.join(home, ".cc-pulse", "state")

STATE_DIR = get_state_dir()
LOG_FILE = os.path.join(STATE_DIR, "reporter.log")

# 闲置提醒类 notification 不应把已完成的会话翻成 needs_you
STATUS_DONE = "done"
STATUS_BUSY = "busy"
STATUS_NEEDS_YOU = "needs_you"


def log(msg):
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
    """从本脚本进程往上走进程树，找到 cmdline 含 'claude' 的祖先 PID（即本会话的 claude 主进程），
    供 Viewer 端 reaper 用 kill -0 判活。找不到返回 0（回退到超时判废）。"""
    try:
        pid = os.getppid()
        for _ in range(20):
            if pid <= 1:
                break
            cmd = proc_cmdline(pid)
            if "claude" in cmd.lower():
                return pid
            ppid = read_proc_field(pid, "PPid")
            if not ppid:
                break
            pid = int(ppid)
    except Exception:
        pass
    return 0


def title_from_transcript(transcript_path):
    """
    从 transcript 读会话标题。返回 (custom_title, ai_title)，取各自最后一条。
      /rename → {"type":"custom-title","customTitle":...}
      自动     → {"type":"ai-title","aiTitle":...}
    只对含关键串的行做 json 解析，避免逐行解析大文件。任何异常返回 ("","")。
    """
    custom, ai = "", ""
    if not transcript_path:
        return custom, ai
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                if "custom-title" not in line and "ai-title" not in line:
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
    except Exception:
        pass
    return custom, ai


def resolve_label(data):
    """label 解析优先级：CC_LABEL 环境变量 > /rename 自定义标题 > 自动标题 > cwd 目录名。"""
    env_label = os.environ.get("CC_LABEL", "").strip()
    if env_label:
        return env_label[:40]
    custom, ai = title_from_transcript(data.get("transcript_path"))
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


def atomic_write(session_id, payload):
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=session_id + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, state_path(session_id))  # 同目录同 fs，原子替换
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def write_status(data, status):
    session_id = data.get("session_id")
    if not session_id:
        return
    payload = {
        "session_id": session_id,
        "status": status,
        "label": resolve_label(data),
        "cwd": data.get("cwd", ""),
        "pid": find_claude_pid(),
        "updated_at": now_iso(),
        "wt_session": os.environ.get("WT_SESSION", ""),
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
            # 关键规则：当前 busy → needs_you（任务中途阻塞）；当前 done → 保持（闲置提醒）
            cur = load_state(session_id)
            cur_status = cur.get("status") if cur else None
            if cur_status == STATUS_BUSY:
                write_status(data, STATUS_NEEDS_YOU)
            elif cur is None:
                # 没有记录时收到通知，保守置 needs_you（确实有事找你）
                write_status(data, STATUS_NEEDS_YOU)
            # cur_status 为 done/needs_you 时不动
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
