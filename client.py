#!/usr/bin/env python3
"""協作白板 GUI 客戶端 (tkinter + WebSocket)，一般由 start.py 啟動。

執行緒模型：
  - 網路執行緒 (Net)：連線、自動重連、讀取伺服器訊息 -> 放進 queue
  - 主執行緒 (tkinter)：每 15ms 從 queue 取訊息更新畫面 (tkinter 只能在主執行緒操作)
效能：滑鼠拖曳時先在本機畫出，點座標累積起來每 30ms 批次送出一次。

工具：畫筆 / 橡皮擦 / 文字 / 選取 (移動、縮放、複製、剪下、貼上、刪除)
"""
import argparse
import json
import queue
import sys
import threading
import time
import tkinter as tk
import uuid
import zlib
from tkinter import colorchooser, filedialog, messagebox, simpledialog
from tkinter import font as tkfont

from protocol import (APP_ID, MAX_FONT, MAX_TEXT, MIN_FONT, chunk_pts, clamp_move, clamp_scale,
                      dump_board, is_text, move_obj, objects_bbox, parse_board, scale_obj)
from transport import ConnectionClosed, configured_server, open_connection

BG = "#ffffff"
PALETTE = ["#000000", "#e53935", "#fb8c00", "#fdd835", "#43a047",
           "#1e88e5", "#8e24aa", "#6d4c41"]
FLUSH_MS = 30          # 筆跡批次送出間隔
CURSOR_INTERVAL = 0.05  # 游標位置最多每 50ms 送一次
RECONNECT_SEC = 2
HEARTBEAT_MS = 25000    # 心跳間隔：讓雲端平台知道還有人在用，不會進入休眠


# ====================== 網路執行緒 ======================
class Net(threading.Thread):
    """網路執行緒：連線、自動重連、把收到的訊息放進 queue。"""

    def __init__(self, server, name, room, inbox):
        super().__init__(daemon=True)
        self.server, self.name, self.room = server, name, room
        self.inbox = inbox
        self.conn = None
        self.lock = threading.Lock()
        self.running = True

    def run(self):
        first = True
        while self.running:
            try:
                # 伺服器休眠時，第一次連線要等它喚醒 (約 1 分鐘)
                if first:
                    self.inbox.put({"type": "_status", "online": False, "waking": True})
                conn = open_connection(self.server, timeout=90 if first else 10)
                conn.settimeout(None)
                with self.lock:
                    self.conn = conn
                self.send(type="hello", name=self.name, room=self.room)
                self.inbox.put({"type": "_status", "online": True})
                first = False
                while True:
                    self.inbox.put(conn.recv())
            except (OSError, ValueError, ConnectionClosed) as e:
                error = str(e) or e.__class__.__name__
            with self.lock:
                c, self.conn = self.conn, None
            if c:
                c.close()
            if self.running:
                status = {"type": "_status", "online": False}
                if first:                   # 從沒連上過：顯示原因
                    status["error"] = error
                self.inbox.put(status)
                time.sleep(RECONNECT_SEC)   # 自動重連

    def send(self, **obj):
        with self.lock:
            if not self.conn:
                return False
            try:
                self.conn.send(**obj)
                return True
            except OSError:
                return False

    def stop(self):
        self.running = False
        with self.lock:
            if self.conn:
                self.conn.shutdown()


def user_color(name):
    """依暱稱產生固定顏色，用於顯示他人游標。"""
    h = zlib.crc32(name.encode())
    return "#%02x%02x%02x" % (60 + h % 160, 60 + (h >> 8) % 160, 60 + (h >> 16) % 160)


# ====================== GUI ======================
TOOLS = [("pen", "畫筆 (P)", "crosshair"), ("eraser", "橡皮擦 (E)", "circle"),
         ("text", "文字 (T)", "xterm"), ("select", "選取 (V)", "arrow")]
SEL_COLOR = "#1e88e5"
HANDLE_SIZE = 9
MIN_SEL_BOX = 24          # 選取框最小邊長 (像素)，避免控制點蓋住細線
HANDLE_CURSORS = {"nw": "top_left_corner", "ne": "top_right_corner", "sw": "bottom_left_corner",
                  "se": "bottom_right_corner", "n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
                  "e": "sb_h_double_arrow", "w": "sb_h_double_arrow"}
PASTE_OFFSET = 0.02


def new_sid():
    return uuid.uuid4().hex[:12]


