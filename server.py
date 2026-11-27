#!/usr/bin/env python3
"""協作白板伺服器 (asyncio + WebSocket)

- 每個房間各自保存畫布上的物件 (筆跡、文字)，新加入者會收到完整畫面
- 訊息依伺服器接收順序轉發給同房間其他人，伺服器的順序就是最終順序
- 任何人都能移動 / 縮放 / 刪除物件、編輯文字；復原只會移除自己最後新增的物件
- 一般 HTTP 請求回傳狀態頁 (雲端平台的健康檢查)，WebSocket 請求進入白板協定

    python3 server.py              # port 取自環境變數 PORT (Render 會設定)，沒有則 9100
"""
import argparse
import asyncio
import logging
import os
import re
import secrets
import time
from collections import OrderedDict

from protocol import (DEFAULT_PORT, MAX_FONT, MAX_PTS_PER_MSG, MAX_TEXT, MAX_WIDTH, MIN_FONT,
                      clamp01, clamp_move, clamp_scale, clean_obj, decode, encode, is_text,
                      move_obj, scale_obj, valid_color)
from transport import MAX_MESSAGE, read_http_headers, ws_accept_key, ws_frame, ws_read_message

MAX_NAME = 16
MAX_ROOM = 20
MAX_SID = 40
MAX_STROKES = 5000          # 每個房間的物件上限，超過就丟掉最舊的
MAX_ROOMS = 500             # 房間數上限，超過就清掉最久沒人用的空房間
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford Base32 (不含 I L O U)
CODE_LEN = 5


class Client:
    def __init__(self, writer):
        self.writer = writer
        self.name = None
        self.room = None
        self.addr = writer.get_extra_info("peername")


class Room:
    def __init__(self, name):
        self.name = name
        self.users = {}                    # 暱稱 -> Client
        self.strokes = OrderedDict()       # sid -> 物件 (畫布由下到上的順序)
        self.touched = time.monotonic()    # 最後有人使用的時間


