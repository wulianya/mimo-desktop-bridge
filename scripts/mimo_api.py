#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiMo Desktop 本地 API 客户端
============================
Xiaomi MiMo 桌面版（Electron）在主进程里跑了一个仅监听 127.0.0.1 的 HTTP API，
启动时把 {api, port, token, pid} 写入用户数据目录的 desktop-api.json（权限 0600）。
本脚本就是这份 API 的客户端 —— 用来从外部读会话、发指令、拉文件。

鉴权与约束（均已在源码中确认，非猜测）：
  1. Header 必须是 `Authorization: Bearer <token>`，正则 /^Bearer[ \\t]+(\\S+)$/i
  2. 必须校验 Host 头 = 127.0.0.1:<port> | localhost:<port> | [::1]:<port>，否则 403 forbidden-host
  3. 路由前缀 /v1；sessionId 必须匹配 /^[A-Za-z0-9_.-]{1,64}$/
  4. 错误码：401 unauthorized / 403 forbidden-host|forbidden-file / 404 not-found
             / 409 busy(session busy) / 400 bad-request / 503 engine-not-ready|not-logged-in

完整路由表（服务器端分类器 uJ() 解出的全部路由，没有别的）：
  GET  /v1/health                       -> {"ok":true,"api":1,"app":..., "engine":"ready"}
  GET  /v1/sessions?limit=N             -> 会话列表（默认 200，上限 2000）
  GET  /v1/sessions/{id}/messages?dir=  -> 该会话消息
  GET  /v1/sessions/{id}/events?dir=    -> SSE 事件流（text/event-stream）
  POST /v1/sessions/{id}/turns          -> 发一轮指令，202 {"ok":true}
  GET  /v1/sessions/{id}/files?u=&dir=  -> 读会话内文件

⚠️ 已知硬限制：**没有"新建会话"路由**。turns 只能打到已存在的 sessionId，
   不存在的 id 返回 404 / 400，不会隐式创建。新会话必须先在 MiMo 界面里建立。

用法：
  python mimo_api.py selftest                 # 只读自检（健康 + 列会话 + 鉴权失败路径）
  python mimo_api.py health
  python mimo_api.py sessions --limit 10 [--md]
  python mimo_api.py messages <sessionId> [--dir D:\\path] [--head 2000]
  python mimo_api.py turn <sessionId> "指令文本" [--dir D:\\path] [--model X] [--perm Y]
  python mimo_api.py file <sessionId> "相对路径" [--dir D:\\path] [-o out.bin]
  python mimo_api.py events <sessionId> [--seconds 30]
  python mimo_api.py raw GET /v1/sessions          # 逃生舱：直接打任意路径
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 用户数据目录（Electron 的 app.getPath('userData')）
DEFAULT_CRED = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "Xiaomi MiMo",
    "desktop-api.json",
)


class MimoError(RuntimeError):
    """API 返回了结构化错误（{"code":..., "message":...}）。"""

    def __init__(self, status, code, message):
        super().__init__("HTTP %s %s: %s" % (status, code, message))
        self.status = status
        self.code = code
        self.message = message


def load_cred(path=None):
    """读 desktop-api.json。注意：这个文件是 MiMo 运行中才存在的，退出时会被删除。"""
    p = path or os.environ.get("MIMO_DESKTOP_API_JSON") or DEFAULT_CRED
    if not os.path.exists(p):
        raise SystemExit(
            "找不到 %s —— MiMo Desktop 没在运行。本 API 只在应用运行期间存在。" % p
        )
    with open(p, "r", encoding="utf-8") as f:
        cred = json.load(f)
    for k in ("port", "token"):
        if k not in cred:
            raise SystemExit("%s 缺字段 %s" % (p, k))
    return cred


class Mimo:
    def __init__(self, cred=None):
        cred = cred or load_cred()
        self.port = int(cred["port"])
        self.token = cred["token"]
        self.api = cred.get("api", 1)
        self.pid = cred.get("pid")
        self.base = "http://127.0.0.1:%d/v%d" % (self.port, self.api)

    # ---------- 底层 ----------

    def _req(self, method, path, body=None, timeout=30, stream=False):
        url = self.base + path
        data = None
        headers = {"Authorization": "Bearer " + self.token}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                obj = json.loads(raw)
                raise MimoError(e.code, obj.get("code", "?"), obj.get("message", raw))
            except json.JSONDecodeError:
                raise MimoError(e.code, "http-error", raw[:400])
        except urllib.error.URLError as e:
            raise SystemExit("连不上 %s：%s（MiMo 是否已退出？）" % (url, e.reason))
        if stream:
            return resp
        raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if raw else None

    # ---------- 六个路由 ----------

    def health(self):
        return self._req("GET", "/health", timeout=10)

    def sessions(self, limit=200):
        return self._req("GET", "/sessions?limit=%d" % limit, timeout=30)

    def messages(self, session_id, directory=None):
        q = "?dir=" + urllib.parse.quote(directory) if directory else ""
        return self._req("GET", "/sessions/%s/messages%s" % (session_id, q), timeout=60)

    def turn(self, session_id, message, model=None, directory=None, perm=None,
             origin=None, files=None):
        """发一轮指令。服务端立刻返回 202，真正的输出要另外用 events() 订阅。

        model 必填（服务端第一行就校验），形如 "xiaomi/mimo-x-pro-preview"。
        origin 是 harness 的并发闸门 key：同一 origin 有 run 在跑时新请求返回 409 busy。
        传一个独立 origin 可以绕开与桌面 UI 线程的争用。
        """
        body = {"message": message}
        if model:
            body["model"] = model
        if directory:
            body["dir"] = directory
        if perm:
            body["perm"] = perm
        if origin:
            body["origin"] = origin
        if files:
            body["files"] = files
        return self._req("POST", "/sessions/%s/turns" % session_id, body=body, timeout=30)

    def file(self, session_id, path, directory=None, out=None):
        q = "?u=" + urllib.parse.quote(path)
        if directory:
            q += "&dir=" + urllib.parse.quote(directory)
        resp = self._req("GET", "/sessions/%s/files%s" % (session_id, q),
                         timeout=60, stream=True)
        blob = resp.read()
        if out:
            with open(out, "wb") as f:
                f.write(blob)
            return {"saved": out, "bytes": len(blob)}
        return blob

    def events(self, session_id, directory=None, seconds=30, on_event=None):
        """订阅 SSE 事件流。返回收到的事件列表。"""
        q = "?dir=" + urllib.parse.quote(directory) if directory else ""
        resp = self._req("GET", "/sessions/%s/events%s" % (session_id, q),
                         timeout=seconds + 10, stream=True)
        out, deadline, buf = [], time.time() + seconds, ""
        try:
            while time.time() < deadline:
                chunk = resp.readline()
                if not chunk:
                    break
                line = chunk.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload:
                        try:
                            ev = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        out.append(ev)
                        if on_event:
                            on_event(ev)
        finally:
            resp.close()
        return out


