#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mimo_api.py 的离线单元测试。

不需要 MiMo 在跑 —— 本地起一个打桩 HTTP 服务，按 references/api-spec.md 复刻准入规则
（Bearer 校验 / Host 校验 / 6 条路由 / 错误码），然后让客户端去打它。

跑法：
    python -m pytest tests/ -v          # 有 pytest
    python tests/test_mimo_api.py       # 无 pytest，直接跑（内置极简 runner）
"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from mimo_api import Mimo, MimoError, is_settled  # noqa: E402

TOKEN = "test-token-0123456789"
SESSION_OK = "ses_test0000000000000000000000"
KNOWN_SESSIONS = [{
    "id": SESSION_OK,
    "title": "打桩会话",
    "directory": r"C:\stub\dir",
    "projectID": "global",
}]
MESSAGES = [{
    "info": {"role": "user", "model": {"providerID": "xiaomi", "modelID": "mimo-x-pro-preview"}},
    "parts": [{"type": "text", "text": "hello"}],
}]

captured = {}  # 记录服务端收到的最后一次 turns 请求体，供断言


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 静音
        pass

    # ---- 工具 ----
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, name):
        return self._json(code, {"code": name, "message": name})

    def _gate(self):
        """复刻两道准入闸。返回 True 表示应继续处理。"""
        host = self.headers.get("Host", "")
        if not (host.startswith("127.0.0.1:") or host.startswith("localhost:")):
            self._err(403, "forbidden-host")
            return False
        auth = self.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
        else:
            got = ""
        if got != TOKEN:
            self._err(401, "unauthorized")
            return False
        return True

    # ---- 路由 ----
    def do_GET(self):
        if not self._gate():
            return
        u = urlparse(self.path)
        seg = [s for s in u.path.split("/") if s]
        if len(seg) < 2 or seg[0] != "v1":
            return self._err(404, "not-found")

        if seg[1] == "health" and len(seg) == 2:
            return self._json(200, {"ok": True, "api": 1, "app": "stub", "engine": "ready"})

        if seg[1] != "sessions":
            return self._err(404, "not-found")

        if len(seg) == 2:
            limit = parse_qs(u.query).get("limit", ["200"])[0]
            return self._json(200, KNOWN_SESSIONS[: int(limit)])

        if len(seg) != 4:
            return self._err(404, "not-found")
        sid, action = seg[2], seg[3]
        if sid != SESSION_OK:
            return self._err(404, "not-found")

        if action == "messages":
            return self._json(200, MESSAGES)
        if action == "files":
            blob = b"stub-file-content"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return
        if action == "events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "close")   # SSE 无 Content-Length，靠关连接表示结束
            self.close_connection = True
            self.end_headers()
            frames = [
                {"api": 1, "sessionId": sid},
                {"type": "busy"},
                {"type": "ui", "ui": {"kind": "text", "text": "hi"}},
                {"type": "idle"},
            ]
            for f in frames:
                try:
                    self.wfile.write(("data: %s\n\n" % json.dumps(f, ensure_ascii=False)).encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
            return
        return self._err(404, "not-found")

    def do_POST(self):
        if not self._gate():
            return
        u = urlparse(self.path)
        seg = [s for s in u.path.split("/") if s]
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return self._err(400, "bad-request")

        if len(seg) == 4 and seg[0] == "v1" and seg[1] == "sessions" and seg[3] == "turns":
            sid = seg[2]
            msg = body.get("message") if isinstance(body.get("message"), str) else ""
            model = body.get("model") if isinstance(body.get("model"), str) else ""
            if not msg.strip() or not sid or not model:
                return self._err(400, "bad-request")   # ← 与真实服务端同一行校验
            if sid != SESSION_OK:
                return self._err(404, "not-found")
            captured.clear()
            captured.update(body)
            return self._json(202, {"ok": True})
        return self._err(404, "not-found")


class TestIsSettled(unittest.TestCase):
    """is_settled 的回归护栏。

    这个函数是被真实 bug 逼出来的：assistant 消息在流式输出期间就已经出现在 /messages 里，
    `info.time.completed` 还是 None，只看 role 会在半途以为结束。
    """

    def test_user_message_is_always_settled(self):
        self.assertTrue(is_settled({"info": {"role": "user"}, "parts": []}))

    def test_assistant_completed(self):
        self.assertTrue(is_settled({
            "info": {"role": "assistant", "time": {"created": 1, "completed": 2}},
            "parts": [{"type": "text", "text": "done"}],
        }))

    def test_assistant_still_streaming(self):
        # completed 缺失 = 还在跑
        self.assertFalse(is_settled({
            "info": {"role": "assistant", "time": {"created": 1}},
            "parts": [{"type": "text", "text": "部分"}],
        }))

    def test_assistant_with_running_tool(self):
        self.assertFalse(is_settled({
            "info": {"role": "assistant", "time": {"created": 1, "completed": 2}},
            "parts": [{"type": "tool", "tool": "bash", "state": {"status": "running"}}],
        }))

    def test_assistant_with_completed_tool(self):
        self.assertTrue(is_settled({
            "info": {"role": "assistant", "time": {"created": 1, "completed": 2}},
            "parts": [{"type": "tool", "tool": "bash", "state": {"status": "completed"}}],
        }))


class TestMimoApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        cls.port = cls.httpd.server_address[1]
        cls.t = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def cred(self, token=TOKEN):
        # pid 字段客户端不校验，随便给
        return {"api": 1, "port": self.port, "token": token, "pid": 0}

    def client(self, token=TOKEN):
        return Mimo(self.cred(token))

    # ---------- 正常路径 ----------

    def test_health(self):
        h = self.client().health()
        self.assertTrue(h["ok"])
        self.assertEqual(h["api"], 1)
        self.assertEqual(h["engine"], "ready")

    def test_sessions_returns_list(self):
        s = self.client().sessions(5)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["id"], SESSION_OK)

    def test_messages(self):
        msgs = self.client().messages(SESSION_OK)
        self.assertEqual(msgs[0]["parts"][0]["text"], "hello")

    def test_turn_accepts_and_forwards_body(self):
        r = self.client().turn(SESSION_OK, "do it", model="xiaomi/mimo-x-pro-preview")
        self.assertEqual(r, {"ok": True})
        self.assertEqual(captured["message"], "do it")
        self.assertEqual(captured["model"], "xiaomi/mimo-x-pro-preview")

    def test_turn_forwards_perm_and_dir(self):
        self.client().turn(SESSION_OK, "x", model="xiaomi/m", directory=r"C:\d", perm="完全访问权限")
        self.assertEqual(captured["perm"], "完全访问权限")
        self.assertEqual(captured["dir"], r"C:\d")

    def test_events_parses_sse(self):
        evs = self.client().events(SESSION_OK, seconds=3)
        types = [e.get("type") for e in evs]
        self.assertIn("busy", types)
        self.assertIn("idle", types)

    def test_file_reads_bytes(self):
        blob = self.client().file(SESSION_OK, "a.txt")
        self.assertEqual(blob, b"stub-file-content")

    # ---------- 错误路径（这几条是真实服务端的准入规则） ----------

    def test_wrong_token_401(self):
        with self.assertRaises(MimoError) as cm:
            self.client("deadbeef").health()
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(cm.exception.code, "unauthorized")

    def test_missing_bearer_prefix_401(self):
        """服务端用 /^Bearer[ \\t]+(\\S+)$/i 提取 token，裸 token 一律取不出来 → 401。"""
        req = urllib.request.Request("http://127.0.0.1:%d/v1/health" % self.port,
                                     headers={"Authorization": TOKEN})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("裸 token 竟然通过了")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_unknown_session_404(self):
        with self.assertRaises(MimoError) as cm:
            self.client().messages("ses_probe0000000000000000")
        self.assertEqual(cm.exception.status, 404)

    def test_turn_without_model_400(self):
        with self.assertRaises(MimoError) as cm:
            self.client().turn(SESSION_OK, "hi")
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(cm.exception.code, "bad-request")

    def test_turn_without_message_400(self):
        with self.assertRaises(MimoError) as cm:
            self.client().turn(SESSION_OK, "   ", model="xiaomi/m")
        self.assertEqual(cm.exception.status, 400)

    def test_host_check_absent_path_not_reachable(self):
        """验证服务端的 Host 白名单确实生效 —— 手工塞一个假 Host。"""
        req = urllib.request.Request("http://127.0.0.1:%d/v1/health" % self.port,
                                     headers={"Authorization": "Bearer " + TOKEN,
                                              "Host": "evil.com"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("伪造 Host 竟然通过了")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
