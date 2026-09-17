#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闭环驱动：发一轮指令 → 订阅事件流 → 轮询直到出结果 → 打印助手回复。

MiMo Desktop 的 `turns` 接口是**异步**的：POST 立刻返回 202，真正的输出要另外订阅
`/v1/sessions/{id}/events`（SSE）或轮询 `/v1/sessions/{id}/messages`。本脚本把这两步串起来。

用法：
  python run_turn.py <sessionId> "指令文本" [选项]

选项：
  --model X   模型（provider/model）。不传则自动从会话历史里探测；服务端强制必填。
  --dir D     工作目录。注意：实测**不改变目标的运行时 cwd**，只影响 harness 侧。
  --perm P    审批模式（见下）。不传=默认权限，工具调用会弹确认卡片并阻塞会话。
  --events N  发完指令后订阅 N 秒事件流（默认 0 = 不订阅）。
  --wait N    等待助手回复的最长秒数（默认 180）。

审批模式（服务端字面量，实测取自 app.asar 的 Ef 枚举）：
  "完全访问权限"  全自动批准 —— 所有工具调用直接放行，不再弹卡片
  "帮我审批"      仅文件编辑类（edit/write/apply_patch）自动批准，其它仍走审查
  不传 / ""      默认权限 —— 每次受保护操作都弹卡片

⚠️ 传入 "完全访问权限" 等于**从外部绕过用户的确认 UI**。仅在你明确知情并授权的场景用。
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mimo_api import Mimo, MimoError  # noqa: E402

PERM_FULL = "完全访问权限"
PERM_ACCEPT_EDITS = "帮我审批"


def detect_model(m, sid, d=None):
    """从会话历史里抠出最近一条 user 消息使用的模型，拼成 provider/model。

    服务端 sendTurn 的第一行是
        `if(!message.trim() || !sessionId || !model) return bad-request`
    model 是硬性必填 —— 所以必须给出确定值，不能靠猜。
    """
    try:
        msgs = m.messages(sid, d) or []
    except MimoError:
        return None
    for msg in reversed(msgs):
        mo = (msg.get("info") or {}).get("model") or {}
        pid, mid = mo.get("providerID"), mo.get("modelID")
        if pid and mid:
            return "%s/%s" % (pid, mid)
    return None


def extract_text(msg):
    """从一条 message 里抠出可读文本。"""
    parts = msg.get("parts") or []
    out = []
    for p in parts:
        t = p.get("type")
        if t == "text":
            out.append(p.get("text", ""))
        elif t == "tool":
            inp = (p.get("state") or {}).get("input", {})
            out.append("[tool] %s %s" % (p.get("tool"), json.dumps(inp, ensure_ascii=False)[:300]))
        elif t == "reasoning":
            out.append("[reasoning] %s" % (p.get("text", "")[:200]))
    return "\n".join(out) if out else json.dumps(msg, ensure_ascii=False)[:1500]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id")
    ap.add_argument("message", nargs="?", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--dir", default=None)
    ap.add_argument("--perm", default=None, choices=[PERM_FULL, PERM_ACCEPT_EDITS])
    ap.add_argument("--origin", default=None,
                    help="harness 并发闸门 key。默认用桌面共享槽位；传独立值可避开与 UI 线程争用导致的 409 busy")
    ap.add_argument("--wait", type=int, default=180)
    ap.add_argument("--events", type=int, default=0)
    a = ap.parse_args()

    m = Mimo()
    sid = a.session_id

    if a.message and not a.model:
        a.model = detect_model(m, sid, a.dir)
        if not a.model:
            print("!!! 无法探测会话模型，请显式传 --model", flush=True)
            return 1
        print(">>> 自动探测模型：%s" % a.model, flush=True)

    if a.message:
        print(">>> 发送指令：%s" % a.message, flush=True)
        print(">>> dir=%s  perm=%s  origin=%s" % (a.dir or "(会话默认)",
                                                  a.perm or "(默认权限·会弹卡片)",
                                                  a.origin or "(共享默认槽)"))
        if a.perm == PERM_FULL:
            print(">>> ⚠️ 已请求完全访问权限：目标将自动放行所有工具调用，不再寻求确认。", flush=True)
        t0 = time.time()
        try:
            r = m.turn(sid, a.message, model=a.model, directory=a.dir, perm=a.perm,
                       origin=a.origin)
            print("<<< 受理：%s  耗时 %.2fs" % (json.dumps(r, ensure_ascii=False), time.time() - t0), flush=True)
        except MimoError as e:
            if e.status == 409:
                print("!!! 该会话在 harness/引擎侧被判为未空闲（409 busy）。可能原因：\n"
                      "    ① MiMo 界面里有未处理的权限确认卡片（点掉即可）\n"
                      "    ② 该会话有 goal-pursuit / actor 未收束\n"
                      "    ③ UI 侧仍持有该会话的 run 槽位（该会话正开在界面上）\n"
                      "    换 origin 无效（实测）。**最省事的解法是换一个干净会话**。", flush=True)
            else:
                print("!!! 指令被拒：%s" % e, flush=True)
            return 1

    if a.events:
        print(">>> 订阅事件 %ds …" % a.events, flush=True)
        hist = Counter()

        def on_ev(ev):
            hist[ev.get("type", "?")] += 1
            print("  [ev] %s" % json.dumps(ev, ensure_ascii=False)[:300], flush=True)

        m.events(sid, a.dir, seconds=a.events, on_event=on_ev)
        print("<<< 事件类型统计：%s" % dict(hist), flush=True)

    print(">>> 轮询消息直至出结果（上限 %ds）…" % a.wait, flush=True)
    deadline = time.time() + a.wait
    last_n = len(m.messages(sid, a.dir) or [])
    print("    起始消息数：%d" % last_n, flush=True)
    while time.time() < deadline:
        time.sleep(4)
        cur = m.messages(sid, a.dir) or []
        if len(cur) > last_n:
            tail = cur[-1]
            role = (tail.get("info") or {}).get("role")
            if role == "assistant":
                print("\n===== 助手回复（消息数 %d→%d）=====" % (last_n, len(cur)), flush=True)
                print(extract_text(tail), flush=True)
                return 0
            print("    新增 %d 条（role=%s），继续等…" % (len(cur) - last_n, role), flush=True)
            last_n = len(cur)
    print("!!! 超时：%ds 内未拿到助手回复。常见原因：权限确认卡片待处理（看 MiMo 界面）。" % a.wait, flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
