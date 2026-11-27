#!/usr/bin/env python3
"""白板啟動器：建立房間 / 輸入房間號加入 (線上)。

    python3 start.py                     # 跳出小視窗：建立房間，或輸入房間號加入
    python3 start.py create              # 建立房間，顯示房間號
    python3 start.py join K7Q2M          # 輸入房間號加入
    python3 start.py join K7Q2M --check  # 只檢查房間在不在，不開白板
    (都可加 --name 暱稱)

線上伺服器網址依序讀取：--server > 環境變數 WHITEBOARD_SERVER > config.json。
開發 / 測試時可以在本機開伺服器，再用 --server ws://127.0.0.1:9100 連線。
"""
import argparse
import getpass
import re
import socket
import sys

import transport
from transport import configured_server

CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford Base32 (不含 I L O U)
CODE_LEN = 5
WAKE_HINT = "(伺服器休眠時第一次約需 1 分鐘)"
NO_SERVER_MSG = ("這台電腦還沒設定線上伺服器網址 (config.json 是空的)。\n"
                 "請執行 git pull 取得最新的 config.json；\n"
                 "或用  python3 start.py --server wss://你的網址.onrender.com")


# ---------- 與伺服器溝通 ----------
def server_error(e):
    msg = str(e) or e.__class__.__name__
    if isinstance(e, socket.gaierror):
        msg = "找不到伺服器網址 (網址打錯，或這台電腦沒有網路)"
    elif isinstance(e, (socket.timeout, TimeoutError)):
        msg = "連線逾時 (伺服器可能還在喚醒，請再試一次)"
    return RuntimeError(f"連不上線上伺服器：{msg}")


def normalize_code(code):
    """整理使用者輸入的房間號：去空白、轉大寫，O 當 0、I/L 當 1。"""
    c = re.sub(r"[\s-]+", "", str(code)).upper()
    c = c.replace("O", "0").replace("I", "1").replace("L", "1")
    if not c:
        raise ValueError("請輸入房間號")
    bad = sorted({ch for ch in c if ch not in CODE_ALPHABET})
    if bad:
        raise ValueError(f"房間號裡有無效的字元：{''.join(bad)}")
    if len(c) != CODE_LEN:
        raise ValueError(f"房間號應該是 {CODE_LEN} 碼")
    return c


def create_room(server):
    """請伺服器開一個新房間，回傳房間號。"""
    if not server:
        raise RuntimeError(NO_SERVER_MSG)
    try:
        reply = transport.request(server, timeout=90, type="create_room")
    except (OSError, ValueError) as e:
        raise server_error(e)
    if reply.get("type") != "room_created":
        raise RuntimeError("伺服器版本太舊，不支援建立房間 (請在 Render 重新部署最新版)")
    return reply["code"]


def join_room(server, code):
    """確認房間存在，回傳整理過的房間號。房間號錯誤丟 ValueError，其他丟 RuntimeError。"""
    code = normalize_code(code)
    if not server:
        raise RuntimeError(NO_SERVER_MSG)
    try:
        reply = transport.request(server, timeout=90, type="room_info", room=code)
    except (OSError, ValueError) as e:
        raise server_error(e)
    if reply.get("type") != "room_info":
        raise RuntimeError("伺服器版本太舊，不支援房間號 (請在 Render 重新部署最新版)")
    if not reply.get("exists"):
        raise RuntimeError(f"找不到房間 {code}，請確認房間號 (伺服器重啟過的話，請房主重新建立)")
    return code


def run_client(server, room, name=None):
    import client                               # 用到才載入 tkinter (--check 不需要)
    try:
        client.run(server, room, name)
    except KeyboardInterrupt:
        pass


# ---------- 指令 ----------
def cmd_create(server, name=None):
    print(f"連線到 {server} ... {WAKE_HINT}")
    try:
        code = create_room(server)
    except RuntimeError as e:
        sys.exit(str(e))
    line = "=" * 44
    print(f"\n{line}\n  房間已建立！ 房間號：  {code}\n"
          f"  其他人執行  python3 start.py  並輸入房間號即可加入\n{line}\n")
    run_client(server, code, name)


def cmd_join(server, code, name=None, check=False):
    try:
        code = join_room(server, code)
    except (ValueError, RuntimeError) as e:
        sys.exit(str(e))
    print(f"房間 {code} 存在")
    if not check:
        run_client(server, code, name)


