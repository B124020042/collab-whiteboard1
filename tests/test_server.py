#!/usr/bin/env python3
"""伺服器測試：啟動伺服器，用多個 WebSocket 客戶端驗證同步邏輯、權限與防呆。

    python3 tests/test_server.py
"""
import json
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 專案根目錄
sys.path.insert(0, str(ROOT))

from protocol import chunk_pts, clamp_move, dump_board, move_obj, objects_bbox, parse_board  # noqa: E402
from transport import OP_TEXT, ConnectionClosed, open_connection, ws_frame  # noqa: E402


PORT = 9177


class C:
    """測試用客戶端：可以等待特定訊息，或確認一段時間內沒收到某種訊息。"""

    def __init__(self, name, room="lobby"):
        self.conn = open_connection(f"ws://127.0.0.1:{PORT}", timeout=3)
        self.send(type="hello", name=name, room=room)
        self.welcome = self.until("welcome")
        self.name = self.welcome["name"]

    def send(self, **o):
        self.conn.send(**o)

    def send_raw(self, payload: bytes):
        self.conn.sock.sendall(ws_frame(payload, OP_TEXT, mask=True))

    def recv(self, timeout=3):
        self.conn.settimeout(timeout)
        try:
            return self.conn.recv()
        except socket.timeout:
            raise TimeoutError("timed out")
        except ConnectionClosed:
            raise ConnectionError("closed")

    def until(self, t, pred=lambda m: True):
        for _ in range(500):
            m = self.recv()
            if m["type"] == t and pred(m):
                return m
        raise AssertionError(f"沒收到 {t}")

    def nothing(self, t, wait=0.3):
        """確認 wait 秒內沒收到 type 為 t 的訊息。"""
        end = time.monotonic() + wait
        try:
            while (left := end - time.monotonic()) > 0:
                if self.recv(left)["type"] == t:
                    return False
        except TimeoutError:
            pass
        return True

    def close(self):
        self.conn.close()


class WhiteboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", str(PORT)],
                                 cwd=ROOT, stderr=subprocess.DEVNULL)
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

    def test_draw_sync_and_replay(self):
        a, b = C("alice", "r1"), C("bob", "r1")
        a.until("user_join", lambda m: m["name"] == "bob")
        a.send(type="draw", sid="s1", color="#ff0000", width=5, pts=[[0.1, 0.1], [0.2, 0.2]])
        a.send(type="draw", sid="s1", color="#ff0000", width=5, pts=[[0.2, 0.2], [0.3, 0.3]])
        m1, m2 = b.until("draw"), b.until("draw")
        self.assertEqual((m1["owner"], m1["color"], m1["width"]), ("alice", "#ff0000", 5))
        self.assertEqual(m2["pts"][-1], [0.3, 0.3])
        self.assertTrue(a.nothing("draw"))           # 不會回送給自己

        late = C("carol", "r1")                       # 後加入者拿到完整重播
        strokes = late.welcome["strokes"]
        self.assertEqual(len(strokes), 1)
        self.assertEqual(len(strokes[0]["pts"]), 4)
        self.assertEqual(sorted(late.welcome["users"]), ["alice", "bob", "carol"])
        for c in (a, b, late):
            c.close()

    def test_undo_only_own(self):
        a, b = C("alice", "r2"), C("bob", "r2")
        a.send(type="draw", sid="a1", pts=[[0.5, 0.5]])
        b.until("draw")
        b.send(type="draw", sid="b1", pts=[[0.6, 0.6]])
        a.until("draw")
        b.send(type="draw", sid="a1", pts=[[0.9, 0.9]])   # 不能接續別人的筆跡
        b.send(type="cursor", x=0, y=0)
        a.until("cursor")                                  # 確定 b 的訊息已被伺服器處理
        self.assertTrue(a.nothing("draw", 0.1))
        a.send(type="undo")
        self.assertEqual(a.until("remove")["sid"], "a1")
        self.assertEqual(b.until("remove")["sid"], "a1")
        a.send(type="undo")                                # a 已沒有筆跡 -> 不動作
        self.assertTrue(b.nothing("remove"))
        strokes = C("x", "r2").welcome["strokes"]
        self.assertEqual([s["sid"] for s in strokes], ["b1"])
        a.close(); b.close()

    def test_clear_rooms_cursor_names(self):
        a, b, other = C("alice", "r3"), C("alice", "r3"), C("dave", "r4")
        self.assertEqual(b.name, "alice_2")                # 重複暱稱自動加編號
        a.send(type="draw", sid="z", pts=[[0.1, 0.1]])
        b.until("draw")
        self.assertTrue(other.nothing("draw"))             # 房間隔離
        a.send(type="cursor", x=0.4, y=1.7)
        m = b.until("cursor")
        self.assertEqual((m["name"], m["x"], m["y"]), ("alice", 0.4, 1.0))  # 座標被限制在 0~1
        b.send(type="clear")
        self.assertEqual(a.until("clear")["by"], "alice_2")
        self.assertEqual(C("e", "r3").welcome["strokes"], [])
        b.close()
        self.assertEqual(a.until("user_leave", lambda m: m["name"] == "alice_2")["name"], "alice_2")
        a.close(); other.close()

    def test_bad_input_does_not_crash(self):
        watcher = C("watcher", "r5")
        c = C("evil", "r5")
        for raw in (b"not json", b'{"type":"draw"}', b"[1,2]"):
            c.send_raw(raw)
        c.send(type="draw", sid="q", color="red;rm", width=9999, pts=[["a", 1]])
        c.send(type="draw", sid="ok", color="javascript", width=9999, pts=[[2, -1]])
        watcher.until("draw")
        s = C("check", "r5").welcome["strokes"]
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0]["color"], s[0]["width"], s[0]["pts"]), ("#000000", 60, [[1.0, 0.0]]))
        c.close()

    def test_bot_stress(self):
        watcher = C("watcher", "botroom")
        r = subprocess.run([sys.executable, str(ROOT / "bot.py"), "botroom", "--server",
                            f"ws://127.0.0.1:{PORT}", "--count", "3"], cwd=ROOT, timeout=30)
        self.assertEqual(r.returncode, 0)
        watcher.until("user_leave")
        self.assertEqual(len(C("v", "botroom").welcome["strokes"]), 3)
        watcher.close()

    def test_save_load_roundtrip(self):
        """模擬：A 畫圖 -> 存成 .json -> 讀檔 -> 用 client 的方式送出 -> B 看到相同畫面。"""
        a, w = C("artist", "save"), C("watcher", "save")
        long_line = [[i / 1200, 0.5] for i in range(1200)]          # 超過單訊息上限
        a.send(type="draw", sid="dot", color="#123456", width=7, pts=[[0.3, 0.3]])
        for ch in chunk_pts(long_line):
            a.send(type="draw", sid="long", color="#ff0000", width=2, pts=ch)
        for _ in range(1 + len(list(chunk_pts(long_line)))):
            w.until("draw")                                          # 確定伺服器都處理完了
        original = C("saver", "save").welcome["strokes"]
        self.assertEqual(len(original), 2)
        text = json.dumps(dump_board(original))                     # 存檔
        loaded = parse_board(json.loads(text))                       # 讀檔
        a.send(type="clear")
        a.until("clear")
        viewer = C("viewer", "save")
        for i, s in enumerate(loaded):                               # 載入 = 重新送出
            for ch in chunk_pts(s["pts"]):
                a.send(type="draw", sid=f"new{i}", color=s["color"], width=s["width"], pts=ch)
        got = [viewer.until("draw") for _ in range(1 + len(list(chunk_pts(long_line))))]
        self.assertEqual(got[0]["color"], "#123456")
        restored = C("check", "save").welcome["strokes"]
        self.assertEqual([(s["color"], s["width"]) for s in restored],
                         [(s["color"], s["width"]) for s in original])
        # 去掉分段時重複的接點後，點座標與原本完全相同
        dedup = [p for i, p in enumerate(restored[1]["pts"])
                 if i == 0 or p != restored[1]["pts"][i - 1]]
        self.assertEqual(dedup, long_line)
        a.close(); w.close(); viewer.close()

    def test_text_move_delete(self):
        a, b = C("amy", "objs"), C("ben", "objs")
        a.send(type="text", sid="t1", x=0.5, y=0.5, text="哈囉", color="#ff0000", size=30)
        m = b.until("text")
        self.assertEqual((m["text"], m["owner"], m["kind"], m["size"]), ("哈囉", "amy", "text", 30))
        b.send(type="text", sid="t1", x=0.9, y=0.9, text="改", color="#00ff00", size=999)
        m = a.until("text")                                 # 別人也能編輯文字
        self.assertEqual((m["text"], m["x"], m["size"], m["owner"]), ("改", 0.5, 120, "amy"))
        b.send(type="text", sid="t2", x=0.1, y=0.1, text="   ")   # 空白文字被拒絕
        a.send(type="draw", sid="s1", pts=[[0.1, 0.1], [0.3, 0.2]])
        b.until("draw")
        a.send(type="text", sid="s1", x=0, y=0, text="把筆跡改成文字")  # 不允許
        b.send(type="draw", sid="t1", pts=[[0.2, 0.2]])                 # 不允許
        b.send(type="move", sids=["s1", "t1", "nope"], dx=0.6, dy=-5)
        m = a.until("move")
        self.assertEqual(m["sids"], ["s1", "t1"])
        self.assertAlmostEqual(m["dx"], 0.5)                # 文字 x=0.5 -> 最多右移 0.5
        self.assertAlmostEqual(m["dy"], -0.1)               # 筆跡 y 最小 0.1 -> 最多上移 0.1
        b.send(type="delete", sids=["t1", "t1", "ghost"])
        self.assertEqual(a.until("remove")["sid"], "t1")
        state = {o["sid"]: o for o in C("v", "objs").welcome["strokes"]}
        self.assertEqual(list(state), ["s1"])
        self.assertAlmostEqual(state["s1"]["pts"][0][0], 0.6)
        self.assertAlmostEqual(state["s1"]["pts"][0][1], 0.0)
        a.send(type="undo")                                 # 復原也適用文字
        a.send(type="text", sid="t3", x=0.2, y=0.2, text="最後", color="#000000", size=12)
        b.until("text")
        a.send(type="undo")
        self.assertEqual(b.until("remove")["sid"], "t3")
        a.close(); b.close()

    def test_scale(self):
        a, b = C("amy", "scale"), C("ben", "scale")
        a.send(type="draw", sid="s", width=4, pts=[[0.2, 0.2], [0.4, 0.3]])
        a.send(type="text", sid="t", x=0.3, y=0.25, text="字", color="#000000", size=20)
        b.until("text")
        b.send(type="scale", sids=["s", "t", "x"], ox=0.2, oy=0.2, sx=2, sy=2)
        m = a.until("scale")
        self.assertEqual((m["sids"], m["sx"], m["sy"]), (["s", "t"], 2.0, 2.0))
        st = {o["sid"]: o for o in C("v", "scale").welcome["strokes"]}
        self.assertEqual(st["s"]["pts"], [[0.2, 0.2], [0.6, 0.4]])
        self.assertEqual((st["s"]["width"], st["t"]["size"]), (8, 40))
        self.assertAlmostEqual(st["t"]["x"], 0.4)
        b.send(type="scale", sids=["s"], ox=0.2, oy=0.2, sx=100, sy=-3)   # 超出畫布 / 翻轉
        m = a.until("scale")
        self.assertAlmostEqual(m["sx"], 2.0)                 # 0.2 + 0.4*2 = 1.0 剛好到邊
        self.assertAlmostEqual(m["sy"], 0.05)                # 不可翻轉，最小 0.05
        a.close(); b.close()


