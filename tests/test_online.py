#!/usr/bin/env python3
"""傳輸層與線上功能測試：WebSocket、HTTP 狀態頁、房間號、復原訊息。

    python3 tests/test_online.py
"""
import os
import socket
import struct
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 專案根目錄
sys.path.insert(0, str(ROOT))

import transport  # noqa: E402
from transport import OP_CONT, OP_PING, OP_TEXT, open_connection, ws_frame  # noqa: E402


PORT = 9241


def start_server(port, env_port=False):
    args = [sys.executable, str(ROOT / "server.py")] + ([] if env_port else ["--port", str(port)])
    env = dict(os.environ, PORT=str(port)) if env_port else None
    p = subprocess.Popen(args, cwd=ROOT, stderr=subprocess.DEVNULL, env=env)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server not up")


def http(port, req):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        s.sendall(req.encode())
        data = b""
        while chunk := s.recv(4096):
            data += chunk
    return data.decode()


class WS:
    def __init__(self, name=None, room=None, port=PORT):
        self.c = open_connection(f"ws://127.0.0.1:{port}/", timeout=3)
        self.c.settimeout(3)
        if name:
            self.send(type="hello", name=name, room=room)
            self.welcome = self.until("welcome")

    def send(self, **o):
        self.c.send(**o)

    def until(self, t):
        for _ in range(200):
            m = self.c.recv()
            if m["type"] == t:
                return m
        raise AssertionError(t)

    def close(self):
        self.c.close()


class OnlineServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = start_server(PORT)

    @classmethod
    def tearDownClass(cls):
        cls.p.terminate()
        cls.p.wait()

    def test_http_status_pages(self):
        r = http(PORT, "GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertTrue(r.startswith("HTTP/1.1 200"), r)
        self.assertIn("collab-whiteboard server OK", r)
        self.assertTrue(http(PORT, "GET /healthz HTTP/1.1\r\n\r\n").startswith("HTTP/1.1 200"))
        self.assertTrue(http(PORT, "GET /nope HTTP/1.1\r\n\r\n").startswith("HTTP/1.1 404"))
        head = http(PORT, "HEAD / HTTP/1.1\r\n\r\n")
        self.assertTrue(head.startswith("HTTP/1.1 200") and head.endswith("\r\n\r\n"))

    def test_rooms(self):
        codes = {transport.request(f"ws://127.0.0.1:{PORT}", type="create_room")["code"]
                 for _ in range(20)}
        self.assertEqual(len(codes), 20)                       # 不重複
        for c in codes:
            self.assertEqual(len(c), 5)
            self.assertTrue(set(c) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ"))
        code = sorted(codes)[0]
        info = transport.request(f"ws://127.0.0.1:{PORT}", type="room_info", room=code)
        self.assertEqual((info["exists"], info["users"]), (True, 0))
        info = transport.request(f"ws://127.0.0.1:{PORT}", type="room_info", room="NOPE1")
        self.assertFalse(info["exists"])
        a = WS("amy", code)
        self.assertEqual(transport.request(f"ws://127.0.0.1:{PORT}", type="room_info",
                                           room=code)["users"], 1)
        a.close()
        time.sleep(0.2)                                         # 房間空了也保留
        self.assertTrue(transport.request(f"ws://127.0.0.1:{PORT}", type="room_info",
                                          room=code)["exists"])

    def test_non_http_rejected(self):
        """不是 HTTP / WebSocket 的連線會直接被關閉，伺服器不受影響。"""
        with socket.create_connection(("127.0.0.1", PORT), timeout=3) as raw:
            raw.sendall(b'{"type":"hello","name":"x","room":"y"}\n')
            self.assertEqual(raw.recv(100), b"")
        self.assertTrue(transport.request(f"ws://127.0.0.1:{PORT}", type="room_info", room="y"))

    def test_frames_fragmented_ping_large(self):
        w = WS("frag", "frames")
        sock = w.c.sock
        # 分成三段的訊息 (FIN=0 ... continuation ... FIN=1)，中間夾一個 ping
        payload = b'{"type":"text","sid":"f1","x":0.1,"y":0.1,"text":"' + "分段".encode() + b'"}'
        a, b, c = payload[:10], payload[10:30], payload[30:]

        def frame(data, op, fin):
            f = bytearray(ws_frame(data, op, mask=True))
            if not fin:
                f[0] &= 0x7F
            return bytes(f)
        sock.sendall(frame(a, OP_TEXT, False) + frame(b"hi", OP_PING, True)
                     + frame(b, OP_CONT, False) + frame(c, OP_CONT, True))
        # 伺服器回 pong (Conn.recv 會略過 pong)，並收下完整訊息
        check = WS("check", "frames")
        self.assertEqual([o["text"] for o in check.welcome["strokes"]], ["分段"])
        # 大訊息 (>64KB，使用 8 byte 長度欄位)：復原 2000 筆筆跡
        big = [{"sid": f"b{i}", "owner": "frag", "color": "#000000", "width": 2,
                "pts": [[0.1, 0.1], [0.2, 0.3]] * 10} for i in range(2000)]
        r = WS("r", "bigroom")
        r.send(type="restore", strokes=big)
        self.assertEqual(len(r.until("reset")["strokes"]), 2000)
        for x in (w, check, r):
            x.close()

    def test_bad_ws_input(self):
        w = WS()
        w.c.sock.sendall(ws_frame(b"not json", OP_TEXT, mask=True)
                         + ws_frame(b"[1,2,3]", OP_TEXT, mask=True))
        w.send(type="room_info", room="x")
        self.assertEqual(w.c.recv()["type"], "room_info")     # 壞訊息被忽略，連線還在
        w.c.sock.sendall(struct.pack("!BB", 0x81, 0xFF) + struct.pack("!Q", 1 << 40))  # 超大
        w.c.sock.settimeout(3)
        self.assertEqual(w.c.sock.recv(10), b"")               # 伺服器直接斷線
        w.close()
        self.assertTrue(transport.request(f"ws://127.0.0.1:{PORT}", type="room_info",
                                          room="x"))          # 伺服器沒事

    def test_restore_only_once(self):
        a, b = WS("amy", "rest"), WS("ben", "rest")
        objs = [{"sid": "x1", "owner": "amy", "color": "#123456", "width": 4,
                 "pts": [[0.1, 0.1], [0.5, 0.5]]},
                {"sid": "x2", "owner": "ben", "kind": "text", "x": 0.3, "y": 0.3,
                 "text": "hi", "color": "#000000", "size": 30},
                {"sid": "bad", "color": "#000000", "pts": "nope"}]
        a.send(type="restore", strokes=objs)
        for w in (a, b):
            m = w.until("reset")
            self.assertEqual([(o["sid"], o["owner"]) for o in m["strokes"]],
                             [("x1", "amy"), ("x2", "ben")])   # 保留原本的 id 與作者
        b.send(type="restore", strokes=[dict(objs[0], sid="dup")])   # 已有內容 -> 忽略
        b.send(type="undo")                                    # ben 可以復原自己的文字
        self.assertEqual(a.until("remove")["sid"], "x2")
        self.assertEqual([o["sid"] for o in WS("v", "rest").welcome["strokes"]], ["x1"])
        a.close(); b.close()


class TransportTest(unittest.TestCase):
    def test_parse_server(self):
        P = transport.parse_server
        self.assertEqual(P("wss://wb.onrender.com"), ("wss", "wb.onrender.com", 443, "/"))
        self.assertEqual(P("ws://1.2.3.4:8080/ws?x=1"), ("ws", "1.2.3.4", 8080, "/ws?x=1"))
        self.assertEqual(P(" https://wb.onrender.com/ "), ("wss", "wb.onrender.com", 443, "/"))
        for bad in ("ftp://x", "wb.onrender.com", "ws://"):
            with self.assertRaises(ValueError):
                P(bad)

    def test_mask_roundtrip(self):
        key = b"\x01\x02\x03\x04"
        for n in (0, 1, 5, 4096, 100001):
            data = os.urandom(n)
            masked = transport.mask_bytes(data, key)
            self.assertEqual(masked, bytes(b ^ key[i % 4] for i, b in enumerate(data)))
            self.assertEqual(transport.mask_bytes(masked, key), data)

    def test_env_port_and_wrong_url(self):
        p = start_server(9243, env_port=True)                  # 雲端平台用環境變數 PORT
        try:
            self.assertTrue(transport.request("ws://127.0.0.1:9243", type="create_room")["code"])
        finally:
            p.terminate(); p.wait()
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen()   # 不是白板的網站
        port = srv.getsockname()[1]
        import threading

        def fake():
            c, _ = srv.accept()
            c.recv(1000)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
            c.close()
        threading.Thread(target=fake, daemon=True).start()
        with self.assertRaises(OSError) as cm:
            open_connection(f"ws://127.0.0.1:{port}", timeout=3)
        self.assertIn("網址可能不對", str(cm.exception))
        srv.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
