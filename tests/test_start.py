#!/usr/bin/env python3
"""start.py 建立 / 加入房間測試 (在本機開一個伺服器模擬線上伺服器，不需要螢幕)。

    python3 tests/test_start.py
"""
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 專案根目錄
sys.path.insert(0, str(ROOT))

import start  # noqa: E402
import transport  # noqa: E402


PORT = 9251
SERVER = f"ws://127.0.0.1:{PORT}"


def run(*args, env=None):
    return subprocess.run([sys.executable, "start.py", *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=30, env=dict(os.environ, **(env or {})))


class StartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", str(PORT)], cwd=ROOT, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.p.terminate()
        cls.p.wait()

    def test_normalize_code(self):
        self.assertEqual(start.normalize_code(" k7q-2m "), "K7Q2M")
        self.assertEqual(start.normalize_code("ooill"), "00111")       # O->0, I/L->1
        for bad, why in [("", "請輸入"), ("K7Q2", "5 碼"), ("K7Q2MM", "5 碼"), ("K7?2M", "無效")]:
            with self.assertRaises(ValueError) as cm:
                start.normalize_code(bad)
            self.assertIn(why, str(cm.exception))

    def test_create_and_join(self):
        code = start.create_room(SERVER)
        self.assertEqual(len(code), 5)
        self.assertEqual(start.join_room(SERVER, code.lower()), code)
        with self.assertRaises(RuntimeError) as cm:
            start.join_room(SERVER, "ZZZZZ" if code != "ZZZZZ" else "00000")
        self.assertIn("找不到房間", str(cm.exception))

    def test_errors(self):
        for fn in (lambda: start.create_room(None), lambda: start.join_room(None, "K7Q2M")):
            with self.assertRaises(RuntimeError) as cm:
                fn()
            self.assertIn("git pull", str(cm.exception))             # 沒設定網址
        with self.assertRaises(RuntimeError) as cm:
            start.create_room("ws://127.0.0.1:9259")                 # 伺服器沒開
        self.assertIn("連不上線上伺服器", str(cm.exception))
        with self.assertRaises(RuntimeError) as cm:
            start.create_room("wss://no-such-host.invalid")
        self.assertIn("找不到伺服器網址", str(cm.exception))

    def test_cli(self):
        out = run("--server", SERVER, "join", "K7?2M", "--check")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("無效的字元", out.stderr)
        code = start.create_room(SERVER)
        out = run("join", code, "--check", env={"WHITEBOARD_SERVER": SERVER})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(f"房間 {code} 存在", out.stdout)
        out = run("join", code, "--check", "--server", SERVER)       # --server 放在後面也可以
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_configured_server(self):
        self.assertEqual(transport.configured_server(" wss://a.b "), "wss://a.b")
        os.environ["WHITEBOARD_SERVER"] = "wss://env.example"
        try:
            self.assertEqual(transport.configured_server(), "wss://env.example")
        finally:
            del os.environ["WHITEBOARD_SERVER"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