# ---------- 啟動視窗 ----------
def launcher_gui(server):
    """小視窗：建立房間，或輸入房間號加入。回傳 {"code", "name"} (關閉視窗則為空)。

    連線在背景執行緒進行，等伺服器喚醒時視窗不會卡住。
    """
    import threading
    import tkinter as tk
    root = tk.Tk()
    root.title("協作白板")
    root.resizable(False, False)
    result = {}
    f = tk.Frame(root, padx=24, pady=18)
    f.pack()
    tk.Label(f, text="協作白板", font=("TkDefaultFont", 18, "bold")).grid(
        row=0, column=0, columnspan=3, pady=(0, 12))
    tk.Label(f, text="暱稱").grid(row=1, column=0, sticky="e", padx=(0, 6))
    try:
        default_name = getpass.getuser()
    except Exception:
        default_name = "guest"
    name_var = tk.StringVar(value=default_name)
    tk.Entry(f, textvariable=name_var, width=18).grid(row=1, column=1, columnspan=2, sticky="we")
    status = tk.Label(f, text="", fg="#c62828", wraplength=320, justify="left")
    buttons = []

    def name():
        return name_var.get().strip() or "guest"

    def run_bg(text, work):
        """在背景執行 work()；成功就關閉視窗並回傳房間號，失敗顯示原因。"""
        status.config(text=text, fg="#555")
        for b in buttons:
            b.config(state=tk.DISABLED)
        box = {}

        def target():
            try:
                box["code"] = work()
            except Exception as e:          # noqa: BLE001  顯示給使用者
                box["error"] = e
        th = threading.Thread(target=target, daemon=True)
        th.start()

        def check():
            if th.is_alive():
                root.after(100, check)
                return
            for b in buttons:
                b.config(state=tk.NORMAL)
            if "error" in box:
                status.config(text=str(box["error"]), fg="#c62828")
                return
            result.update(code=box["code"], name=name())
            root.destroy()
        root.after(100, check)

    def on_create():
        run_bg(f"建立房間中… {WAKE_HINT}", lambda: create_room(server))

    def on_join(event=None):
        code = code_var.get()
        try:
            normalize_code(code)                # 格式錯誤直接提示，不用連線
        except ValueError as e:
            status.config(text=str(e), fg="#c62828")
            return
        run_bg(f"連線中… {WAKE_HINT}", lambda: join_room(server, code))

    b = tk.Button(f, text="建立房間", width=24, command=on_create)
    b.grid(row=2, column=0, columnspan=3, pady=(14, 6))
    buttons.append(b)
    tk.Label(f, text="──────  或  ──────", fg="#999").grid(row=3, column=0, columnspan=3)
    tk.Label(f, text="房間號").grid(row=4, column=0, sticky="e", padx=(0, 6), pady=(6, 0))
    code_var = tk.StringVar()
    entry = tk.Entry(f, textvariable=code_var, width=12, font=("TkFixedFont", 14))
    entry.grid(row=4, column=1, pady=(6, 0))
    entry.bind("<Return>", on_join)
    b = tk.Button(f, text="加入房間", command=on_join)
    b.grid(row=4, column=2, padx=(6, 0), pady=(6, 0))
    buttons.append(b)
    status.grid(row=5, column=0, columnspan=3, pady=(10, 0), sticky="w")
    if not server:                              # 沒有設定網址：直接說明，按鈕停用
        status.config(text=NO_SERVER_MSG)
        for b in buttons:
            b.config(state=tk.DISABLED)
    entry.focus_set()
    root.mainloop()
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    ap = argparse.ArgumentParser(description="協作白板：建立房間 / 輸入房間號加入")
    ap.add_argument("--server", help="伺服器網址 (預設讀 config.json)，例如 wss://xxx.onrender.com")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("create", help="建立房間")
    c.add_argument("--name", help="暱稱 (未指定會在視窗跳出詢問)")
    c.add_argument("--server", dest="server_sub", help=argparse.SUPPRESS)
    j = sub.add_parser("join", help="輸入房間號加入")
    j.add_argument("code", help="房間號，例如 K7Q2M")
    j.add_argument("--name", help="暱稱 (未指定會在視窗跳出詢問)")
    j.add_argument("--check", action="store_true", help="只檢查房間在不在，不開白板")
    j.add_argument("--server", dest="server_sub", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    server = configured_server(getattr(a, "server_sub", None) or a.server)

    if a.cmd is None:
        r = launcher_gui(server)
        if r:
            run_client(server, r["code"], r["name"])
        return
    if not server:
        sys.exit(NO_SERVER_MSG)
    if a.cmd == "create":
        cmd_create(server, a.name)
    else:
        cmd_join(server, a.code, a.name, a.check)


if __name__ == "__main__":
    main()
