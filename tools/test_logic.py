#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Headless 单测：在没有 tkinter 的 WSL 上验证 Viewer 的纯逻辑。
stub 掉 tkinter / tkinter.font 后导入模块，逐项断言。
    python3 tools/test_logic.py
"""
import os
import sys
import time
import types
import json
import tempfile

# ── stub tkinter，让模块可在无 GUI 环境导入 ──
for name in ("tkinter", "tkinter.font"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["tkinter"].TclError = type("TclError", (Exception,), {})

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cc_pulse_viewer as v  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  ", name)
    else:
        FAIL += 1
        print("  FAIL", name)


def write(sdir, sid, status="busy", label="x", pid=111, updated=None, raw=None):
    p = os.path.join(sdir, sid + ".json")
    if raw is not None:
        with open(p, "w", encoding="utf-8") as f:
            f.write(raw)
        return p
    # 动态获取测试用的 cwd（使用临时目录）
    test_cwd = os.environ.get("HOME", "/tmp")
    obj = {
        "session_id": sid, "status": status, "label": label,
        "cwd": test_cwd, "pid": pid,
        "updated_at": updated or v.datetime.now(v.timezone.utc).isoformat(),
        "wt_session": "w",
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    return p


def main():
    d = tempfile.mkdtemp(prefix="ccpulse-test-")
    sdir = os.path.join(d, "state")
    os.makedirs(sdir)

    # ── read_states：排序 + 跳过损坏 + 状态过滤 ──
    t0 = "2026-06-05T10:00:00+08:00"
    t1 = "2026-06-05T11:00:00+08:00"
    t2 = "2026-06-05T12:00:00+08:00"
    write(sdir, "old", status="done", label="老", pid=1, updated=t0)
    write(sdir, "mid", status="busy", label="中", pid=2, updated=t1)
    write(sdir, "new", status="needs_you", label="新", pid=3, updated=t2)
    write(sdir, "broken", raw="{ this is not json ")          # 损坏 → 跳过
    write(sdir, "halfempty", raw="")                          # 空 → 跳过
    write(sdir, "badstatus", status="weird", label="?", pid=9, updated=t2)  # 非法状态 → 跳过

    states = v.read_states(sdir)
    check("read: 只保留 3 个有效会话", len(states) == 3)
    check("read: 按 updated_at 降序 (new 在首)", states[0]["session_id"] == "new")
    check("read: 末位是最旧的 old", states[-1]["session_id"] == "old")
    check("read: 损坏/空/非法状态被跳过",
          all(s["session_id"] not in ("broken", "halfempty", "badstatus") for s in states))

    # 空 label → 未命名会话
    write(sdir, "noname", status="busy", label="", pid=4, updated=t2)
    states2 = v.read_states(sdir)
    noname = [s for s in states2 if s["session_id"] == "noname"][0]
    check("read: 空 label 回退为 未命名会话", noname["label"] == "未命名会话")
    os.remove(os.path.join(sdir, "noname.json"))

    # 同 updated_at 下次级排序 needs_you < busy < done
    sdir2 = os.path.join(d, "state2")
    os.makedirs(sdir2)
    write(sdir2, "a", status="done", pid=1, updated=t2)
    write(sdir2, "b", status="needs_you", pid=2, updated=t2)
    write(sdir2, "c", status="busy", pid=3, updated=t2)
    ss = v.read_states(sdir2)
    check("read: 同时间 needs_you 排最前",
          [s["status"] for s in ss] == ["needs_you", "busy", "done"])

    # ── parse_iso ──
    check("parse_iso: 带时区可解析", v.parse_iso(t2) is not None)
    check("parse_iso: 垃圾返回 None", v.parse_iso("nonsense") is None)
    check("parse_iso: 空返回 None", v.parse_iso("") is None)

    # ── config：默认 / clamp / 损坏修复 ──
    cfg = v.clamp_config({})
    check("config: 默认 opacity=85", cfg["opacity"] == 85)
    check("config: 默认尺寸 260x200", cfg["width"] == 260 and cfg["height"] == 200)

    clamped = v.clamp_config({"opacity": 5, "width": 9999, "height": 1})
    check("config: opacity 夹到下限 20", clamped["opacity"] == 20)
    check("config: width 夹到上限 600", clamped["width"] == 600)
    check("config: height 夹到下限 120", clamped["height"] == 120)
    check("config: 默认 font_level=0", cfg["font_level"] == 0)
    fl = v.clamp_config({"font_level": 99})
    fl2 = v.clamp_config({"font_level": -99})
    check("config: font_level 夹到 +5", fl["font_level"] == 5)
    check("config: font_level 夹到 -5", fl2["font_level"] == -5)
    check("config: 默认 theme=dark", cfg["theme"] == "dark")
    check("config: theme=light 保留", v.clamp_config({"theme": "light"})["theme"] == "light")
    check("config: 非法 theme → dark", v.clamp_config({"theme": "neon"})["theme"] == "dark")

    # ── 主题调色板：两套键一致，色点覆盖三态 ──
    keys = set(v.THEMES["dark"])
    check("theme: 两套调色板键一致", all(set(t) == keys for t in v.THEMES.values()))
    check("theme: 色点覆盖三态", all(set(t["dots"]) == set(v.DOT_COLORS)
                                   for t in v.THEMES.values()))

    # ── 额度行：格式 / 阈值 / 过期 / 缺失 ──
    nowt = 1_000_000.0
    U = {"five_hour": {"used_percentage": 31.4, "resets_at": nowt + 3600},
         "seven_day": {"used_percentage": 7, "resets_at": nowt + 86400}}
    segs = v.usage_segments(U, nowt)
    hhmm = v.datetime.fromtimestamp(nowt + 3600).strftime("%H:%M")
    check("usage: 文本为 31% · HH:MM · 7%",
          "".join(t for t, _ in segs) == "31%% · %s · 7%%" % hhmm)
    lv = lambda p: v.usage_segments(
        {"five_hour": {"used_percentage": p, "resets_at": nowt + 1}}, nowt)[0][1]
    check("usage: 59 normal / 60 warn / 85 crit",
          (lv(59), lv(60), lv(85)) == ("normal", "warn", "crit"))
    exp5 = dict(U, five_hour={"used_percentage": 90, "resets_at": nowt - 1})
    check("usage: 5h 过期 → -- · -- · 7%",
          "".join(t for t, _ in v.usage_segments(exp5, nowt)) == "-- · -- · 7%")
    check("usage: 两窗口都过期 → 隐藏",
          v.usage_segments({"five_hour": {"used_percentage": 1, "resets_at": nowt - 1}},
                           nowt) is None)
    check("usage: 无数据/损坏 → 隐藏", v.usage_segments(None, nowt) is None
          and v.usage_segments({"five_hour": {"x": 1}}, nowt) is None)
    upath = os.path.join(d, "usage.json")
    with open(upath, "w") as f:
        f.write("{ broken")
    check("usage: 损坏文件 read_usage → None", v.read_usage(upath) is None)
    check("usage: 文件不存在 → None", v.read_usage(upath + ".nope") is None)

    badcfg = os.path.join(d, "bad_config.json")
    with open(badcfg, "w") as f:
        f.write("{ broken")
    loaded = v.load_config(badcfg)
    check("config: 损坏 → 回退默认", loaded["opacity"] == 85)
    check("config: 损坏 → 重写出干净文件", os.path.exists(badcfg)
          and v.load_config(badcfg)["width"] == 260)

    # ── reaper_decide ──
    now = time.time()
    iso_now = v.datetime.now(v.timezone.utc).isoformat()
    iso_old = v.datetime.fromtimestamp(now - 4000, v.timezone.utc).isoformat()

    S = [
        {"session_id": "alive", "pid": 100, "status": "busy", "_ts": v.parse_iso(iso_now)},
        {"session_id": "dead", "pid": 200, "status": "busy", "_ts": v.parse_iso(iso_now)},
        {"session_id": "pid0_fresh", "pid": 0, "status": "done", "_ts": v.parse_iso(iso_now)},
        {"session_id": "pid0_stale", "pid": 0, "status": "done", "_ts": v.parse_iso(iso_old)},
    ]
    dead = v.reaper_decide(S, alive_pids={100}, now_ts=now)
    check("reap: pid 不在存活集合 → 删", "dead" in dead)
    check("reap: pid 在存活集合 → 留", "alive" not in dead)
    check("reap: pid=0 新鲜 → 留", "pid0_fresh" not in dead)
    check("reap: pid=0 超 30min → 删", "pid0_stale" in dead)

    # wsl 不可用(None)：busy 用更长阈值，4000s < 7200s 不删；done 用 1800s，4000s > 1800 删
    S2 = [
        {"session_id": "busy_old", "pid": 300, "status": "busy", "_ts": v.parse_iso(iso_old)},
        {"session_id": "done_old", "pid": 301, "status": "done", "_ts": v.parse_iso(iso_old)},
    ]
    dead2 = v.reaper_decide(S2, alive_pids=None, now_ts=now)
    check("reap fallback: busy 4000s 内不误删 (阈值 7200)", "busy_old" not in dead2)
    check("reap fallback: done 超 1800s 删", "done_old" in dead2)

    # ── truncate_to_width（用假 font，measure≈每字符 7px）──
    class FakeFont:
        def measure(self, s):
            return len(s) * 7
    f = FakeFont()
    short = v.truncate_to_width(f, "abc", 100)
    check("truncate: 不超宽原样返回", short == "abc")
    long = v.truncate_to_width(f, "x" * 50, 70)  # 容纳约 10 字符
    check("truncate: 超宽带省略号", long.endswith("…") and len(long) < 50)

    # ── format_tokens（context 占用显示）──
    check("fmt: 0 → 空串", v.format_tokens(0) == "")
    check("fmt: None → 空串", v.format_tokens(None) == "")
    check("fmt: 负数 → 空串", v.format_tokens(-5) == "")
    check("fmt: <1k 原样", v.format_tokens(999) == "999")
    check("fmt: 1k–10k 一位小数", v.format_tokens(4647) == "4.6k")
    check("fmt: ≥10k 取整", v.format_tokens(35100) == "35k")
    check("fmt: 大值取整", v.format_tokens(234922) == "235k")

    # ── 屏外判定 ──
    check("offscreen: None 视为越界", v.is_offscreen(None, None, 260, 200, 1920, 1080))
    check("offscreen: 正常坐标不越界", not v.is_offscreen(100, 100, 260, 200, 1920, 1080))
    check("offscreen: 远超右下越界", v.is_offscreen(5000, 5000, 260, 200, 1920, 1080))
    px, py = v.primary_geometry(260, 200, 1920, 1080)
    check("primary: 默认落点在主屏右上", px == 1920 - 260 - 16 and py == 48)

    # ── 贴边隐藏 ──
    area = (0, 0, 1920, 1040)      # 下方 40px 任务栏
    check("dock: 贴左", v.dock_edge_for(15, 300, 260, 200, area) == "left")
    check("dock: 越过左边也算", v.dock_edge_for(-80, 300, 260, 200, area) == "left")
    check("dock: 贴右", v.dock_edge_for(1920 - 260 - 10, 300, 260, 200, area) == "right")
    check("dock: 贴上", v.dock_edge_for(600, 5, 260, 200, area) == "top")
    check("dock: 中间不吸附", v.dock_edge_for(600, 300, 260, 200, area) is None)
    check("dock: 贴下边不吸附", v.dock_edge_for(600, 1040 - 200, 260, 200, area) is None)
    check("dock: 右展开贴齐", v.dock_geometry("right", 1600, 300, 260, 200, area, False) == (1660, 300))
    check("dock: 右收起只露窄边",
          v.dock_geometry("right", 1600, 300, 260, 200, area, True) == (1920 - v.DOCK_STRIP, 300))
    check("dock: 左收起只露窄边",
          v.dock_geometry("left", 5, 300, 260, 200, area, True) == (-(260 - v.DOCK_STRIP), 300))
    check("dock: 上收起只露窄边",
          v.dock_geometry("top", 600, 3, 260, 200, area, True) == (600, -(200 - v.DOCK_STRIP)))
    check("dock: 沿边坐标夹进工作区（不压任务栏）",
          v.dock_geometry("right", 1700, 950, 260, 200, area, False) == (1660, 840))
    check("dock: 上边沿边坐标夹进工作区",
          v.dock_geometry("top", 1800, 0, 260, 200, area, False) == (1660, 0))
    dots = v.strip_dots(["busy", "done", "needs_you"], 200)
    check("strip: 三个点按原顺序", [st for _, st in dots] == ["busy", "done", "needs_you"])
    check("strip: 居中排布", abs(dots[0][0] - (200 - (dots[-1][0] + v.DOCK_DOT))) <= 1)
    many = ["done"] * 40 + ["needs_you"]
    kept = v.strip_dots(many, 120)
    check("strip: 放不下时截断但保留红点",
          len(kept) < len(many) and "needs_you" in [st for _, st in kept]
          and kept[0][0] >= 0 and kept[-1][0] + v.DOCK_DOT <= 120)
    check("strip: 无会话无点", v.strip_dots([], 200) == [])
    check("config: dock_edge 非法值回退 None", v.clamp_config({"dock_edge": "bottom"})["dock_edge"] is None)
    check("config: dock_edge 合法值保留", v.clamp_config({"dock_edge": "right"})["dock_edge"] == "right")

    # ── 提示音边沿检测 / 开关 / 独立节流 ──
    B, N, D = v.STATUS_BUSY, v.STATUS_NEEDS_YOU, v.STATUS_DONE
    check("edge: busy→done 触发完成音", v.alert_edges({"a": B}, {"a": D}) == (False, True))
    check("edge: 新会话直接 done 不响", v.alert_edges({}, {"a": D}) == (False, False))
    check("edge: done→done 不响", v.alert_edges({"a": D}, {"a": D}) == (False, False))
    check("edge: needs_you→done 不响完成音", v.alert_edges({"a": N}, {"a": D}) == (False, False))
    check("edge: busy→needs_you 只响红点", v.alert_edges({"a": B}, {"a": N}) == (True, False))
    check("edge: 新会话直接 needs_you 响红点", v.alert_edges({}, {"a": N}) == (True, False))
    check("edge: 多会话同时红+完成两类都触发",
          v.alert_edges({"a": B, "b": B}, {"a": N, "b": D}) == (True, True))
    check("config: sound_done_enabled 默认开", v.clamp_config({})["sound_done_enabled"] is True)
    check("config: sound_done_enabled 可关", v.clamp_config({"sound_done_enabled": False})["sound_done_enabled"] is False)

    played = []
    fake_ws = types.SimpleNamespace(SND_FILENAME=1, SND_ASYNC=2,
                                    PlaySound=lambda path, fl: played.append(os.path.basename(path)))
    real_ws = v.winsound
    v.winsound = fake_ws
    try:
        app = types.SimpleNamespace(cfg={"sound_enabled": True, "sound_done_enabled": True},
                                    _last_alert_t=0.0, _last_done_t=0.0)
        play = lambda name: v.CCPulseViewer._play_wav(app, getattr(v, name), *(
            ("sound_enabled", "_last_alert_t") if name == "ALERT_SOUND" else ("sound_done_enabled", "_last_done_t")))
        play("ALERT_SOUND"); play("DONE_SOUND")
        check("sound: 红点与完成各自独立节流（同一瞬间两个都响）", played == ["notify.wav", "done.wav"])
        play("DONE_SOUND"); play("ALERT_SOUND")
        check("sound: 同类在节流窗口内被吞", played == ["notify.wav", "done.wav"])
        app.cfg["sound_done_enabled"] = False; app._last_done_t = 0.0
        play("DONE_SOUND")
        check("sound: 关闭完成音开关后不响", played == ["notify.wav", "done.wav"])
        app.cfg["sound_enabled"] = False; app._last_alert_t = 0.0
        app.cfg["sound_done_enabled"] = True
        play("ALERT_SOUND"); play("DONE_SOUND")
        check("sound: 两开关互不影响（红点关、完成开 → 只响完成）",
              played == ["notify.wav", "done.wav", "done.wav"])
        # 试听：开=响一次、关=不响；绕过节流且不改节流计时
        played.clear()
        pv = types.SimpleNamespace(cfg={"sound_enabled": True, "sound_done_enabled": True},
                                   _last_alert_t=0.0, _last_done_t=0.0, _save_config=lambda: None,
                                   var_sound=types.SimpleNamespace(get=lambda: True),
                                   var_sound_done=types.SimpleNamespace(get=lambda: True))
        pv._preview_wav = lambda path: v.CCPulseViewer._preview_wav(pv, path)
        v.CCPulseViewer._on_sound(pv); v.CCPulseViewer._on_sound(pv)
        v.CCPulseViewer._on_sound_done(pv)
        check("preview: 勾选即响、连点不被节流吞", played == ["notify.wav", "notify.wav", "done.wav"])
        check("preview: 不占用节流计时", pv._last_alert_t == 0.0 and pv._last_done_t == 0.0)
        pv.var_sound = types.SimpleNamespace(get=lambda: False)
        pv.var_sound_done = types.SimpleNamespace(get=lambda: False)
        v.CCPulseViewer._on_sound(pv); v.CCPulseViewer._on_sound_done(pv)
        check("preview: 关闭开关不响", len(played) == 3)
    finally:
        v.winsound = real_ws
    check("assets: notify.wav / done.wav 存在", os.path.isfile(v.ALERT_SOUND) and os.path.isfile(v.DONE_SOUND))

    # ── atomic_write_json ──
    apath = os.path.join(d, "atomic.json")
    v.atomic_write_json(apath, {"k": "值"})
    with open(apath, encoding="utf-8") as fp:
        check("atomic: 写入可回读且 UTF-8", json.load(fp)["k"] == "值")
    check("atomic: 不残留 .tmp", not any(x.endswith(".tmp") for x in os.listdir(d)))

    # ── 用真实 live state 目录跑一遍（若存在）──
    live = v.state_dir()
    if os.path.isdir(live):
        live_states = v.read_states(live)
        check("live: 真实 state 目录可解析（不抛）", isinstance(live_states, list))
        print("       live sessions:",
              [(s["label"], s["status"]) for s in live_states])

    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
