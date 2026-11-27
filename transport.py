"""傳輸層：WebSocket (RFC 6455)，只用 Python 標準函式庫實作。

雲端平台 (例如 Render) 只轉發 HTTP / WebSocket，所以白板的 JSON 訊息走 WebSocket，
每則訊息是一個 text frame。

伺服器端 (asyncio)：read_http_headers / ws_accept_key / ws_frame / ws_read_message
客戶端 (blocking) ：open_connection(server) -> Conn (send / recv / close)、request()
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from protocol import encode

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 16 * 1024 * 1024     # 單則訊息上限 (復原整張畫布時可能較大)
OP_CONT, OP_TEXT, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x8, 0x9, 0xA


class ConnectionClosed(Exception):
    pass


# ====================== frame 編碼 (共用) ======================
def ws_accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def mask_bytes(data: bytes, key: bytes) -> bytes:
    """用 4 byte 金鑰做 XOR 遮罩 (遮罩與解遮罩是同一個運算)。以大整數一次運算，速度快。"""
    if not data or not key:
        return data
    n = len(data)
    k = int.from_bytes((key * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(data, "big") ^ k).to_bytes(n, "big")


def ws_frame(payload: bytes, opcode=OP_TEXT, mask=False) -> bytes:
    """組一個完整 (FIN=1) 的 frame。客戶端送出的 frame 依規定必須加遮罩。"""
    n = len(payload)
    head = bytearray([0x80 | opcode])
    mbit = 0x80 if mask else 0
    if n < 126:
        head.append(mbit | n)
    elif n < 65536:
        head.append(mbit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(mbit | 127)
        head += struct.pack("!Q", n)
    if mask:
        key = os.urandom(4)
        head += key
        payload = mask_bytes(payload, key)
    return bytes(head) + payload


# ====================== 伺服器端 (asyncio) ======================
async def read_http_headers(reader, first_line: bytes):
    """讀完 HTTP 請求的 header。回傳 (method, path, {header 小寫: 值})。"""
    try:
        method, path, _ = first_line.decode("latin-1").split(" ", 2)
    except ValueError:
        raise ValueError("不是 HTTP 請求")
    headers = {}
    while True:
        line = await reader.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        if len(headers) > 100:
            raise ValueError("header 太多")
        k, _, v = line.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    return method, path, headers


async def ws_read_message(reader, writer):
    """讀一則完整訊息 (會合併分段 frame、自動回應 ping)。對方關閉時回傳 None。"""
    parts, total = [], 0
    while True:
        b1, b2 = await reader.readexactly(2)
        fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", await reader.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", await reader.readexactly(8))[0]
        if total + n > MAX_MESSAGE:
            raise ValueError("訊息太大")          # 先檢查長度再讀資料，避免被拖住
        key = await reader.readexactly(4) if b2 & 0x80 else b""
        data = mask_bytes(await reader.readexactly(n), key)
        if opcode == OP_CLOSE:
            try:
                writer.write(ws_frame(data[:2], OP_CLOSE))
                await writer.drain()
            except Exception:
                pass
            return None
        if opcode == OP_PING:
            writer.write(ws_frame(data, OP_PONG))
            await writer.drain()
            continue
        if opcode == OP_PONG:
            continue
        parts.append(data)
        total += n
        if fin:
            return b"".join(parts)


# ====================== 客戶端 (blocking) ======================
def config_paths():
    """config.json 的位置。打包成執行檔時，放在執行檔旁邊的 config.json 優先於內建的。"""
    if getattr(sys, "frozen", False):
        yield Path(sys.executable).resolve().parent / "config.json"
        yield Path(getattr(sys, "_MEIPASS", "")) / "config.json"
    yield Path(__file__).resolve().parent / "config.json"


def configured_server(cli=None):
    """伺服器網址：參數 > 環境變數 WHITEBOARD_SERVER > config.json。沒有設定回傳 None。"""
    if cli and cli.strip():
        return cli.strip()
    if os.environ.get("WHITEBOARD_SERVER", "").strip():
        return os.environ["WHITEBOARD_SERVER"].strip()
    for path in config_paths():
        try:
            server = (json.loads(path.read_text(encoding="utf-8")).get("server") or "").strip()
        except (OSError, ValueError, AttributeError):
            continue
        if server:
            return server
    return None


def parse_server(server):
    """'wss://host/path' -> (scheme, host, port, path)。https:// / http:// 會當成 wss:// / ws://。"""
    u = urlsplit(server.strip())
    scheme = {"https": "wss", "http": "ws"}.get(u.scheme.lower(), u.scheme.lower())
    if scheme not in ("ws", "wss"):
        raise ValueError(f"伺服器網址要以 wss:// 開頭：{server}")
    if not u.hostname:
        raise ValueError(f"伺服器網址缺少主機名稱：{server}")
    port = u.port or (443 if scheme == "wss" else 80)
    return scheme, u.hostname, port, (u.path or "/") + (f"?{u.query}" if u.query else "")


