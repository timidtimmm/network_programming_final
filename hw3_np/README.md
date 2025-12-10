# HW3 — Game Store System

本專案實作了一個整合「遊戲大廳（Lobby）」與「遊戲商城（Store）」的平台，支援：

- **開發者**：上架 / 更新 / 下架自己的遊戲
- **玩家**：註冊登入、瀏覽商城、下載 / 更新遊戲、建立 / 加入房間、遊戲結束後評分與留言
- **多版本管理**（semantic version: `major.minor.patch`）與「只允許最新版本建房」
- **Menu-driven 介面**（所有前台程式皆**不需 CLI 參數**）
- **JSON** 檔作為持久化資料庫
- **動態 Port**（避免硬編 Port 衝突）
- **支援 CLI 遊戲**（RPS）與 **GUI 遊戲**（Tetris，使用 pygame）
- **依賴**：僅 Python 標準函式庫與 pygame（僅用於 Tetris GUI）

---

## 1. 專案結構

```text
專案根目錄/
├── config.json                 # 全域設定檔
├── server/
│   ├── main.py                 # 啟動 Developer 與 Lobby Server
│   ├── dev_server.py           # 開發者後端（上架 / 更新 / 下架 / 登入註冊）
│   ├── lobby_server.py         # 玩家後端（商城、下載、房間、SSE 房間更新）
│   ├── runtime_ports.json      # 動態產生（主程式啟動後）
│   ├── data/                   # 伺服器端資料庫 JSON
│   │   ├── dev_users.json
│   │   ├── player_users.json
│   │   ├── games.json
│   │   ├── rooms.json
│   │   └── tokens.json
│   └── uploaded_games/         # Server 端遊戲實體
├── common/
│   ├── db.py                   # Thread-safe JSON DB
│   └── auth.py                 # Token / Session 管理
├── developer/
│   ├── developer_client.py     # 開發者前台主程式
│   └── games/                  # 開發中的遊戲原始碼
├── player/
│   ├── lobby_client.py         # 玩家前台主程式
│   └── downloads/              # 玩家下載遊戲副本（依玩家 ID 分目錄）
└── sample_games/               # 範例遊戲（例如 CLI 版 rps）
```

**config.json** 主要欄位：

- `server_host`：Server 綁定的 host（通常 `0.0.0.0`）
- `developer_endpoint.host` / `lobby_endpoint.host`：預設對外 host
- `data_dir`：Server 資料儲存目錄（如 `server/data` 或 `server/storage`）
- `public_host`：房間對外 IP（140.113.17.11 到 140.113.17.14）

---

## 2. 環境需求

- **Python 3.10+**
- **套件需求**
  - GUI Tetris 需安裝 `pygame`
  - 其餘皆為 Python 標準函式庫

### 安裝方式（建議虛擬環境）

```sh
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows
# .venv\Scripts\activate

pip install pygame
```

---

## 3. Demo 流程（操作步驟）

### Step 1. 啟動 Server

```sh
python server/main.py
```

- 自動啟動 Developer Server + Lobby Server（各自綁定動態 Port）
- 建立 / 更新 `server/runtime_ports.json`
- 偵測公網 / 內網 IP（供跨機 Demo 用）

### Step 2. 啟動開發者 Client

```sh
python developer/developer_client.py
```

數字選單操作：

- 開發者註冊 / 登入
- 上傳新遊戲
- 上傳新版本（檢查 semantic versioning 格式，嚴格比大小）
- 下架遊戲
- 檢視我的遊戲與所有版本

### Step 3. 啟動玩家 Client

```sh
python player/lobby_client.py
```

數字選單操作：

- 玩家註冊 / 登入
- 瀏覽商城遊戲列表、查看遊戲詳細資訊 / 版本 / 評價
- 下載 / 更新遊戲（下載到 `player/downloads/{player_id}/{game}/{version}/`）
- 建立房間（只用最新版本）
- 加入房間（版本需一致）
- 遊戲結束自動回 Lobby，可評分留言
- 所有人離開房間後，Server 會自動釋放房間與 Port

### Step 4. 遊戲示範

內建遊戲：

- `rps`：黑白ㄘㄟˋ（CLI）
- `tetris`：俄羅斯方塊（GUI, pygame）
- `threeplayer_rps`：三人剪刀石頭布（CLI）

示範流程：

1. 開發者端登入 → 上傳 / 更新遊戲
2. 兩名玩家註冊登入 → 下載遊戲
3. 一人建房，一人加入 → 進行遊戲
4. 遊戲結束自動回 Lobby，可評分留言
5. 檢查 `server/data/games.json`、`rooms.json`、`player/downloads/` 變化

---

## 4. 資料儲存與版本管理

### JSON DB

所有狀態儲存於 `server/data/` 下：

- `dev_users.json` / `player_users.json`：帳號資料
- `games.json`：遊戲 metadata（作者、描述、所有版本、最新版本、評價）
- `rooms.json`：運作中的房間
- `tokens.json`：登入 token 與有效期限

Server 重啟時資料不會遺失（除非手動刪除 JSON）。

### Game Version（版本號）

- 格式：`major.minor.patch`（如 `1.0.13`）
- 開發者上傳新版本會檢查：
  - 格式是否合法
  - 是否「嚴格大於」現有版本
- Lobby 端：
  - 建房一律用 `games.json` 標註的最新版本
  - 加入房間必須本機版本與房間一致

---

## 5. 規格對照與系統設計重點

| 功能 | 實作內容說明 |
| --- | --- |
| 開發者平台 | 註冊 / 登入 / 上架 / 上傳新版本 / 下架 / 查詢版本 |
| 玩家平台 | 註冊 / 登入 / 商城列表 / 詳細頁 / 下載 / 建房 / 加房 / 離房 / 評價留言 |
| 系統設計 | 全 menu-driven、不用 CLI 參數、動態 Port、自動提示 |
| 版本一致性 | 上傳需 X.Y.Z，建房與加入房間都檢查版本 |
| 下架處理 | 遊戲下架後不可下載、不可建房，UI 會顯示並導回菜單 |

---

## 6. 重置與除錯小提示

### 重置系統狀態

1. 關閉所有 Server 與前台程式
2. 刪除 `server/data/*.json`
3. 清空 `server/uploaded_games/*` 與 `player/downloads/*`
4. 重新執行：

```sh
python server/main.py
```

### Port 被占用 / 連線異常

- 確認無殘留 Python process
- 刪除 `server/runtime_ports.json`
- 重新啟動 `server/main.py`

### Tetris GUI 啟動不了

- 檢查是否已安裝 `pygame`
- Windows 請用 `py` 執行避免 Python 版本搞混

---