def text_width_units(line):
    """估算一行文字的寬度 (中日韓全形字約佔兩個字元寬)。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in line)


class App:
    def __init__(self, root, net, inbox):
        self.root, self.net, self.inbox = root, net, inbox
        self.code = net.room       # 房間號
        self.welcomed = False      # 收過 welcome 了嗎 (用來判斷是否為「重新」連線)
        self.my_name = net.name
        self.online = False
        self.users = []
        self.strokes = {}          # sid -> 物件 (筆跡或文字，見 protocol.py)
        self.cursors = {}          # name -> [dot_id, text_id, x, y]
        self.color = PALETTE[0]
        self.tool = "pen"
        self.cur_sid = None        # 正在畫的筆跡
        self.pending = []          # 尚未送出的點
        self.last_sent = None      # 上一批最後一點 (讓批次之間線條連續)
        self.last_px = None
        self.last_cursor_t = 0.0
        self.mouse = (0.1, 0.1)    # 最後的滑鼠位置 (比例座標)，貼上純文字時使用
        self.resize_job = None
        self.pending_load = None   # 「清除後載入」：等伺服器確認清除後再載入
        self.selection = set()     # 已選取的 sid
        self.sel_bbox = None       # 選取範圍的整體外框 (像素)
        self.drag = None           # 選取工具的拖曳狀態 (移動 / 框選)
        self.editor = None         # 文字編輯框狀態
        self.clip = None           # 內部剪貼簿 (系統剪貼簿無法使用時的備援)
        self.paste_count = 0
        self.family = tkfont.nametofont("TkDefaultFont").actual("family")
        self.build_ui()
        self.root.after(15, self.poll)
        self.root.after(FLUSH_MS, self.flush_timer)
        self.root.after(HEARTBEAT_MS, self.heartbeat)

    # ====================== 介面 ======================
    def build_ui(self):
        self.root.title(f"協作白板 — 房間號 {self.code}" if self.code else "協作白板")
        self.root.geometry("1100x720")

        # 第一列：工具、顏色、粗細、字級
        bar = tk.Frame(self.root, padx=6, pady=3)
        bar.pack(side=tk.TOP, fill=tk.X)
        self.tool_btns = {}
        for key, label, _ in TOOLS:
            b = tk.Button(bar, text=label, width=9, command=lambda k=key: self.set_tool(k))
            b.pack(side=tk.LEFT, padx=1)
            self.tool_btns[key] = b
        tk.Frame(bar, width=12).pack(side=tk.LEFT)
        self.swatches = []
        for c in PALETTE:
            sw = tk.Label(bar, bg=c, width=2, height=1, relief=tk.RAISED, bd=2)
            sw.pack(side=tk.LEFT, padx=2)
            sw.bind("<Button-1>", lambda e, c=c: self.set_color(c))
            self.swatches.append((c, sw))
        tk.Button(bar, text="自訂色", command=self.pick_color).pack(side=tk.LEFT, padx=4)
        self.cur_swatch = tk.Label(bar, bg=self.color, width=3, relief=tk.SUNKEN, bd=2)
        self.cur_swatch.pack(side=tk.LEFT, padx=2)
        tk.Label(bar, text="粗細").pack(side=tk.LEFT, padx=(10, 0))
        self.width_var = tk.IntVar(value=4)
        tk.Scale(bar, from_=1, to=40, orient=tk.HORIZONTAL, variable=self.width_var,
                 length=100, showvalue=True).pack(side=tk.LEFT)
        tk.Label(bar, text="字級").pack(side=tk.LEFT, padx=(10, 0))
        self.font_var = tk.StringVar(value="20")
        tk.Spinbox(bar, from_=MIN_FONT, to=MAX_FONT, increment=2, width=4,
                   textvariable=self.font_var).pack(side=tk.LEFT)

        # 第二列：編輯動作與檔案
        bar2 = tk.Frame(self.root, padx=6, pady=2)
        bar2.pack(side=tk.TOP, fill=tk.X)
        for text, cmd in [("復原 Ctrl+Z", self.undo), ("清除畫布", self.clear),
                          ("開啟 Ctrl+O", self.open_file), ("儲存 Ctrl+S", self.save)]:
            tk.Button(bar2, text=text, command=cmd).pack(side=tk.LEFT, padx=1)
        if self.code:
            tk.Label(bar2, text="   房間號").pack(side=tk.LEFT)
            tk.Label(bar2, text=self.code, fg="#1565c0",
                     font=(self.family, 13, "bold")).pack(side=tk.LEFT, padx=4)
            tk.Button(bar2, text="複製房間號", command=self.copy_code).pack(side=tk.LEFT)
        self.status = tk.Label(bar2, text="連線中...", fg="#888", anchor="e")
        self.status.pack(side=tk.RIGHT)

        bottom = tk.Frame(self.root)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        self.users_lbl = tk.Label(bottom, text="", anchor="w", fg="#555", padx=8)
        self.users_lbl.pack(side=tk.LEFT)
        tk.Label(bottom, text="按右鍵：複製 / 剪下 / 貼上 / 刪除", fg="#999",
                 padx=8).pack(side=tk.RIGHT)

        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, takefocus=1)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Double-Button-1>", self.on_double)
        self.canvas.bind("<Motion>", self.on_move)
        self.canvas.bind("<Configure>", self.on_resize)

        # 右鍵選單 (macOS 的右鍵是 Button-2，也支援 Control+點擊)
        try:
            self.aqua = self.root.tk.call("tk", "windowingsystem") == "aqua"
        except Exception:
            self.aqua = False
        self.mod_label = "Cmd" if self.aqua else "Ctrl"
        self.menu = tk.Menu(self.root, tearoff=0)
        self.canvas.bind("<Button-3>", self.on_context)
        if self.aqua:
            self.canvas.bind("<Button-2>", self.on_context)
            self.canvas.bind("<Control-Button-1>", self.on_context)

        # 快捷鍵 (Windows/Linux 用 Ctrl，macOS 另外綁 Command)
        for key, fn in [("z", self.undo), ("c", self.copy), ("x", self.cut), ("v", self.paste),
                        ("a", self.select_all), ("s", self.save), ("o", self.open_file)]:
            for mod in ("Control", "Command"):
                for k in (key, key.upper()):
                    try:
                        self.root.bind(f"<{mod}-{k}>", self.shortcut(fn))
                    except tk.TclError:
                        pass            # 此平台沒有 Command 鍵
        for k in ("<Delete>", "<BackSpace>"):
            self.root.bind(k, self.shortcut(self.delete_selection))
        self.root.bind("<Escape>", self.shortcut(lambda: self.set_selection(())))
        for key, tool in [("p", "pen"), ("e", "eraser"), ("t", "text"), ("v", "select")]:
            self.root.bind(f"<KeyPress-{key}>", self.shortcut(lambda t=tool: self.set_tool(t)))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.set_color(self.color)
        self.set_tool("pen")
        self.canvas.focus_set()

    def shortcut(self, fn):
        """快捷鍵包裝：正在輸入文字 (文字框、字級欄位) 時交給輸入框自己處理。"""
        def handler(e):
            if self.editor or isinstance(self.root.focus_get(), (tk.Entry, tk.Spinbox, tk.Text)):
                return None
            fn()
            return "break"
        return handler

    def refresh_status(self):
        if self.online:
            self.status.config(text=f"● 線上  {self.my_name} @ {self.net.room}", fg="#2e7d32")
        else:
            self.status.config(text="● 離線，重新連線中...", fg="#c62828")
        self.users_lbl.config(text=f"線上 {len(self.users)} 人: " + ", ".join(self.users))

    def copy_code(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.code)
        except tk.TclError:
            pass
        self.flash(f"已複製房間號 {self.code}，傳給其他人即可加入")

    def flash(self, text):
        self.status.config(text=text, fg="#1565c0")
        self.root.after(2000, self.refresh_status)

    def set_tool(self, tool):
        self.commit_editor()
        if tool != "select":
            self.set_selection(())
        self.tool = tool
        for key, _, cursor in TOOLS:
            self.tool_btns[key].config(relief=tk.SUNKEN if key == tool else tk.RAISED)
            if key == tool:
                self.canvas.config(cursor=cursor)

    def set_color(self, c):
        self.color = c
        self.cur_swatch.config(bg=c)
        for col, sw in self.swatches:
            sw.config(relief=tk.SUNKEN if col == c else tk.RAISED)
        if self.tool == "eraser":
            self.set_tool("pen")

    def pick_color(self):
        c = colorchooser.askcolor(color=self.color, title="選擇顏色")[1]
        if c:
            self.set_color(c.lower())

    def font_size(self):
        try:
            return min(MAX_FONT, max(MIN_FONT, int(self.font_var.get())))
        except (ValueError, tk.TclError):
            return 20

    # ====================== 座標轉換 ======================
    def size(self):
        return max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())

    def to_norm(self, x, y):
        """像素 -> 比例座標 (取到小數 4 位，與送給伺服器的值一致)。"""
        w, h = self.size()
        return [round(min(1.0, max(0.0, x / w)), 4), round(min(1.0, max(0.0, y / h)), 4)]

    def to_px(self, p):
        w, h = self.size()
        return p[0] * w, p[1] * h

    # ====================== 繪製 ======================
    def draw_pts(self, sid, color, width, pts):
        tag = "s_" + sid               # 加前綴，避免全數字 tag 被當成 item id
        coords = []
        for p in pts:
            coords.extend(self.to_px(p))
        if len(coords) == 2:           # 單點 -> 畫圓點
            x, y = coords
            r = width / 2
            self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=color, outline=color,
                                    tags=(tag, "stroke"))
        elif coords:
            self.canvas.create_line(*coords, fill=color, width=width, capstyle=tk.ROUND,
                                    joinstyle=tk.ROUND, smooth=False, tags=(tag, "stroke"))
        self.canvas.tag_raise("cursor")

    def draw_obj(self, sid, o):
        if is_text(o):
            px, py = self.to_px((o["x"], o["y"]))
            self.canvas.create_text(px, py, text=o["text"], fill=o["color"], anchor="nw",
                                    font=(self.family, o["size"]), tags=("s_" + sid, "stroke"))
            self.canvas.tag_raise("cursor")
        else:
            self.draw_pts(sid, o["color"], o["width"], o["pts"])

    def redraw_obj(self, sid):
        self.canvas.delete("s_" + sid)
        if sid in self.strokes:
            self.draw_obj(sid, self.strokes[sid])
            if self.editor and self.editor["sid"] == sid:
                self.canvas.itemconfigure("s_" + sid, state="hidden")

    def redraw_all(self):
        self.canvas.delete("stroke")
        for sid, o in self.strokes.items():
            self.draw_obj(sid, o)
        for name, cur in self.cursors.items():
            self.place_cursor(name, cur[2], cur[3])
        if self.editor:
            if self.editor["sid"]:
                self.canvas.itemconfigure("s_" + self.editor["sid"], state="hidden")
            self.canvas.coords(self.editor["item"], *self.to_px(self.editor["pos"]))
        self.refresh_sel()

    # ====================== 選取 ======================
    def hit(self, x, y, text_only=False):
        """回傳滑鼠位置最上層的物件 sid。"""
        for item in reversed(self.canvas.find_overlapping(x - 3, y - 3, x + 3, y + 3)):
            for tag in self.canvas.gettags(item):
                if tag.startswith("s_"):
                    o = self.strokes.get(tag[2:])
                    if o and (not text_only or is_text(o)):
                        return tag[2:]
        return None

    def set_selection(self, sids):
        self.selection = {s for s in sids if s in self.strokes}
        self.refresh_sel()

    def refresh_sel(self):
        """畫出每個已選物件的虛線框，以及整體外框的 8 個縮放控制點。"""
        self.canvas.delete("selbox")
        boxes = []
        for sid in self.selection:
            bb = self.canvas.bbox("s_" + sid)
            if bb:
                boxes.append(bb)
                x0, y0, x1, y1 = bb
                self.canvas.create_rectangle(x0 - 3, y0 - 3, x1 + 3, y1 + 3, outline=SEL_COLOR,
                                             dash=(4, 3), tags=("selbox",))
        self.sel_bbox = None
        if boxes and self.tool == "select":
            x0, y0 = min(b[0] for b in boxes) - 3, min(b[1] for b in boxes) - 3
            x1, y1 = max(b[2] for b in boxes) + 3, max(b[3] for b in boxes) + 3
            if x1 - x0 < MIN_SEL_BOX:
                xm = (x0 + x1) / 2
                x0, x1 = xm - MIN_SEL_BOX / 2, xm + MIN_SEL_BOX / 2
            if y1 - y0 < MIN_SEL_BOX:
                ym = (y0 + y1) / 2
                y0, y1 = ym - MIN_SEL_BOX / 2, ym + MIN_SEL_BOX / 2
            self.sel_bbox = (x0, y0, x1, y1)
            if len(boxes) > 1:
                self.canvas.create_rectangle(x0, y0, x1, y1, outline=SEL_COLOR,
                                             tags=("selbox",))
            r = HANDLE_SIZE / 2
            for name, (hx, hy) in self.handle_points(self.sel_bbox).items():
                self.canvas.create_rectangle(hx - r, hy - r, hx + r, hy + r, fill="white",
                                             outline=SEL_COLOR, width=1.5,
                                             tags=("selbox", "handle", "h_" + name))
        self.canvas.tag_raise("cursor")

    @staticmethod
    def handle_points(bb):
        x0, y0, x1, y1 = bb
        xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
        return {"nw": (x0, y0), "n": (xm, y0), "ne": (x1, y0), "e": (x1, ym),
                "se": (x1, y1), "s": (xm, y1), "sw": (x0, y1), "w": (x0, ym)}

    def hit_handle(self, x, y):
        for item in reversed(self.canvas.find_overlapping(x - 1, y - 1, x + 1, y + 1)):
            for tag in self.canvas.gettags(item):
                if tag.startswith("h_"):
                    return tag[2:]
        return None

    def start_scale(self, e, handle):
        """拖曳控制點：記下原始物件，之後每次移動都從原始狀態重新計算 (避免誤差累積)。"""
        x0, y0, x1, y1 = self.sel_bbox
        ox = {"w": x1, "nw": x1, "sw": x1, "e": x0, "ne": x0, "se": x0}.get(handle)  # 對邊為基準
        oy = {"n": y1, "nw": y1, "ne": y1, "s": y0, "sw": y0, "se": y0}.get(handle)
        hx, hy = self.handle_points(self.sel_bbox)[handle]
        sids = self.ordered(self.selection)
        self.drag = {"mode": "scale", "handle": handle, "sids": sids,
                     "orig": {s: json.loads(json.dumps(self.strokes[s])) for s in sids},
                     "ox": ox, "oy": oy, "hx": hx, "hy": hy, "params": None}

    def update_scale(self, e):
        d = self.drag
        w, h = self.size()
        corner = d["ox"] is not None and d["oy"] is not None
        sx = (e.x - d["ox"]) / (d["hx"] - d["ox"]) if d["ox"] is not None else 1.0
        sy = (e.y - d["oy"]) / (d["hy"] - d["oy"]) if d["oy"] is not None else 1.0
        keep = corner and not (e.state & 0x0001)       # 角落預設等比例，按 Shift 自由縮放
        if keep:
            sx = sy = max(sx, sy)
        ox = d["ox"] / w if d["ox"] is not None else 0.0
        oy = d["oy"] / h if d["oy"] is not None else 0.0
        objs = [d["orig"][s] for s in d["sids"]]
        ox, oy, sx, sy = clamp_scale(objs, ox, oy, sx, sy, keep_ratio=keep)
        # 四捨五入後再用伺服器同樣的規則檢查一次，確保伺服器算出的結果與本機完全相同
        params = clamp_scale(objs, *(round(v, 5) for v in (ox, oy, sx, sy)))
        d["params"] = params
        for s in d["sids"]:
            if s in self.strokes:
                o = json.loads(json.dumps(d["orig"][s]))
                scale_obj(o, *params)
                self.strokes[s] = o
                self.redraw_obj(s)
        self.refresh_sel()
        self.flash(f"縮放 {params[2] * 100:.0f}% × {params[3] * 100:.0f}%")

    def finish_scale(self, d):
        p = d["params"]
        sids = [s for s in d["sids"] if s in self.strokes]
        if p and sids and (p[2] != 1 or p[3] != 1):    # 放開滑鼠才送出一次 scale
            ox, oy, sx, sy = p
            self.net.send(type="scale", sids=sids, ox=ox, oy=oy, sx=sx, sy=sy)
        self.refresh_sel()

    def ordered(self, sids):
        """依畫布上的先後順序 (下層到上層) 排列。"""
        return [s for s in self.strokes if s in sids]

    def select_all(self):
        self.set_tool("select")
        self.set_selection(self.strokes.keys())
        self.flash(f"已選取 {len(self.selection)} 個物件")

    def delete_sids(self, sids):
        sids = [s for s in sids if s in self.strokes]
        if not sids:
            return
        for s in sids:
            self.strokes.pop(s, None)
            self.canvas.delete("s_" + s)
            self.selection.discard(s)
        self.net.send(type="delete", sids=sids)
        self.refresh_sel()

    def delete_selection(self):
        if not self.selection:
            self.flash("沒有選取任何物件 (先用「選取」工具點選或框選)")
            return
        n = len(self.selection)
        self.delete_sids(self.ordered(self.selection))
        self.flash(f"已刪除 {n} 個物件")

    # ====================== 複製 / 剪下 / 貼上 ======================
    def copy(self):
        objs = [self.strokes[s] for s in self.ordered(self.selection)]
        if not objs:
            self.flash("沒有選取任何物件 (先用「選取」工具點選或框選)")
            return False
        data = dump_board(objs)
        self.clip = json.loads(json.dumps(data))
        self.paste_count = 0
        try:                                # 放進系統剪貼簿，可貼到另一個視窗 / 另一台電腦的白板
            self.root.clipboard_clear()
            self.root.clipboard_append(json.dumps(data, ensure_ascii=False))
        except tk.TclError:
            pass
        self.flash(f"已複製 {len(objs)} 個物件")
        return True

    def cut(self):
        if self.copy():
            n = len(self.selection)
            self.delete_sids(self.ordered(self.selection))
            self.flash(f"已剪下 {n} 個物件")

    def read_clipboard(self):
        """讀取剪貼簿：回傳 (物件清單, 是否為純文字)；沒有可貼的內容回傳 None。"""
        try:
            txt = self.root.clipboard_get()
        except tk.TclError:
            txt = None
        if txt:
            try:
                data = json.loads(txt)
                if isinstance(data, dict) and data.get("app") == APP_ID:
                    objs = parse_board(data)          # 白板物件
                    return (objs, False) if objs else None
            except ValueError:
                pass
            if txt.strip():                           # 一般文字 -> 建立文字框
                return [{"kind": "text", "x": self.mouse[0], "y": self.mouse[1],
                         "text": txt.rstrip()[:MAX_TEXT], "color": self.color,
                         "size": self.font_size()}], True
            return None
        if self.clip:
            objs = parse_board(self.clip)
            return (objs, False) if objs else None
        return None

    def paste(self, at=None):
        """貼上；at 為比例座標時 (右鍵選單)，把內容的左上角放在該位置。"""
        got = self.read_clipboard()
        if not got:
            self.flash("剪貼簿是空的")
            return
        objs, plain = got
        if at is not None:
            if plain:
                objs[0]["x"], objs[0]["y"] = at
            else:
                minx, miny, _, _ = objects_bbox(objs)
                dx, dy = clamp_move(objs, at[0] - minx, at[1] - miny)
                for o in objs:
                    move_obj(o, dx, dy)
        elif not plain:                               # 每次貼上都往右下偏移一點，避免完全重疊
            self.paste_count += 1
            d = PASTE_OFFSET * self.paste_count
            dx, dy = clamp_move(objs, d, d)
            for o in objs:
                move_obj(o, dx, dy)
        sids = self.add_objects(objs)
        self.set_tool("select")
        self.set_selection(sids)
        self.flash(f"已貼上 {len(sids)} 個物件")

    # ====================== 右鍵選單 ======================
    def on_context(self, e):
        if self.drag:
            return "break"
        self.commit_editor()
        self.canvas.focus_set()
        sid = self.hit(e.x, e.y)
        inside = self.sel_bbox and (self.sel_bbox[0] <= e.x <= self.sel_bbox[2]
                                    and self.sel_bbox[1] <= e.y <= self.sel_bbox[3])
        if sid and sid not in self.selection:         # 右鍵點到物件：選取它
            self.set_tool("select")
            self.set_selection({sid})
        elif not sid and not inside:                  # 點到空白處：取消選取
            self.set_selection(())
        at = self.to_norm(e.x, e.y)
        has_sel = bool(self.selection)
        can_paste = self.read_clipboard() is not None
        one_text = (len(self.selection) == 1
                    and is_text(self.strokes[next(iter(self.selection))]))
        on = lambda ok: tk.NORMAL if ok else tk.DISABLED  # noqa: E731
        m, mod = self.menu, self.mod_label
        m.delete(0, "end")
        m.add_command(label="複製", accelerator=f"{mod}+C", command=self.copy, state=on(has_sel))
        m.add_command(label="剪下", accelerator=f"{mod}+X", command=self.cut, state=on(has_sel))
        m.add_command(label="貼上", accelerator=f"{mod}+V", command=lambda: self.paste(at=at),
                      state=on(can_paste))
        m.add_command(label="刪除", accelerator="Del", command=self.delete_selection,
                      state=on(has_sel))
        m.add_separator()
        if one_text:
            tsid = next(iter(self.selection))
            m.add_command(label="編輯文字", command=lambda: self.open_editor(sid=tsid))
        m.add_command(label="全選", accelerator=f"{mod}+A", command=self.select_all,
                      state=on(bool(self.strokes)))
        m.add_command(label="復原", accelerator=f"{mod}+Z", command=self.undo)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()
        return "break"

    def add_objects(self, objs):
        """把物件當成自己新增的送給伺服器 (貼上 / 開啟檔案共用；之後可用復原撤銷)。"""
        self.flush()
        sids = []
        for o in objs:
            sid = new_sid()
            if is_text(o):
                obj = {"kind": "text", "x": round(o["x"], 4), "y": round(o["y"], 4),
                       "text": o["text"], "color": o["color"], "size": o["size"]}
                self.net.send(type="text", sid=sid, **obj)
            else:
                pts = [[round(x, 4), round(y, 4)] for x, y in o["pts"]]
                obj = {"color": o["color"], "width": o["width"], "pts": pts}
                for chunk in chunk_pts(pts):
                    self.net.send(type="draw", sid=sid, color=obj["color"],
                                  width=obj["width"], pts=chunk)
            obj["owner"] = self.my_name
            self.strokes[sid] = obj
            self.draw_obj(sid, obj)
            sids.append(sid)
        return sids

    # ====================== 文字編輯框 ======================
    def open_editor(self, sid=None, px=None, py=None):
        self.commit_editor()
        if sid:
            o = self.strokes[sid]
            pos, text, color, size = [o["x"], o["y"]], o["text"], o["color"], o["size"]
            self.canvas.itemconfigure("s_" + sid, state="hidden")
        else:
            pos, text, color, size = self.to_norm(px, py), "", self.color, self.font_size()
        w = tk.Text(self.canvas, font=(self.family, size), fg=color, bg="#fffde7",
                    insertbackground=color, wrap="none", undo=True, bd=1, relief=tk.SOLID,
                    highlightthickness=1, highlightcolor=SEL_COLOR, padx=0, pady=0)
        w.insert("1.0", text)
        w.bind("<Return>", lambda e: (self.commit_editor(), "break")[1])   # Enter = 完成
        w.bind("<Shift-Return>", lambda e: None)                          # Shift+Enter = 換行
        w.bind("<Escape>", lambda e: (self.commit_editor(cancel=True), "break")[1])
        w.bind("<FocusOut>", lambda e: self.commit_editor())
        w.bind("<KeyRelease>", lambda e: self.autosize_editor())
        item = self.canvas.create_window(*self.to_px(pos), window=w, anchor="nw",
                                         tags=("editor",))
        self.editor = {"w": w, "item": item, "sid": sid, "pos": pos,
                       "color": color, "size": size}
        self.autosize_editor()
        w.focus_set()
        w.mark_set("insert", "end-1c")

    def autosize_editor(self):
        if not self.editor:
            return
        w = self.editor["w"]
        lines = w.get("1.0", "end-1c").split("\n")
        w.config(width=max(8, max(text_width_units(l) for l in lines) + 2), height=len(lines))

    def commit_editor(self, cancel=False):
        ed, self.editor = self.editor, None
        if not ed:
            return
        text = ed["w"].get("1.0", "end-1c").rstrip()
        self.canvas.delete(ed["item"])
        ed["w"].destroy()
        sid = ed["sid"]
        if sid:
            self.canvas.itemconfigure("s_" + sid, state="normal")
        self.canvas.focus_set()
        if cancel:
            return
        text = text[:MAX_TEXT]
        if sid and sid in self.strokes:               # 修改既有文字
            o = self.strokes[sid]
            if not text.strip():
                self.delete_sids([sid])               # 內容清空 = 刪除
            elif text != o["text"]:
                o["text"] = text
                self.net.send(type="text", sid=sid, x=o["x"], y=o["y"], text=text,
                              color=o["color"], size=o["size"])
                self.redraw_obj(sid)
        elif not sid and text.strip():                # 新文字框
            self.add_objects([{"kind": "text", "x": ed["pos"][0], "y": ed["pos"][1],
                               "text": text, "color": ed["color"], "size": ed["size"]}])
        self.refresh_sel()

    # ====================== 滑鼠事件 ======================
    def on_press(self, e):
        self.canvas.focus_set()
        if self.editor:                    # 點畫布其他地方 = 完成目前的文字
            self.commit_editor()
            return
        if self.tool in ("pen", "eraser"):
            self.start_stroke(e)
        elif self.tool == "text":
            sid = self.hit(e.x, e.y, text_only=True)
            self.open_editor(sid=sid) if sid else self.open_editor(px=e.x, py=e.y)
        elif self.tool == "select":
            handle = self.hit_handle(e.x, e.y) if self.sel_bbox else None
            if handle:                     # 點到控制點：開始縮放
                self.start_scale(e, handle)
                return
            sid = self.hit(e.x, e.y)
            shift = bool(e.state & 0x0001)
            if sid and shift:              # Shift+點擊：加選 / 取消選取
                self.set_selection(self.selection ^ {sid})
                return
            if sid:                        # 點到物件：選取並準備拖曳移動
                if sid not in self.selection:
                    self.set_selection({sid})
                self.drag = {"mode": "move", "x0": e.x, "y0": e.y, "applied": (0.0, 0.0),
                             "sids": self.ordered(self.selection)}
            else:                          # 點到空白：開始框選
                if not shift:
                    self.set_selection(())
                band = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline=SEL_COLOR,
                                                    dash=(3, 3), tags=("band",))
                self.drag = {"mode": "band", "x0": e.x, "y0": e.y, "item": band, "add": shift}

    def on_drag(self, e):
        self.on_move(e)
        if self.tool == "select":
            d = self.drag
            if not d:
                return
            if d["mode"] == "scale":
                self.update_scale(e)
                return
            if d["mode"] == "move":
                w, h = self.size()
                objs = [self.strokes[s] for s in d["sids"] if s in self.strokes]
                dx, dy = clamp_move(objs, (e.x - d["x0"]) / w, (e.y - d["y0"]) / h)
                ax, ay = d["applied"]
                for s in d["sids"]:
                    self.canvas.move("s_" + s, (dx - ax) * w, (dy - ay) * h)
                self.canvas.move("selbox", (dx - ax) * w, (dy - ay) * h)
                d["applied"] = (dx, dy)
            else:
                self.canvas.coords(d["item"], d["x0"], d["y0"], e.x, e.y)
            return
        if self.tool in ("pen", "eraser") and self.cur_sid:
            self.continue_stroke(e)

    def on_release(self, e):
        if self.tool == "select" and self.drag:
            d, self.drag = self.drag, None
            if d["mode"] == "scale":
                self.finish_scale(d)
                return
            if d["mode"] == "move":
                dx, dy = round(d["applied"][0], 5), round(d["applied"][1], 5)
                sids = [s for s in d["sids"] if s in self.strokes]
                if sids and (dx or dy):    # 放開滑鼠才送出一次 move，減少流量
                    for s in sids:
                        move_obj(self.strokes[s], dx, dy)
                    self.net.send(type="move", sids=sids, dx=dx, dy=dy)
                self.refresh_sel()
            else:
                x0, y0, x1, y1 = self.canvas.coords(d["item"])
                self.canvas.delete(d["item"])
                found = set()
                for item in self.canvas.find_overlapping(min(x0, x1), min(y0, y1),
                                                         max(x0, x1), max(y0, y1)):
                    for tag in self.canvas.gettags(item):
                        if tag.startswith("s_") and tag[2:] in self.strokes:
                            found.add(tag[2:])
                self.set_selection((self.selection | found) if d["add"] else found)
                if self.selection:
                    self.flash(f"已選取 {len(self.selection)} 個物件")
            return
        self.flush()
        self.cur_sid = None

    def on_double(self, e):
        """雙擊文字：編輯。"""
        if self.tool not in ("select", "text"):
            return
        sid = self.hit(e.x, e.y, text_only=True)
        if sid and not (self.editor and self.editor["sid"] == sid):
            self.drag = None
            self.open_editor(sid=sid)

    def on_move(self, e):
        self.mouse = self.to_norm(e.x, e.y)
        if self.tool == "select" and not self.drag:   # 滑到控制點上換成縮放游標
            handle = self.hit_handle(e.x, e.y) if self.sel_bbox else None
            self.canvas.config(cursor=HANDLE_CURSORS[handle] if handle else "arrow")
        now = time.monotonic()
        if now - self.last_cursor_t >= CURSOR_INTERVAL:
            self.last_cursor_t = now
            self.net.send(type="cursor", x=round(self.mouse[0], 4), y=round(self.mouse[1], 4))

    # ====================== 畫筆 ======================
    def start_stroke(self, e):
        self.cur_sid = new_sid()
        p = self.to_norm(e.x, e.y)
        eraser = self.tool == "eraser"
        color = BG if eraser else self.color
        width = self.width_var.get() * (3 if eraser else 1)
        self.strokes[self.cur_sid] = {"color": color, "width": width, "pts": [p],
                                      "owner": self.my_name}
        self.pending, self.last_sent, self.last_px = [p], None, (e.x, e.y)
        self.draw_pts(self.cur_sid, color, width, [p])

    def continue_stroke(self, e):
        s = self.strokes.get(self.cur_sid)
        if s is None:                      # 正在畫的筆跡被別人刪掉了
            self.cur_sid = None
            return
        p = self.to_norm(e.x, e.y)
        s["pts"].append(p)
        self.pending.append(p)
        lx, ly = self.last_px
        self.canvas.create_line(lx, ly, e.x, e.y, fill=s["color"], width=s["width"],
                                capstyle=tk.ROUND, tags=("s_" + self.cur_sid, "stroke"))
        self.last_px = (e.x, e.y)

    def flush(self):
        """把累積的點一次送出 (批次化，大幅減少封包數)。"""
        if not self.cur_sid or not self.pending:
            return
        s = self.strokes.get(self.cur_sid)
        if s is None:
            self.pending = []
            return
        pts = ([self.last_sent] if self.last_sent else []) + self.pending
        pts = [[round(x, 4), round(y, 4)] for x, y in pts]
        self.net.send(type="draw", sid=self.cur_sid, color=s["color"],
                      width=s["width"], pts=pts)
        self.last_sent = self.pending[-1]
        self.pending = []

    def heartbeat(self):
        self.net.send(type="heartbeat")
        self.root.after(HEARTBEAT_MS, self.heartbeat)

    def flush_timer(self):
        self.flush()
        self.root.after(FLUSH_MS, self.flush_timer)

    def on_resize(self, e):
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
        self.resize_job = self.root.after(80, self.redraw_all)

    # ====================== 其他工具 ======================
    def undo(self):
        self.flush()
        self.net.send(type="undo")      # 伺服器會廣播 remove 給所有人 (包含自己)

    def clear(self):
        if messagebox.askyesno("清除畫布", "確定要清除所有人的畫布嗎?"):
            self.net.send(type="clear")

    def save(self):
        self.commit_editor()
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("白板檔 (可再開啟)", "*.json"), ("PostScript 圖檔 (僅圖片)", "*.ps")])
        if not path:
            return
        try:
            if path.lower().endswith(".ps"):
                self.canvas.itemconfigure("cursor", state="hidden")
                self.canvas.itemconfigure("selbox", state="hidden")
                self.canvas.postscript(file=path, colormode="color")
                self.canvas.itemconfigure("cursor", state="normal")
                self.canvas.itemconfigure("selbox", state="normal")
            else:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(dump_board(self.strokes.values()), f, ensure_ascii=False)
        except OSError as e:
            messagebox.showerror("儲存失敗", str(e))
            return
        messagebox.showinfo("儲存", f"已儲存到\n{path}")

    def open_file(self):
        self.commit_editor()
        path = filedialog.askopenfilename(
            filetypes=[("白板檔", "*.json"), ("所有檔案", "*.*")])
        if not path:
            return
        if path.lower().endswith(".ps"):
            messagebox.showwarning("無法開啟", ".ps 是圖片格式，無法還原成筆跡。\n"
                                   "請用「儲存」存成 .json，之後就能用「開啟」讀回來。")
            return
        try:
            with open(path, encoding="utf-8") as f:
                objs = parse_board(json.load(f))
        except (OSError, ValueError) as e:     # JSONDecodeError 也是 ValueError
            messagebox.showerror("開啟失敗", f"無法讀取這個檔案:\n{e}")
            return
        if not self.online:
            messagebox.showwarning("開啟", "目前離線，連上伺服器後再試一次。")
            return
        ans = messagebox.askyesnocancel(
            "開啟", f"檔案共有 {len(objs)} 個物件，會同步給房間內所有人。\n\n"
                    "是：先清除目前畫布再載入\n否：疊加在目前畫布上")
        if ans is None:
            return
        if ans:
            self.pending_load = objs         # 等伺服器廣播 clear 回來再載入，避免被清掉
            self.net.send(type="clear")
        else:
            self.load_objects(objs)

    def load_objects(self, objs):
        self.add_objects(objs)
        self.flash(f"已載入 {len(objs)} 個物件")

    # ====================== 他人游標 ======================
    def place_cursor(self, name, x, y):
        px, py = self.to_px((x, y))
        cur = self.cursors.get(name)
        if cur is None:
            col = user_color(name)
            dot = self.canvas.create_oval(0, 0, 0, 0, fill=col, outline="white",
                                          tags=("cursor",))
            txt = self.canvas.create_text(0, 0, text=name, fill=col, anchor="nw",
                                          font=(self.family, 10, "bold"), tags=("cursor",))
            cur = self.cursors[name] = [dot, txt, x, y]
        cur[2], cur[3] = x, y
        self.canvas.coords(cur[0], px - 5, py - 5, px + 5, py + 5)
        self.canvas.coords(cur[1], px + 8, py + 6)
        self.canvas.tag_raise("cursor")

    def remove_cursor(self, name):
        cur = self.cursors.pop(name, None)
        if cur:
            self.canvas.delete(cur[0], cur[1])

    # ====================== 處理伺服器訊息 ======================
    def poll(self):
        for _ in range(2000):
            try:
                m = self.inbox.get_nowait()
            except queue.Empty:
                break
            try:
                self.handle(m)
            except (KeyError, TypeError, ValueError, tk.TclError):
                pass
        self.root.after(15, self.poll)

    def apply_state(self, strokes):
        """用一份完整狀態取代畫布 (welcome / reset)。"""
        if self.editor and self.editor["sid"]:
            self.commit_editor(cancel=True)
        if self.drag and self.drag["mode"] == "band":
            self.canvas.delete(self.drag["item"])
        self.cur_sid, self.pending, self.pending_load, self.drag = None, [], None, None
        self.strokes = strokes
        self.selection = set()
        self.redraw_all()

    def object_gone(self, sid):
        """物件被別人刪除 / 清除時，同步清掉本機的選取與編輯狀態。"""
        self.selection.discard(sid)
        if self.editor and self.editor["sid"] == sid:
            self.commit_editor(cancel=True)
        if self.cur_sid == sid:
            self.cur_sid = None

    def handle(self, m):
        t = m.get("type")
        if t == "_status":
            self.online = m["online"]
            if m.get("waking"):
                self.status.config(text="● 連線到線上伺服器中… (伺服器休眠時第一次約需 1 分鐘)",
                                   fg="#ef6c00")
                return
            if m.get("error"):
                self.status.config(text=f"● 連不上伺服器：{m['error'][:60]} (重試中...)",
                                   fg="#c62828")
                return
            if not self.online:
                self.users = []
                for n in list(self.cursors):
                    self.remove_cursor(n)
            self.refresh_status()
        elif t == "welcome":              # (重新)連線 -> 以伺服器的完整狀態為準
            self.my_name = m["name"]
            self.users = m["users"]
            local = dict(self.strokes)
            if self.welcomed and not m["strokes"] and local:
                # 重新連線時房間是空的 = 伺服器重啟過 (例如雲端休眠 / 更新)：
                # 把自己手上的畫面傳回去復原；如果別人先復原了，伺服器會忽略並送 reset 來
                self.net.send(type="restore",
                              strokes=[dict(o, sid=sid) for sid, o in local.items()])
                self.apply_state(local)
                self.flash("伺服器重新啟動過，正在復原畫面...")
            else:
                self.apply_state({s["sid"]: s for s in m["strokes"]})
            self.welcomed = True
            self.refresh_status()
        elif t == "reset":                # 有人復原了整個畫面
            self.apply_state({s["sid"]: s for s in m["strokes"]})
            self.flash(f"畫面已由 {m.get('by', '?')} 復原")
        elif t == "draw":
            s = self.strokes.setdefault(m["sid"], {"color": m["color"], "width": m["width"],
                                                   "pts": [], "owner": m["owner"]})
            s["pts"].extend(m["pts"])
            self.draw_pts(m["sid"], m["color"], m["width"], m["pts"])
            if m["sid"] in self.selection:
                self.refresh_sel()
        elif t == "text":
            self.strokes[m["sid"]] = {k: m[k] for k in
                                      ("kind", "x", "y", "text", "color", "size", "owner")}
            self.redraw_obj(m["sid"])
            if m["sid"] in self.selection:
                self.refresh_sel()
        elif t == "move":
            w, h = self.size()
            dragging = set(self.drag["sids"]) if self.drag and self.drag["mode"] == "move" else ()
            for sid in m["sids"]:
                if sid in self.strokes:
                    move_obj(self.strokes[sid], m["dx"], m["dy"])
                    if self.drag and self.drag["mode"] == "scale" and sid in self.drag["orig"]:
                        move_obj(self.drag["orig"][sid], m["dx"], m["dy"])
                    if sid in dragging:    # 我正在拖曳的物件被別人移動：以模型為準重畫
                        self.redraw_obj(sid)
                        self.canvas.move("s_" + sid, *(v * s for v, s in
                                                       zip(self.drag["applied"], (w, h))))
                    else:
                        self.canvas.move("s_" + sid, m["dx"] * w, m["dy"] * h)
            if self.editor and self.editor["sid"] in m["sids"]:
                o = self.strokes[self.editor["sid"]]
                self.editor["pos"] = [o["x"], o["y"]]
                self.canvas.coords(self.editor["item"], *self.to_px(self.editor["pos"]))
            self.refresh_sel()
        elif t == "scale":
            p = (m["ox"], m["oy"], m["sx"], m["sy"])
            for sid in m["sids"]:
                if sid in self.strokes:
                    scale_obj(self.strokes[sid], *p)
                    self.redraw_obj(sid)
                    if self.drag and self.drag["mode"] == "scale" and sid in self.drag["orig"]:
                        scale_obj(self.drag["orig"][sid], *p)   # 我正在縮放時別人也縮放了
            self.refresh_sel()
        elif t == "remove":
            self.strokes.pop(m["sid"], None)
            self.canvas.delete("s_" + m["sid"])
            self.object_gone(m["sid"])
            self.refresh_sel()
        elif t == "clear":
            for sid in list(self.strokes):
                self.object_gone(sid)
            self.strokes.clear()
            self.canvas.delete("stroke")
            self.refresh_sel()
            self.flash(f"{m['by']} 清除了畫布")
            if self.pending_load is not None and m["by"] == self.my_name:
                objs, self.pending_load = self.pending_load, None
                self.load_objects(objs)
        elif t == "cursor":
            self.place_cursor(m["name"], m["x"], m["y"])
        elif t == "user_join":
            if m["name"] not in self.users:
                self.users = sorted(self.users + [m["name"]])
            self.refresh_status()
        elif t == "user_leave":
            self.users = [u for u in self.users if u != m["name"]]
            self.remove_cursor(m["name"])
            self.refresh_status()
        elif t == "error":
            self.status.config(text="錯誤: " + m.get("text", ""), fg="#c62828")

    def on_close(self):
        self.commit_editor()
        self.net.stop()
        self.root.destroy()


def run(server, room, name=None):
    """開啟白板視窗，直到使用者關閉。"""
    root = tk.Tk()
    if not name:
        root.withdraw()
        name = simpledialog.askstring("協作白板", "請輸入暱稱:", parent=root) or "guest"
        root.deiconify()
    inbox = queue.Queue()
    net = Net(server, name, room, inbox)
    App(root, net, inbox)
    net.start()
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="協作白板客戶端 (一般請用 start.py 啟動)")
    ap.add_argument("--server", help="伺服器網址 (預設讀 config.json)，例如 wss://xxx.onrender.com")
    ap.add_argument("--name", help="暱稱 (未指定會詢問)")
    ap.add_argument("--room", required=True, help="房間號")
    args = ap.parse_args()
    server = configured_server(args.server)
    if not server:
        sys.exit("沒有設定伺服器網址：請用 --server 或在 config.json 填入")
    run(server, args.room, args.name)


if __name__ == "__main__":
    main()