class Conn:
    """一條已建立的 WebSocket 連線：send(**obj) / recv() / close()。"""

    def __init__(self, sock, buf=b""):
        self.sock, self.buf = sock, buf
        self.lock = threading.Lock()

    def send(self, **obj):
        data = ws_frame(encode(**obj), OP_TEXT, mask=True)
        with self.lock:
            self.sock.sendall(data)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise ConnectionClosed()
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def recv_raw(self):
        """回傳下一則訊息的 bytes；連線結束丟出 ConnectionClosed。"""
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            if n > MAX_MESSAGE:
                raise ConnectionClosed("訊息太大")
            key = self._read(4) if b2 & 0x80 else b""
            data = mask_bytes(self._read(n), key)
            if opcode == OP_CLOSE:
                raise ConnectionClosed()
            if opcode == OP_PING:
                with self.lock:
                    self.sock.sendall(ws_frame(data, OP_PONG, mask=True))
                continue
            if opcode == OP_PONG:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts)

    def recv(self):
        """回傳下一則 JSON 訊息 (dict)；略過格式錯誤的訊息。"""
        while True:
            raw = self.recv_raw()
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(msg, dict):
                return msg

    def settimeout(self, t):
        self.sock.settimeout(t)

    def shutdown(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self):
        try:
            with self.lock:
                self.sock.sendall(ws_frame(struct.pack("!H", 1000), OP_CLOSE, mask=True))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def ssl_context():
    ctx = ssl.create_default_context()
    try:                                       # 有安裝 certifi 就用它的憑證 (macOS 常缺憑證)
        import certifi
        ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    return ctx


def open_connection(server, timeout=10):
    """連到伺服器並完成 WebSocket 握手，回傳 Conn。"""
    scheme, host, port, path = parse_server(server)
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if scheme == "wss":
            try:
                sock = ssl_context().wrap_socket(sock, server_hostname=host)
            except ssl.SSLCertVerificationError as e:
                raise OSError("SSL 憑證驗證失敗。macOS 請執行「應用程式 > Python 3.x > "
                              "Install Certificates.command」後再試一次") from e
        key = base64.b64encode(os.urandom(16)).decode()
        host_hdr = host if port in (80, 443) else f"{host}:{port}"
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host_hdr}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                      f"Sec-WebSocket-Version: 13\r\nUser-Agent: collab-whiteboard\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("伺服器在 WebSocket 握手時關閉連線")
            buf += chunk
            if len(buf) > 65536:
                raise OSError("伺服器回應的 header 太長")
        head, buf = buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ")
        if len(status) < 2 or status[1] != "101":
            raise OSError(f"伺服器拒絕 WebSocket 連線 ({lines[0]})，網址可能不對")
        headers = {k.strip().lower(): v.strip()
                   for k, _, v in (line.partition(":") for line in lines[1:])}
        if headers.get("sec-websocket-accept") != ws_accept_key(key):
            raise OSError("WebSocket 握手驗證失敗")
        return Conn(sock, buf)
    except BaseException:
        sock.close()
        raise


def request(server, timeout=10, **obj):
    """送出一則訊息並等第一則回覆 (用於 create_room / room_info)。"""
    conn = open_connection(server, timeout)
    try:
        conn.settimeout(timeout)
        conn.send(**obj)
        return conn.recv()
    except (socket.timeout, ConnectionClosed) as e:
        raise OSError(f"伺服器沒有回應 ({e or '連線中斷'})") from e
    finally:
        conn.close()
