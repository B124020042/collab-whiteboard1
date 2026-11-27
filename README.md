# 協作白板 Collaborative Whiteboard

企業資訊通訊期末專案：多人透過網路在同一張畫布上即時畫圖。
建立房間取得房間號，其他人輸入房間號就能加入。在哪裡、用什麼網路都可以。

伺服器部署在雲端 (Render)，客戶端是 tkinter 視窗程式。全部只用 Python 標準函式庫，WebSocket 協定也是自己實作的。

## 功能

- **即時同步**：任何人畫的東西會立刻出現在同房間所有人的畫面上，後加入的人也會看到完整畫面
- **房間**：建立房間取得 5 碼房間號 (例如 `K7Q2M`)，可一鍵複製；各房間畫布互相獨立
- **畫筆與橡皮擦**：8 色調色盤＋自訂顏色、粗細調整
- **文字框**：可選顏色與字級，可修改
- **選取**：點選、Shift 加選、框選、全選，拖曳移動，拉控制點縮放 (筆跡粗細與字級一起調整)
- **右鍵選單**：複製、剪下、貼上 (貼在按右鍵的位置)、刪除、編輯文字
- **剪貼簿**：可在不同視窗、不同人之間複製貼上；從其他程式複製的文字貼上後會變成文字框
- **復原 (Ctrl+Z)**：移除自己最後新增的物件，不會動到別人的
- **儲存 / 開啟**：存成 `.json` 白板檔，開啟後同步給房間內所有人；也可另存 `.ps` 圖檔
- **他人游標與線上名單**：即時顯示其他人的滑鼠位置與暱稱
- **斷線自動重連**；伺服器重啟後，還開著白板的人會自動把畫面傳回去復原

## 使用方式

### 下載執行檔 (不需要安裝 Python)

到 GitHub 的 [Releases → 最新版](../../releases/tag/latest) 下載：

| 系統 | 檔案 | 開啟方式 |
|---|---|---|
| Windows | `CollabWhiteboard-Windows.exe` | 雙擊執行。若出現「Windows 已保護您的電腦」，按「其他資訊 → 仍要執行」 |
| macOS (Apple 晶片) | `CollabWhiteboard-macOS.zip` | 解壓縮後把 `CollabWhiteboard.app` 拖到「應用程式」。第一次開啟若被擋，到「系統設定 → 隱私權與安全性」按「強制打開」，或在終端機執行 `xattr -cr /Applications/CollabWhiteboard.app` |

執行檔沒有經過數位簽章，所以系統第一次會跳出警告，照上表操作即可。

### 用 Python 執行

需要 Python 3.8 以上。tkinter 通常已內建；若出現 `No module named 'tkinter'`：
Ubuntu/Debian 執行 `sudo apt install python3-tk`，macOS (Homebrew) 執行 `brew install python-tk`。

```bash
python3 start.py
```

### 開始畫

1. 打開程式會跳出小視窗，輸入暱稱。
2. **第一個人**按「建立房間」，白板上方會顯示房間號，按「複製房間號」傳給其他人。
3. **其他人**輸入房間號，按「加入房間」。房間號不分大小寫，打成 O / I / L 會自動當成 0 / 1 / 1；打錯或找不到房間時會直接說明。

也可以用指令：

```bash
python3 start.py create --name amy        # 建立房間
python3 start.py join K7Q2M --name bob    # 加入房間
python3 start.py join K7Q2M --check       # 只檢查房間在不在
python3 bot.py K7Q2M --count 5            # 讓機器人到房間裡畫圖 (單人 demo 用)
```

### 操作說明

| 工具 / 動作 | 操作 |
|------|------|
| 畫筆 `P`、橡皮擦 `E` | 按住左鍵拖曳 |
| 文字 `T` | 點畫布開始輸入；`Enter` 完成、`Shift+Enter` 換行、`Esc` 取消；點既有文字可修改，清空內容即刪除 |
| 選取 `V` | 點選物件；`Shift`+點擊加選 / 取消；從空白處拖曳框選；拖曳已選物件可移動；雙擊文字可修改 |
| 縮放 | 拖曳選取框的控制點：四角等比例 (按住 `Shift` 自由縮放)，上下左右四邊只改高或寬 |
| 右鍵選單 | 複製、剪下、貼上、刪除、編輯文字、全選、復原；在物件上按右鍵會自動選取它 (macOS 也可用 Control+點擊) |
| 快捷鍵 | 複製 / 剪下 / 貼上 `Ctrl+C` / `Ctrl+X` / `Ctrl+V`，刪除 `Delete`，全選 `Ctrl+A`，取消選取 `Esc`，復原 `Ctrl+Z`，開啟 `Ctrl+O`，儲存 `Ctrl+S` (macOS 用 `Cmd`) |

