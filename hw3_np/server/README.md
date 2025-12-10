```text
server/
├─ config.json                     # 端點設定（只需 host）
├─ server/
│  ├─ main.py                      # 啟動器：分別啟動 DeveloperServer / LobbyServer
│  ├─ runtime_ports.json           # 啟動後自動填入 developer_port / lobby_port
│  ├─ common/
│  │  ├─ db.py                     # Data/DB 模組（JSON 永續化、thread-safe）
│  │  └─ auth.py                   # Token / Session 管理（開發者/玩家分流）
│  ├─ dev_server.py                # Developer Server（上傳/更新/下架/我的遊戲/登入註冊）
│  ├─ lobby_server.py              # Lobby Server（商城列表/詳細/下載、房間建立/加入/離開、登入註冊）
│  ├─ data/                        # 永續資料（Server 重啟後不遺失）
│  │  ├─ games.json
│  │  ├─ dev_users.json
│  │  ├─ player_users.json
│  │  ├─ rooms.json
│  │  └─ tokens.json
│  └─ uploaded_games/              # 上架遊戲實體檔（依名字/版本展開）
```
