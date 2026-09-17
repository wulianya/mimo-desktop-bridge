#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一个 MiMo 会话「看透」：内容 / 模型 / 思维链 / 工具调用 / 权限 / 交付文件 / 地址。

只读操作（除非你显式给 --turn，那时才会发一轮指令）。

用法：
  python inspect_session.py <sessionId>                          # 纯只读体检
  python inspect_session.py <sessionId> --turn "..." [--perm ...]  # 先发一轮再体检

它回答六个问题：
  1. 地址       会话在哪个目录、属于哪个 project
  2. 模型       每轮用的是哪个 provider/model
  3. 对话内容    user/assistant 的全部文本
  4. 思维链      消息里的 reasoning part（模型显式输出的推理）+ step-finish 的 token 统计
  5. 工具调用    每个 tool part 的工具名、入参、状态（含被权限拦下的）
  6. 交付文件    指令里出现过的文件路径 → 逐个到磁盘核验存在性与大小
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mimo_api import Mimo, MimoError, settle_wait  # noqa: E402

# 从工具入参/文本里捞绝对路径（Windows 盘符式）
PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"'`|<>*?]+")


def short(s, n=200):
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id")
    ap.add_argument("--turn", default=None, help="先发这一轮指令（默认只读）")
    ap.add_argument("--model", default=None)
    ap.add_argument("--perm", default=None)
    ap.add_argument("--events", type=int, default=0, help="发指令后订阅多少秒事件流")
    ap.add_argument("--wait", type=int, default=120)
    a = ap.parse_args()

    m = Mimo()
    sid = a.session_id

    # ---------- 可选：先发一轮 ----------
    if a.turn:
        model = a.model
        if not model:
            for msg in reversed(m.messages(sid) or []):
                mo = (msg.get("info") or {}).get("model") or {}
                if mo.get("providerID") and mo.get("modelID"):
                    model = "%s/%s" % (mo["providerID"], mo["modelID"])
                    break
        model = model or "xiaomi/mimo-x-pro-preview"
        print("=" * 78)
        print("发送指令 | model=%s perm=%s" % (model, a.perm or "(默认)"))
        print("=" * 78)
        try:
            print("受理:", m.turn(sid, a.turn, model=model, perm=a.perm))
        except MimoError as e:
            print("!!! 被拒: %s %s" % (e.status, e.code))
            return 1
        if a.events:
            print("\n--- 事件流 %ds（思维链/工具/权限都在这里）---" % a.events)
            seen = []

            def _on_ev(ev):
                seen.append(ev)
                print("  [%s] %s" % (ev.get("type"),
                                     short(json.dumps(ev, ensure_ascii=False), 240)))

            m.events(sid, seconds=a.events, on_event=_on_ev)
            print("  事件类型计数: %s" % {t: sum(1 for e in seen if e.get("type") == t)
                                          for t in {e.get("type") for e in seen}})
        # 等助手真正收尾（不是「末条是 assistant」就算完）
        import time
        ok, cur = settle_wait(m, sid, timeout=a.wait)
        print(">>> 收尾%s（消息数 %d）" % ("成功" if ok else "超时", len(cur)))

    # ---------- 1. 地址 / 会话元信息 ----------
    meta = None
    for s in m.sessions(50):
        if s["id"] == sid:
            meta = s
            break
    print("=" * 78)
    print("① 地址 / 会话元信息")
    print("=" * 78)
    if not meta:
        print("  ! 会话不在列表里")
    else:
        for k in ("id", "slug", "title", "projectID", "project", "directory", "version",
                  "titleSource", "summary", "time"):
            if k in meta:
                print("  %-12s %s" % (k, json.dumps(meta[k], ensure_ascii=False)[:160]))

    # ---------- 逐条消息 ----------
    msgs = m.messages(sid) or []
    print("\n消息总数：%d" % len(msgs))

    print("\n" + "=" * 78)
    print("② 模型")
    print("=" * 78)
    models = []
    for msg in msgs:
        mo = (msg.get("info") or {}).get("model") or {}
        if mo.get("providerID"):
            tag = "%s/%s" % (mo.get("providerID"), mo.get("modelID"))
            if tag not in models:
                models.append(tag)
    print("  本轮会话出现过的模型：", models or "(无)")

    # ---------- 3/4/5 内容、思维链、工具 ----------
    print("\n" + "=" * 78)
    print("③④⑤ 对话内容 / 思维链 / 工具调用")
    print("=" * 78)
    all_paths = []
    for i, msg in enumerate(msgs):
        info = msg.get("info") or {}
        role = info.get("role")
        t = info.get("time") or {}
        mo = info.get("model") or {}
        print("\n┌─ #%d %s  created=%s completed=%s  model=%s" % (
            i, role, t.get("created"), t.get("completed"),
            mo.get("modelID") or "-"))
        for p in (msg.get("parts") or []):
            ty = p.get("type")
            st = p.get("state") or {}
            if ty == "reasoning":
                print("│  [思维链] %s" % short(p.get("text"), 260))
            elif ty == "text":
                print("│  [内容]   %s" % short(p.get("text"), 260))
            elif ty == "tool":
                inp = st.get("input") or {}
                print("│  [工具]   %s  status=%s" % (p.get("tool"), st.get("status")))
                print("│           input=%s" % short(json.dumps(inp, ensure_ascii=False), 240))
                out = st.get("output")
                if out:
                    print("│           output=%s" % short(json.dumps(out, ensure_ascii=False), 200))
            elif ty == "step-finish":
                tok = p.get("tokens") or {}
                print("│  [收尾]   reason=%s tokens=%s cost=%s" % (
                    p.get("reason"), json.dumps(tok, ensure_ascii=False), p.get("cost")))
            else:
                print("│  [%s] %s" % (ty, short(json.dumps(p, ensure_ascii=False), 160)))
            # 顺手收集路径
            for cand in PATH_RE.findall(json.dumps(p, ensure_ascii=False)):
                all_paths.append(cand.rstrip('\\/"'))
        print("└─")

    # ---------- 6. 交付文件 ----------
    print("\n" + "=" * 78)
    print("⑥ 交付文件（从消息里抽路径 → 到磁盘核验）")
    print("=" * 78)
    uniq = []
    for p in all_paths:
        if p not in uniq and len(p) > 6:
            uniq.append(p)
    if not uniq:
        print("  (消息里没出现绝对路径)")
    for p in uniq[-15:]:
        try:
            stt = os.stat(p)
            print("  ✓ %-88s %d bytes  %s" % (p, stt.st_size,
                                              __import__("time").strftime("%H:%M:%S", __import__("time").localtime(stt.st_mtime))))
        except OSError:
            print("  · %-88s (不存在/不可访问 —— 多半只是被提到)" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