正在輸入文字時，快捷鍵會交給文字框處理 (例如 `Ctrl+C` 是複製文字框裡的字)。

### 免費伺服器的限制

- 15 分鐘沒有任何人使用，伺服器會休眠；下一次連線要等約 1 分鐘喚醒 (視窗會顯示提示)。只要還有人開著白板，程式每 25 秒送一次心跳，就不會休眠。
- 伺服器休眠或重新部署後，記憶體裡的房間會清空。還開著白板的人重新連上時，會自動把畫面傳回去復原；如果大家都關掉了，房間號就失效，要重新建立。想保留畫面請先「儲存」成 `.json`。

### 常見問題

- **「這台電腦還沒設定線上伺服器網址」**：`config.json` 是空的，執行 `git pull` 取得最新版。
- **macOS「SSL 憑證驗證失敗」** (用 Python 執行時)：執行「應用程式 → Python 3.x → Install Certificates.command」。
- **想換伺服器**：修改 `config.json`；執行檔的話，在執行檔旁邊放一個 `config.json` 就會優先使用。

## 部署伺服器 (只要做一次)

伺服器部署在 [Render](https://render.com) 免費方案 (不用信用卡)：

1. 用 GitHub 帳號登入 Render，點 **New + → Blueprint**，選這個 repo，按 **Apply**。設定都在 `render.yaml`。
2. 部署完成後會有網址，例如 `https://collab-whiteboard-xxxx.onrender.com`。用瀏覽器打開看到 `collab-whiteboard server OK` 就成功了。
3. 把網址的 `https://` 改成 `wss://` 填進 `config.json`，commit 並 push：

   ```json
   { "server": "wss://collab-whiteboard-xxxx.onrender.com/" }
   ```

之後每次 push 到 GitHub：Render 會自動重新部署伺服器，GitHub Actions 會重新打包執行檔並更新 Releases 的「最新版」。

## 架構

```
┌──────────── client.py ─────────────┐             ┌──────── server.py ─────────┐
│ 主執行緒 (tkinter)                  │             │ asyncio 事件迴圈 (單執行緒)  │
│  滑鼠事件 → 本機先畫 → 累積點座標    │─ WebSocket ─▶│  rooms: {房間號: Room}      │
│  每 30ms 批次送出 draw              │   (wss)     │   ├ users   線上使用者       │
│  每 15ms 從 queue 取訊息更新畫面 ◀─┐ │             │   └ strokes 畫布物件 (有序)  │
│ 網路執行緒 (Net)                   │ │◀────────────│  轉發給同房間其他人          │
│  連線 / 自動重連 / 讀取 → queue ───┘ │             │  新加入者 → 完整畫面         │
└─────────────────────────────────────┘             └────────────────────────────┘
```

### 通訊協定

每則訊息是一個 UTF-8 JSON 物件，走 WebSocket，一則訊息一個 text frame。WebSocket 本身就有訊息邊界，所以不需要像直接用 TCP 那樣自己處理「黏包」。完整定義見 `protocol.py`。

| 方向 | type | 內容 |
|------|------|------|
| C→S | `create_room` / `room_info` | 建立房間 (回覆隨機房間號) / 查詢房間是否存在 |
| C→S | `hello` | 暱稱、房間號 |
| C→S | `draw` | 筆跡 id、顏色、粗細、一批點座標 |
| C→S | `text` | 新增 / 修改文字框 (位置、內容、顏色、字級) |
| C→S | `move` / `scale` / `delete` | 移動 / 縮放 / 刪除多個物件 |
| C→S | `cursor` / `undo` / `clear` | 游標位置 / 復原 / 清除畫布 |
| C→S | `restore` / `heartbeat` | 伺服器重啟後復原畫面 / 心跳 |
| S→C | `welcome` | 最終暱稱、線上名單、目前所有物件 |
| S→C | `draw` / `text` / `move` / `scale` / `cursor` | 轉發他人的操作 |
| S→C | `remove` / `clear` / `reset` | 移除物件 / 清空 / 整個畫面被復原 |
| S→C | `user_join` / `user_leave` | 使用者進出 |

### 設計重點

1. **WebSocket 自己實作**：雲端平台只轉發 HTTP / WebSocket。`transport.py` 用標準函式庫實作 WebSocket (RFC 6455) 的握手 (`Sec-WebSocket-Accept` 雜湊)、frame 編解碼 (遮罩、分段、ping/pong、2 / 8 byte 長度欄位)。伺服器收到一般 HTTP 請求時回傳狀態頁，給雲端平台做健康檢查。
2. **比例座標**：座標一律用 0~1 傳送，各客戶端依自己的視窗大小換算，視窗縮放後也會重繪。
3. **批次傳送**：滑鼠移動事件每秒可達上百次。先在本機立即畫出 (零延遲手感)，點座標累積後每 30ms 送一批，封包數大幅下降。每批會多帶前一批的最後一點，讓線段連續。
4. **游標節流**：游標位置最多每 50ms 送一次；它是「最新值才重要」的資料，丟掉舊值不影響結果。
5. **GUI 執行緒安全**：tkinter 只能在主執行緒操作，所以網路執行緒只把訊息放進 `queue.Queue`，主執行緒用 `root.after()` 定時取出處理。
6. **一致性**：伺服器接收順序就是最終順序；客戶端每次 (重新) 連線都以伺服器的 `welcome` 完整狀態為準。
7. **物件模型**：畫布上的筆跡與文字都是「物件」，共用同一組 id。任何人都可移動、縮放、刪除、編輯文字，但只能接續自己的筆跡、復原自己新增的物件。
8. **移動與縮放同步**：拖曳時只在本機更新畫面，放開滑鼠才送出一次 `move` (位移量) 或 `scale` (基準點 + 倍率)，封包很小。邊界限制 (不能移出畫布、不能翻轉) 與座標取 4 位小數都由 `protocol.py` 的共用函式計算，伺服器與每台電腦的結果完全一致。
9. **剪貼簿與存檔**：複製時把物件以存檔格式 (JSON) 放進系統剪貼簿，所以能貼到另一個視窗。`.json` 存的是物件資料而不是像素，檔案小、縮放不失真，而且能重新載入並同步給所有人。
10. **房間號**：用 `secrets` 產生 5 碼隨機房間號 (Crockford Base32，不含易混淆的 I L O U，約 3300 萬種組合)；加入前先用 `room_info` 確認房間存在，打錯不會跑進空房間。
11. **防休眠與復原**：客戶端每 25 秒送心跳，讓雲端平台知道還有人在用。伺服器重啟後房間是空的，第一個重連的客戶端送出 `restore` (保留原本的物件 id 與作者)，伺服器廣播 `reset` 給所有人；之後的 `restore` 會被忽略，所以不會重複。
12. **防呆**：伺服器檢查顏色格式、粗細與字級範圍、座標範圍、訊息大小，壞掉的資料直接忽略，不會讓伺服器崩潰。每個房間最多保存 5000 個物件。

## 開發與測試

```bash
python3 server.py                              # 在本機開伺服器 (port 9100)
python3 start.py --server ws://127.0.0.1:9100  # 連本機伺服器

# 自動化測試 (不需要螢幕)
python3 -m unittest discover -s tests       # 全部 43 個測試
python3 tests/test_server.py   # 伺服器：同步、權限、防呆、存檔格式
python3 tests/test_client.py   # 客戶端與啟動視窗 (用假的 tkinter)、伺服器重啟復原
python3 tests/test_start.py    # 建立 / 加入房間
python3 tests/test_online.py   # WebSocket frame、HTTP 狀態頁、房間號

# 打包執行檔 (在要打包的系統上執行；GitHub Actions 會自動做)
pip install pyinstaller certifi
python3 build.py           # 產生 release/CollabWhiteboard-Windows.exe 或 -macOS.zip
```

## 檔案

| 檔案 | 說明 |
|------|------|
| `start.py` | 程式進入點：啟動視窗 (建立房間 / 輸入房間號加入) |
| `client.py` | 白板視窗 (tkinter) |
| `server.py` | 伺服器 (部署在 Render) |
| `transport.py` | WebSocket 傳輸層 (標準函式庫實作) |
| `protocol.py` | 共用協定、資料驗證、移動 / 縮放計算 |
| `config.json` | 伺服器網址 |
| `bot.py` | 自動畫圖的 demo 機器人 |
| `build.py`、`.github/workflows/build.yml` | 打包執行檔、自動發布到 Releases |
| `render.yaml`、`requirements.txt` | Render 部署設定 |
| `tests/` | 自動化測試 |

## 可延伸方向

- 形狀工具 (矩形、圓形、箭頭)、改變已選物件的顏色、側邊聊天室
- 伺服器端存檔 (資料庫)，伺服器重啟後畫布仍在
- 瀏覽器版客戶端 (伺服器已支援 WebSocket，手機也能畫)
