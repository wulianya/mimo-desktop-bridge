#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""等待/捕捉 MiMo 里新出现的会话。

为什么需要这个：`desktop-api` **没有新建会话的路由**，新会话只能在 MiMo 界面里点出来。
所以自动化流程的起手式是「让用户点一下新建任务 → 本脚本把 sessionId 抓回来 → 后续全程 API 驱动」。

用法：
  python watch_new_session.py [--dir D:\\some\\dir] [--timeout 600] [--interval 5]

判定命中（任一）：
  A. 某会话的 directory 等于 --dir 给的目录
  B. 出现了启动快照里没有的新 sessionId（不传 --dir 时用这条）
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mimo_api import Mimo  # noqa: E402


def norm(p):
    return (p or "").rstrip("\\/").lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=None, help="目标工作目录；给了就优先按目录匹配")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--interval", type=int, default=5)
    a = ap.parse_args()

    m = Mimo()
    baseline = {s["id"] for s in m.sessions(50)}
    print("[watch] 基线 %d 个会话，等待新会话…" % len(baseline), flush=True)

    deadline = time.time() + a.timeout
    while time.time() < deadline:
        time.sleep(a.interval)
        try:
            cur = m.sessions(50)
        except Exception as e:
            print("[watch] 轮询失败：%s" % e, flush=True)
            continue
        for s in cur:
            hit_dir = bool(a.dir) and norm(s.get("directory")) == norm(a.dir)
            is_new = s["id"] not in baseline
            if hit_dir or is_new:
                print("[watch] 命中%s！" % ("（目录匹配）" if hit_dir else "（新会话）"), flush=True)
                print(json.dumps({
                    "id": s["id"],
                    "title": s.get("title"),
                    "directory": s.get("directory"),
                    "projectID": s.get("projectID"),
                    "matched_by": "directory" if hit_dir else "new-session",
                }, ensure_ascii=False, indent=2), flush=True)
                return 0
    print("[watch] 超时 %ds 未发现新会话" % a.timeout, flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