class WhiteboardServer:
    def __init__(self):
        self.rooms = {}

    def new_room_code(self):
        self.purge_rooms()
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
            if code not in self.rooms:
                self.rooms[code] = Room(code)
                return code

    def purge_rooms(self):
        """房間太多時，移除沒有人在用、最久沒動的房間。"""
        if len(self.rooms) < MAX_ROOMS:
            return
        idle = sorted((r.touched, n) for n, r in self.rooms.items() if not r.users)
        for _, n in idle[:len(self.rooms) - MAX_ROOMS + 1]:
            del self.rooms[n]

    # ---------- 傳送 ----------
    async def send(self, c, data: bytes):
        try:
            c.writer.write(ws_frame(data))
            await asyncio.wait_for(c.writer.drain(), timeout=5)
        except Exception:
            pass  # 斷線的連線由讀取迴圈清理

    async def broadcast(self, room, exclude=None, **obj):
        data = encode(**obj)
        await asyncio.gather(*(self.send(c, data)
                               for c in list(room.users.values()) if c is not exclude))

    # ---------- 連線 ----------
    async def handle(self, reader, writer):
        c = Client(writer)
        try:
            method, path, headers = await read_http_headers(reader, await reader.readline())
            if "websocket" in headers.get("upgrade", "").lower() and "sec-websocket-key" in headers:
                await self.handle_ws(c, reader, writer, headers)
            else:
                await self.handle_http(writer, method, path)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                ValueError):
            pass
        except Exception:
            logging.exception("處理 %s 發生錯誤", c.addr)
        finally:
            await self.leave(c)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def handle_ws(self, c, reader, writer, headers):
        accept = ws_accept_key(headers["sec-websocket-key"])
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        logging.info("連線: %s", headers.get("x-forwarded-for", c.addr))
        while (data := await ws_read_message(reader, writer)) is not None:
            try:
                msg = decode(data)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(msg, dict):
                await self.dispatch(c, msg)

    async def handle_http(self, writer, method, path):
        """狀態頁 / 健康檢查。"""
        if path.split("?")[0] in ("/", "/healthz"):
            users = sum(len(r.users) for r in self.rooms.values())
            body = (f"collab-whiteboard server OK\nonline users: {users}\n"
                    f"rooms: {len(self.rooms)}\n").encode()
            status = "200 OK"
        else:
            body, status = b"not found\n", "404 Not Found"
        writer.write((f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
                     + (b"" if method == "HEAD" else body))
        await writer.drain()

    # ---------- 訊息處理 ----------
    async def dispatch(self, c, msg):
        t = msg.get("type")
        if t == "hello":
            await self.hello(c, msg)
            return
        if t == "create_room":
            await self.send(c, encode(type="room_created", code=self.new_room_code()))
            return
        if t == "room_info":
            r = self.rooms.get(str(msg.get("room", ""))[:MAX_ROOM])
            await self.send(c, encode(type="room_info", room=msg.get("room"),
                                      exists=r is not None, users=len(r.users) if r else 0))
            return
        if c.room is None:
            await self.send(c, encode(type="error", text="請先送出 hello"))
            return
        room = self.rooms[c.room]
        room.touched = time.monotonic()
        if t == "draw":
            await self.draw(c, room, msg)
        elif t == "text":
            await self.text(c, room, msg)
        elif t == "delete":
            removed = [sid for sid in self.sid_list(msg) if room.strokes.pop(sid, None)]
            for sid in removed:
                await self.broadcast(room, type="remove", sid=sid)
        elif t == "move":
            sids = [sid for sid in self.sid_list(msg) if sid in room.strokes]
            try:
                objs = [room.strokes[sid] for sid in sids]
                dx, dy = clamp_move(objs, msg["dx"], msg["dy"])
            except (KeyError, TypeError, ValueError):
                return
            if sids and (dx or dy):
                for o in objs:
                    move_obj(o, dx, dy)
                await self.broadcast(room, exclude=c, type="move", sids=sids, dx=dx, dy=dy)
        elif t == "scale":
            sids = [sid for sid in self.sid_list(msg) if sid in room.strokes]
            try:
                objs = [room.strokes[sid] for sid in sids]
                ox, oy, sx, sy = clamp_scale(objs, msg["ox"], msg["oy"], msg["sx"], msg["sy"])
            except (KeyError, TypeError, ValueError):
                return
            if sids and (sx != 1 or sy != 1):
                for o in objs:
                    scale_obj(o, ox, oy, sx, sy)
                await self.broadcast(room, exclude=c, type="scale", sids=sids,
                                     ox=ox, oy=oy, sx=sx, sy=sy)
        elif t == "cursor":
            try:
                x, y = clamp01(msg["x"]), clamp01(msg["y"])
            except (KeyError, TypeError, ValueError):
                return
            await self.broadcast(room, exclude=c, type="cursor", name=c.name, x=x, y=y)
        elif t == "undo":
            for sid in reversed(room.strokes):
                if room.strokes[sid]["owner"] == c.name:
                    del room.strokes[sid]
                    await self.broadcast(room, type="remove", sid=sid)
                    break
        elif t == "restore":
            await self.restore(c, room, msg)
        elif t == "clear":
            room.strokes.clear()
            await self.broadcast(room, type="clear", by=c.name)
        # heartbeat 等其他訊息：只用來表示連線還活著，不需處理

    async def hello(self, c, msg):
        if c.room is not None:
            await self.leave(c)
        base = re.sub(r"\s+", "_", str(msg.get("name", "")).strip())[:MAX_NAME] or "guest"
        room_name = str(msg.get("room", "")).strip()[:MAX_ROOM] or "lobby"
        room = self.rooms.setdefault(room_name, Room(room_name))
        name, i = base, 2
        while name in room.users:           # 暱稱重複就自動加編號
            name, i = f"{base}_{i}", i + 1
        c.name, c.room = name, room_name
        room.users[name] = c
        await self.send(c, encode(type="welcome", name=name, room=room_name,
                                  users=sorted(room.users),
                                  strokes=list(room.strokes.values())))
        await self.broadcast(room, exclude=c, type="user_join", name=name)
        logging.info("%s 加入 [%s] (%d 人)", name, room_name, len(room.users))

    def add_object(self, room, sid, obj):
        room.strokes[sid] = obj
        while len(room.strokes) > MAX_STROKES:
            room.strokes.popitem(last=False)

    async def draw(self, c, room, msg):
        """新增筆跡，或接續自己正在畫的筆跡。"""
        try:
            sid = str(msg["sid"])[:MAX_SID]
            color = valid_color(msg.get("color"))
            width = min(MAX_WIDTH, max(1, int(msg.get("width", 3))))
            pts = [[clamp01(p[0]), clamp01(p[1])] for p in msg["pts"][:MAX_PTS_PER_MSG]]
        except (KeyError, TypeError, ValueError, IndexError):
            return
        if not pts:
            return
        s = room.strokes.get(sid)
        if s is None:
            s = dict(sid=sid, owner=c.name, color=color, width=width, pts=[])
            self.add_object(room, sid, s)
        elif is_text(s) or s["owner"] != c.name:
            return                          # 不能接續別人的筆跡
        s["pts"].extend(pts)
        await self.broadcast(room, exclude=c, type="draw", sid=sid, owner=c.name,
                             color=s["color"], width=s["width"], pts=pts)

    async def text(self, c, room, msg):
        """新增文字框；sid 已存在時代表修改內容 / 顏色 / 字級 (位置以伺服器為準)。"""
        try:
            sid = str(msg["sid"])[:MAX_SID]
            text = str(msg.get("text", ""))[:MAX_TEXT]
            size = min(MAX_FONT, max(MIN_FONT, int(msg.get("size", 20))))
            x, y = clamp01(msg["x"]), clamp01(msg["y"])
        except (KeyError, TypeError, ValueError):
            return
        if not text.strip():
            return
        s = room.strokes.get(sid)
        if s is None:
            s = dict(sid=sid, owner=c.name, kind="text", x=x, y=y)
            self.add_object(room, sid, s)
        elif not is_text(s):
            return                          # 不能把筆跡改成文字
        s.update(text=text, color=valid_color(msg.get("color")), size=size)
        await self.broadcast(room, exclude=c, type="text", **s)

    async def restore(self, c, room, msg):
        """伺服器重啟後房間是空的：接受第一個重新連上的人傳回的畫面，並同步給所有人。"""
        if room.strokes or not isinstance(msg.get("strokes"), list):
            return                          # 房間已經有內容 (別人先復原了)
        for s in msg["strokes"][:MAX_STROKES]:
            if not isinstance(s, dict):
                continue
            o = clean_obj(s)
            sid = str(s.get("sid", ""))[:MAX_SID]
            if o and sid and sid not in room.strokes:
                o["sid"] = sid
                o["owner"] = str(s.get("owner") or c.name)[:MAX_NAME + 4]
                room.strokes[sid] = o
        if room.strokes:
            logging.info("%s 復原了 [%s] 的 %d 個物件", c.name, room.name, len(room.strokes))
            await self.broadcast(room, type="reset", by=c.name, strokes=list(room.strokes.values()))

    @staticmethod
    def sid_list(msg):
        sids = msg.get("sids")
        if not isinstance(sids, list):
            return []
        return list(dict.fromkeys(str(x)[:MAX_SID] for x in sids[:MAX_STROKES]))

    async def leave(self, c):
        if c.room is None:
            return
        room = self.rooms.get(c.room)
        if room and room.users.get(c.name) is c:
            del room.users[c.name]
            room.touched = time.monotonic()
            await self.broadcast(room, type="user_leave", name=c.name)
            logging.info("%s 離開 [%s]", c.name, c.room)
        c.room = None


async def main():
    ap = argparse.ArgumentParser(description="協作白板伺服器")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)),
                    help="預設讀環境變數 PORT，沒有則 9100")
    ap.add_argument("--public", action="store_true", help=argparse.SUPPRESS)  # 舊的啟動指令相容
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    srv = WhiteboardServer()
    server = await asyncio.start_server(srv.handle, args.host, args.port, limit=MAX_MESSAGE)
    logging.info("白板伺服器啟動於 port %d (本機測試：python3 start.py --server ws://127.0.0.1:%d)",
                 args.port, args.port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