class FileFormatTest(unittest.TestCase):
    def test_parse_board(self):
        data = {"strokes": [{"color": "#00ff00", "width": 4, "pts": [[0.1, 0.2]]},
                            {"color": "evil", "width": 999, "pts": [[5, -1]]},
                            {"oops": 1}]}
        out = parse_board(data)
        self.assertEqual(out, [{"color": "#00ff00", "width": 4, "pts": [[0.1, 0.2]]},
                               {"color": "#000000", "width": 60, "pts": [[1.0, 0.0]]}])
        self.assertEqual(parse_board([]), [])                       # 空白畫布也合法
        for bad in ({"hello": 1}, "text", [{"x": 1}]):
            with self.assertRaises(ValueError):
                parse_board(bad)

    def test_text_objects_and_move(self):
        board = {"strokes": [{"kind": "text", "x": 2, "y": 0.5, "text": "嗨", "size": 1},
                             {"kind": "text", "x": 0.1, "y": 0.1, "text": "  "},
                             {"color": "#111111", "width": 2, "pts": [[0.2, 0.3], [0.4, 0.9]]}]}
        objs = parse_board(json.loads(json.dumps(dump_board(parse_board(board)))))
        self.assertEqual(objs[0], {"kind": "text", "x": 1.0, "y": 0.5, "text": "嗨",
                                   "color": "#000000", "size": 6})
        self.assertEqual(len(objs), 2)
        self.assertEqual(objects_bbox(objs), (0.2, 0.3, 1.0, 0.9))
        dx, dy = clamp_move(objs, -0.5, 0.5)
        self.assertAlmostEqual(dx, -0.2); self.assertAlmostEqual(dy, 0.1)
        for o in objs:
            move_obj(o, dx, dy)
        self.assertAlmostEqual(objs[0]["x"], 0.8)
        self.assertAlmostEqual(objs[1]["pts"][1][1], 1.0)

    def test_chunk_pts(self):
        for n in (1, 2, 499, 500, 501, 1200):
            pts = [[i, i] for i in range(n)]
            chunks = list(chunk_pts(pts))
            self.assertTrue(all(len(c) <= 500 for c in chunks))
            self.assertEqual(chunks[0][0], pts[0])
            self.assertEqual(chunks[-1][-1], pts[-1])
            for x, y in zip(chunks, chunks[1:]):
                self.assertEqual(x[-1], y[0])                        # 段與段相接


if __name__ == "__main__":
    unittest.main(verbosity=2)