# ---------- CLI ----------

def _print(obj, as_md=False):
    if isinstance(obj, bytes):
        print("<%d bytes>" % len(obj))
        return
    print(json.dumps(obj, ensure_ascii=False, indent=2)[:200000])


def cmd_selftest(m):
    """只读自检：验证链路、鉴权、以及 401/403 边界确实生效。"""
    ok = True
    try:
        h = m.health()
        print("[PASS] health      ->", json.dumps(h, ensure_ascii=False))
    except Exception as e:
        print("[FAIL] health      ->", e)
        return 1

    try:
        s = m.sessions(5)
        print("[PASS] sessions    -> %d 条" % len(s))
        for it in s[:5]:
            print("        %s  %s" % (it.get("id"), (it.get("title") or "")[:50]))
    except Exception as e:
        print("[FAIL] sessions    ->", e)
        ok = False

    # 边界 1：错误 token 必须 401
    bad = Mimo({"port": m.port, "token": "deadbeef"})
    try:
        bad.health()
        print("[FAIL] 错误 token 竟然通过了")
        ok = False
    except MimoError as e:
        print("[PASS] 错误 token  -> %s %s" % (e.status, e.code))

    # 边界 2：不存在的 session 必须 404
    try:
        m.messages("ses_probe0000000000000000")
        print("[FAIL] 不存在会话竟然读到数据")
        ok = False
    except MimoError as e:
        print("[PASS] 假 sessionId -> %s %s" % (e.status, e.code))

    print("\n结论:", "全部通过" if ok else "有失败项")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="MiMo Desktop 本地 API 客户端")
    ap.add_argument("--json", default=None, help="desktop-api.json 路径（默认自动找）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest")
    sub.add_parser("health")
    p = sub.add_parser("sessions"); p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("messages")
    p.add_argument("session_id"); p.add_argument("--dir", default=None)
    p.add_argument("--head", type=int, default=0, help="只打印前 N 个字符")
    p = sub.add_parser("turn")
    p.add_argument("session_id"); p.add_argument("message")
    p.add_argument("--dir", default=None); p.add_argument("--model", default=None)
    p.add_argument("--perm", default=None)
    p = sub.add_parser("file")
    p.add_argument("session_id"); p.add_argument("path")
    p.add_argument("--dir", default=None); p.add_argument("-o", default=None)
    p = sub.add_parser("events")
    p.add_argument("session_id"); p.add_argument("--dir", default=None)
    p.add_argument("--seconds", type=int, default=30)
    p = sub.add_parser("raw")
    p.add_argument("method"); p.add_argument("path")

    a = ap.parse_args()
    m = Mimo(load_cred(a.json))

    if a.cmd == "selftest":
        return cmd_selftest(m)
    if a.cmd == "health":
        _print(m.health()); return 0
    if a.cmd == "sessions":
        _print(m.sessions(a.limit)); return 0
    if a.cmd == "messages":
        data = m.messages(a.session_id, a.dir)
        if a.head:
            print(json.dumps(data, ensure_ascii=False)[: a.head])
        else:
            _print(data)
        return 0
    if a.cmd == "turn":
        _print(m.turn(a.session_id, a.message, model=a.model, directory=a.dir, perm=a.perm))
        print("已受理（202）。输出请用 events 子命令订阅。")
        return 0
    if a.cmd == "file":
        _print(m.file(a.session_id, a.path, a.dir, a.out)); return 0
    if a.cmd == "events":
        evs = m.events(a.session_id, a.dir, a.seconds)
        print("收到 %d 个事件" % len(evs))
        for e in evs[-20:]:
            print(json.dumps(e, ensure_ascii=False)[:300])
        return 0
    if a.cmd == "raw":
        _print(m._req(a.method, a.path)); return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
