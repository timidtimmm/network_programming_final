# player/lobby_client.py - 最終交作業版（自動判斷連線目標 + SSE 房間 UI）

import os, sys, json, asyncio, base64, zipfile, io, shutil, subprocess, socket, signal
from pathlib import Path

# ✅ downloads 放在 player/ 資料夾內
ROOT = Path(__file__).resolve().parents[1]   # 專案根目錄
PLAYER_DIR = Path(__file__).resolve().parent # player/ 資料夾
CONFIG = json.load(open(ROOT / "config.json", "r", encoding="utf-8"))

# Demo 時給助教設定用：
# - 若 config.json["server_ip"] 有填 → 一律連到該 IP + 固定 port
# - 若沒填 → 本機開發模式，使用 runtime_ports.json 自動偵測
SERVER_IP = CONFIG.get("server_ip") or ""

runtime_path = ROOT / "server" / "runtime_ports.json"
SERVER_RUNTIME = {}
if runtime_path.exists():
    SERVER_RUNTIME = json.load(open(runtime_path, "r", encoding="utf-8"))

def _pick_target(endpoint_key: str, rt_host_key: str, rt_port_key: str, default_port: int,
                 env_host_key: str, env_port_key: str):
    endpoint_cfg = CONFIG.get(endpoint_key, {})

    # 0) ✅ ENV 覆蓋（最高優先）
    env_host = os.getenv(env_host_key)
    env_port = os.getenv(env_port_key)
    if env_host and env_port:
        try:
            return env_host, int(env_port)
        except:
            pass
    elif env_host and not env_port:
        # 只給 host 時：port 先吃 runtime / config / default
        if SERVER_RUNTIME:
            port = SERVER_RUNTIME.get(rt_port_key) or endpoint_cfg.get("port", default_port)
        else:
            port = endpoint_cfg.get("port", default_port)
        return env_host, port
    elif env_port and not env_host:
        # 只給 port 時：host 走後續決策（不建議但容錯）
        try:
            forced_port = int(env_port)
        except:
            forced_port = default_port
    else:
        forced_port = None

    # 1) ⭐ 有 runtime → 以 runtime port 為主
    if SERVER_RUNTIME:
        port = forced_port or SERVER_RUNTIME.get(rt_port_key) or endpoint_cfg.get("port", default_port)

        # 1a) 若有 server_ip（遠端 demo），只覆蓋 host
        if SERVER_IP:
            return SERVER_IP, port

        # 1b) 本機開發：先試 127.0.0.1
        try:
            s = socket.socket()
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            s.close()
            return "127.0.0.1", port
        except OSError:
            host = SERVER_RUNTIME.get(rt_host_key) or endpoint_cfg.get("host", "127.0.0.1")
            # 避免 client 連 0.0.0.0
            if host == "0.0.0.0":
                host = "127.0.0.1"
            return host, port

    # 2) 沒 runtime 才走 server_ip + 固定 port
    if SERVER_IP:
        host = SERVER_IP
        port = forced_port or endpoint_cfg.get("port", default_port)
        return host, port

    # 3) 最底線：只有 config endpoint
    host = endpoint_cfg.get("host", "127.0.0.1")
    port = forced_port or endpoint_cfg.get("port", default_port)

    # ✅ client 不能連 0.0.0.0 → 用 public_hosts[0] 或 127.0.0.1
    if host == "0.0.0.0":
        pubs = CONFIG.get("public_hosts") or []
        host = pubs[0] if pubs else "127.0.0.1"

    return host, port



# ✅ 這兩個就是 Lobby / Dev 實際連線使用的 host/port
LOBBY_HOST, LOBBY_PORT = _pick_target(
    "lobby_endpoint", "lobby_host", "lobby_port", 5502,
    "LOBBY_CONNECT_HOST", "LOBBY_CONNECT_PORT"
)
DEV_HOST, DEV_PORT = _pick_target(
    "developer_endpoint", "dev_host", "developer_port", 5501,
    "DEV_CONNECT_HOST", "DEV_CONNECT_PORT"
)

def remote_logout(lobby_host, lobby_port, token):
    if not token:
        return
    try:
        s = socket.socket()
        s.settimeout(1.5)
        s.connect((lobby_host, lobby_port))
        s.sendall((json.dumps({
            "kind": "logout",
            "token": token
        }, ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            s.recv(4096)
        except:
            pass
        s.close()
        print("[LobbyClient] token released by Ctrl+C")
    except Exception:
        # 不要 raise，避免 Ctrl+C 卡死
        pass

def install_sigint_handler(get_lobby_host, get_lobby_port, get_token):
    def handler(sig, frame):
        remote_logout(get_lobby_host(), get_lobby_port(), get_token())
        print("\n[LobbyClient] bye")
        sys.exit(0)

    signal.signal(signal.SIGINT, handler)
    # SIGTERM 在 Windows 不一定會有用，但加了也不會壞
    try:
        signal.signal(signal.SIGTERM, handler)
    except Exception:
        pass

# ✅ 下載目錄改為 player/downloads
DOWNLOADS_ROOT = PLAYER_DIR / "downloads"
DOWNLOADS_ROOT.mkdir(parents=True, exist_ok=True)

def has_local_game_version(player_name: str, game: str, version: str) -> bool:
    p = DOWNLOADS_ROOT / player_name / game / version / "start_client.py"
    return p.exists()

async def send_req(payload):
    """發送請求並接收回應（Lobby Server）"""
    try:
        reader, writer = await asyncio.open_connection(LOBBY_HOST, LOBBY_PORT)
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        writer.write(line.encode("utf-8"))
        await writer.drain()
        
        data = await reader.readline()
        writer.close()
        await writer.wait_closed()
        
        return json.loads(data.decode("utf-8"))
    except ConnectionRefusedError:
        return {"ok": False, "error": "無法連線到大廳伺服器"}
    except Exception as e:
        return {"ok": False, "error": f"連線錯誤：{e}"}

def safe_extract_zip(b: bytes, dest: Path):
    with zipfile.ZipFile(io.BytesIO(b), "r") as z:
        z.extractall(dest)

def get_local_client_dir(player, game, version):
    base = DOWNLOADS_ROOT / player / game / version
    if (base / "manifest.json").exists():
        return base
    return None

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")

def ask_choice(prompt: str, valid: set[str]) -> str:
    while True:
        c = input(prompt).strip()
        if c in valid:
            return c
        print("無效的指令，請輸入：", "/".join(sorted(valid)))

async def fetch_playable_games(token=None):
    resp = await send_req({"kind":"list_games","token":token} if token else {"kind":"list_games"})
    if not resp.get("ok"):
        print(resp); return {}
    return resp.get("games", {})

def print_game_menu(games: dict):
    if not games:
        print("【目前無上架遊戲】"); return []
    items = sorted(games.items())
    print("\n# 可玩遊戲（伺服器上架）")
    for i,(name,info) in enumerate(items,1):
        versions = info.get("versions", [])
        latest = info.get("latest", "-")
        author = info.get("author", "未知")
        display_name = info.get("display_name", name)
        avg = info.get("avg_rating")
        cnt = info.get("review_count", 0)
        rating_str = f"{avg} 分／{cnt} 則" if (avg is not None and cnt > 0) else "尚無評分"
        print(f"{i:>2}) {display_name} ({name})  作者: {author}  "
              f"最新版: {latest}  共 {len(versions)} 版  評分: {rating_str}")
    return items

async def fetch_rooms(token=None):
    resp = await send_req({"kind":"list_rooms","token":token} if token else {"kind":"list_rooms"})
    if not resp.get("ok"):
        print(resp); return {}
    return resp.get("rooms", {})

def print_room_menu(rooms: dict):
    if not rooms:
        print("【目前沒有房間】")
        return []
    items = sorted(rooms.items())
    print("\n# 房間列表")
    for i,(rid,r) in enumerate(items,1):
        players = r.get("players", [])
        ready_players = r.get("ready_players", [])
        max_players = r.get("max_players", "?")
        status = r.get("status","?")
        print(f"{i:>2}) {rid}")
        print(f"     遊戲: {r['game']}@{r['version']}")
        print(f"     位址: {r['host']}:{r['port']}")
        print(f"     狀態: {status}  人數: {len(players)}/{max_players}")
        print(f"     玩家: {', '.join(players)}")
        print(f"     已就緒: {', '.join(ready_players) if ready_players else '無'}")
    return items

class AsyncRoomUI:
    """即時更新的房間介面"""
    def __init__(self, token, player, room_id, join_info):
        self.token = token
        self.player = player
        self.room_id = room_id
        self.join_info = join_info
        self.room_info = None
        self.player_ready = False
        self.running = True
        self.game_started = False
        self.reader = None
        self.writer = None
        self.last_start_state = None   # ⭐ 新增：記住上一個 start.state
        
    async def connect_stream(self):
        """建立持續連線以接收即時更新"""
        self.reader, self.writer = await asyncio.open_connection(LOBBY_HOST, LOBBY_PORT)
        line = json.dumps({"kind": "subscribe_room", "token": self.token, "room_id": self.room_id}) + "\n"
        self.writer.write(line.encode("utf-8"))
        await self.writer.drain()
        
        # 讀取初始確認訊息
        data = await self.reader.readline()
        resp = json.loads(data.decode("utf-8"))
        if not resp.get("ok"):
            print(f"訂閱失敗：{resp.get('error')}")
            return False

        # ✅ 這裡把 room 一次性塞進來，避免卡在「等待房間資料」
        if "room" in resp:
            self.room_info = resp["room"]
            self.display()

        return True

    
    async def update_loop(self):
        """接收伺服器推送的房間更新"""
        try:
            while self.running:
                data = await self.reader.readline()
                if not data:
                    break
                
                msg = json.loads(data.decode("utf-8"))
                if msg.get("event") == "room_update":
                    # 先記錄舊的 start.state
                    prev_state = self.last_start_state

                    # 更新最新 room 狀態
                    self.room_info = msg.get("room")
                    self.last_start_state = (self.room_info or {}).get("start", {}).get("state")

                    self.display()

                    # 如果房間回到 waiting，而且沒有 start 提案，就把本地 ready 狀態也視為「未就緒」
                    status = (self.room_info or {}).get("status")
                    start_state = (self.room_info or {}).get("start", {}).get("state")
                    if status == "waiting" and start_state in (None, "idle"):
                        self.game_started = False
                        # 讓本地標記跟 server 同步，以顯示成「像剛進來」的狀態
                        self.player_ready = (
                            self.player in (self.room_info or {}).get("ready_players", [])
                        )
                    if not self.running:
                        break

                    # 檢查是否可以啟動遊戲（只在由「非 agreed」→「agreed」那一刻觸發）
                    if self.should_auto_start(prev_state, self.last_start_state):
                        print("\n🎮 【所有玩家就緒！自動啟動遊戲...】")
                        await asyncio.sleep(1)
                        await self.start_game()

        except Exception as e:
            if self.running:
                print(f"\n[更新錯誤] {e}")
    
    def should_auto_start(self, prev_state, curr_state) -> bool:
        """檢查是否應該自動啟動遊戲（只在 state 從非 agreed → agreed 時啟動一次）"""
        if not self.room_info or self.game_started:
            return False

        return (
            prev_state != "agreed"
            and curr_state == "agreed"
            and self.player in (self.room_info or {}).get("players", [])
        )

    async def start_game(self):
        """啟動遊戲客戶端"""
        if self.game_started:
            return

        self.game_started = True

        # 檢查是否已下載遊戲
        if not has_local_game_version(self.player, self.join_info["game"], self.join_info["version"]):
            print("❌ 請先去商城下載最新版遊戲")
            self.game_started = False
            return

        client_dir = get_local_client_dir(self.player, self.join_info["game"], self.join_info["version"])
        manifest = json.load(open(client_dir / "manifest.json", "r", encoding="utf-8"))
        entry = manifest.get("entry_client", "start_client.py")

        env = os.environ.copy()
        env.update({
            "GAME_HOST": self.join_info["host"],
            "GAME_PORT": str(self.join_info["port"]),
            "ROOM_ID": self.room_id,
            "GAME_NAME": self.join_info["game"],
            "GAME_VERSION": self.join_info["version"],
            "PLAYER_USERNAME": self.player,
            "PLAYER_NAME": self.player
        })

        print(f"\n🎮 正在啟動遊戲客戶端：{entry}")

        # ✅ 修正：根據環境選擇不同啟動方式
        if os.name == "nt":
            # Windows: 開新 console 視窗
            print("【注意】遊戲將在新視窗中執行")
            subprocess.Popen(
                [sys.executable, entry],
                cwd=str(client_dir),
                env=env,
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
        else:
            # Linux/Unix: 直接在當前終端執行
            print("【注意】遊戲將在當前終端執行")
            print("提示：可以先用 tmux 或 screen 來管理多個視窗\n")

            # 選項 A: 在當前終端執行（簡單）
            subprocess.Popen(
                [sys.executable, entry],
                cwd=str(client_dir),
                env=env
            )

            # 選項 B: 如果系統有 tmux（需先檢查）
            # if shutil.which("tmux"):
            #     subprocess.Popen(
            #         ["tmux", "new-window", "-n", "Game", sys.executable, entry],
            #         cwd=str(client_dir),
            #         env=env
            #     )
            # else:
            #     subprocess.Popen([sys.executable, entry], cwd=str(client_dir), env=env)

        print("✓ 遊戲客戶端已啟動")

        await asyncio.sleep(1)
        self.game_started = False

        if self.room_info is not None:
            self.room_info["start"] = {"state": "idle"}
            self.room_info["status"] = "waiting"
            self.player_ready = False
            self.display()
    
    def display(self):
        """顯示當前房間狀態"""
        clear_screen()
        print(f"=== 房間 {self.room_id} ===")
        print(f"(Lobby Server: {LOBBY_HOST}:{LOBBY_PORT})")
        print(f"遊戲：{self.join_info['game']}@{self.join_info['version']}")
        print(f"位址：{self.join_info['host']}:{self.join_info['port']}")

        if self.room_info:
            players = self.room_info.get("players", [])
            status = self.room_info.get("status", "?")
            ready_players = self.room_info.get("ready_players", [])
            max_players = self.room_info.get("max_players", "?")

            if self.player not in players:
                print("\n[系統] 遊戲結束，房間已關閉，按下Enter返回大廳...")
                self.running = False
                return

            print(f"\n【房間狀態】: {status}")
            print(f"【玩家列表】: {len(players)}/{max_players} 人")
            for p in players:
                ready_mark = "✓" if p in ready_players else "✗"
                you_mark = " (你)" if p == self.player else ""
                print(f"  {ready_mark} {p}{you_mark}")

            start = (self.room_info or {}).get("start", {"state":"idle"})
            owner = (self.room_info or {}).get("owner")
            is_owner = (owner == self.player)
            status = self.room_info.get("status", "waiting")

            if status in ("waiting", "ready"):
                if start.get("state") == "idle":
                    if is_owner:
                        print("\n👉 你是房主：按 [s] 提議開始對局")
                    else:
                        print("\n等待房主提議開始…")
                
                elif start.get("state") == "proposed":
                    responses = start.get("responses", {})
                    guests = [p for p in players if p != owner]
                    
                    if is_owner:
                        # ✅ 房主視角：顯示每個房客的回應狀態
                        print("\n⌛ 已送出開始提議，等待房客回覆：")
                        for guest in guests:
                            if responses.get(guest):
                                status_icon = "✅"
                                status_text = "已同意"
                            else:
                                status_icon = "⏳"
                                status_text = "尚未回應"
                            print(f"   {status_icon} {guest}: {status_text}")
                    else:
                        # ✅ 房客視角：根據自己是否已回應顯示不同訊息
                        if responses.get(self.player):
                            # 自己已同意
                            not_responded = [g for g in guests if not responses.get(g, False) and g != self.player]
                            if not_responded:
                                print(f"\n✅ 你已同意，等待其他玩家：{', '.join(not_responded)}")
                            else:
                                print("\n✅ 所有人都已同意，即將開始...")
                        else:
                            # 自己還沒回應
                            print("\n❓ 房主想開始對局：同意請按 [y]，拒絕按 [n]")
                
                elif start.get("state") == "rejected":
                    rejected_by = start.get("rejected_by")
                    
                    if is_owner:
                        # ✅ 房主視角：顯示誰拒絕了
                        if rejected_by:
                            print(f"\n⚠️ {rejected_by} 拒絕了開始提議")
                        else:
                            print("\n⚠️ 房客已拒絕，請稍後再提議或聊天協調")
                        print("👉 你是房主：按 [s] 提議開始對局")
                    else:
                        # ✅ 房客視角：根據是不是自己拒絕的
                        if rejected_by == self.player:
                            print("\n你已拒絕此輪開始提議")
                        elif rejected_by:
                            print(f"\n⚠️ {rejected_by} 拒絕了開始提議")
                        else:
                            print("\n⚠️ 有人拒絕了開始提議")
            
            elif status == "in_game":
                if start.get("state") == "agreed":
                    if self.game_started:
                        print("\n🎮 遊戲已啟動，請在遊戲視窗中操作。")
                    else:
                        print("\n✅ 所有人都同意！即將啟動遊戲…")

        else:
            print("\n[等待房間資料...]")

        print("\n" + "="*50)
        if not self.player_ready:
            print("r) 標記為就緒 (Ready)")
        else:
            print("r) 取消就緒")
        print("q) 離開房間並返回大廳")
        print("\n(房間狀態會自動更新)")
    
    async def handle_input(self):
        """處理用戶輸入"""
        loop = asyncio.get_event_loop()
        
        while self.running:
            try:
                cmd = await loop.run_in_executor(None, input, "")
                cmd = cmd.strip().lower()
                
                # 🔒 遊戲進行中：房間介面輸入一律無效
                if self.game_started:
                    if cmd:  # 真的有打東西再提醒，避免一直洗版
                        print("⚠ 遊戲進行中，請在遊戲視窗操作；此視窗指令暫時無效。")
                    continue
                
                if cmd == "r":
                    if not self.player_ready:
                        resp = await send_req({"kind": "player_ready", "token": self.token, "room_id": self.room_id})
                        if resp.get("ok"):
                            self.player_ready = True
                        else:
                            print(resp.get("error"))
                    else:
                        resp = await send_req({"kind": "player_unready", "token": self.token, "room_id": self.room_id})
                        if resp.get("ok"):
                            self.player_ready = False
                        else:
                            print(resp.get("error"))
                
                elif cmd == "q":
                    await send_req({
                        "kind": "leave_room",
                        "token": self.token,
                        "room_id": self.room_id
                    })
                    print("\n[系統] 已要求離開房間，返回大廳...")
                    self.running = False

                    if self.writer and not self.writer.is_closing():
                        self.writer.close()
                        try:
                            await self.writer.wait_closed()
                        except Exception:
                            pass
                    break

                elif cmd == "s":
                    resp = await send_req({"kind":"propose_start","token": self.token,"room_id": self.room_id})
                    if not resp.get("ok"):
                        print(resp.get("error"))
                
                elif cmd == "y":
                    resp = await send_req({"kind":"respond_start","token": self.token,"room_id": self.room_id,"accept": True})
                    if not resp.get("ok"):
                        print(resp.get("error"))
                
                elif cmd == "n":
                    resp = await send_req({"kind":"respond_start","token": self.token,"room_id": self.room_id,"accept": False})
                    if not resp.get("ok"):
                        print(resp.get("error"))
 
            except Exception as e:
                print(f"輸入錯誤：{e}")
                await asyncio.sleep(0.1)
    
    async def run(self):
        """運行房間介面"""
        if not await self.connect_stream():
            input("\n(按 Enter 返回大廳) ")
            return
        
        self.display()
        
        try:
            await asyncio.gather(
                self.update_loop(),
                self.handle_input()
            )
        except KeyboardInterrupt:
            print("\n正在離開房間...")
        finally:
            self.running = False
            if self.writer:
                self.writer.close()
                try:
                    await self.writer.wait_closed()
                except Exception:
                    pass
            
            await send_req({"kind": "leave_room", "token": self.token, "room_id": self.room_id})

async def room_interface(token, player, room_id, join_info):
    """房間介面入口"""
    ui = AsyncRoomUI(token, player, room_id, join_info)
    await ui.run()

async def async_main():
    token = None
    player = None

    install_sigint_handler(
        lambda: LOBBY_HOST,
        lambda: LOBBY_PORT,
        lambda: token
    )
    
    try:
        while True:
        
            # 登入選單
            while token is None:
                clear_screen()
                print("[DEBUG] SERVER_IP =", SERVER_IP)
                print("[DEBUG] runtime exists =", runtime_path.exists())
                print("[DEBUG] SERVER_RUNTIME =", SERVER_RUNTIME)
                print("[DEBUG] LOBBY =", LOBBY_HOST, LOBBY_PORT)
                print("=== Lobby 登入選單 ===")
                print(f"(Lobby Server: {LOBBY_HOST}:{LOBBY_PORT})")
                print("1) 註冊")
                print("2) 登入")
                print("3) 離開")
                c = ask_choice("請選擇 (1-3): ", set("123"))

                if c == "1":
                    u = input("帳號: ").strip()
                    p = input("密碼: ").strip()
                    resp = await send_req({"kind":"register","username":u,"password":p})
                    print(resp)
                    input("\n(按 Enter 繼續) ")
                elif c == "2":
                    u = input("帳號: ").strip()
                    p = input("密碼: ").strip()
                    resp = await send_req({"kind":"login","username":u,"password":p})
                    if resp.get("ok"):
                        token = resp["token"]
                        player = u
                        print("登入成功")
                        input("\n(按 Enter 繼續) ")
                    else:
                        print(resp)
                        input("\n(按 Enter 繼續) ")
                else:
                    return

            # 主選單
            while token is not None:
                clear_screen()
                print("=== Lobby 主選單 ===")
                print(f"(Lobby Server: {LOBBY_HOST}:{LOBBY_PORT})")
                print("1) 商城 → 瀏覽遊戲/詳細資訊/下載更新")
                print("2) 大廳 → 建立/查看/加入房間")
                print("3) 我的紀錄 → 評分與評論")
                print("4) 登出並返回登入選單")
                print("5) 離開")
                choice = ask_choice("請選擇 (1-5): ", set("12345"))

                if choice == "1":
                    # 商城
                    while True:
                        clear_screen()
                        print("=== 商城 ===")
                        print(f"(Lobby Server: {LOBBY_HOST}:{LOBBY_PORT})")
                        print("1) 瀏覽遊戲列表")
                        print("2) 查看遊戲詳細資訊")
                        print("3) 下載 / 更新遊戲")
                        print("4) 返回")
                        c2 = ask_choice("選擇 (1-4): ", set("1234"))

                        if c2 == "1":
                            games = await fetch_playable_games(token)
                            _ = print_game_menu(games)
                            input("\n(按 Enter 繼續) ")

                        elif c2 == "2":
                            games = await fetch_playable_games(token)
                            items = print_game_menu(games)
                            if not items:
                                input("\n(按 Enter 繼續) ")
                                continue
                            valid = set(str(i) for i in range(1, len(items)+1))
                            idx = ask_choice("請輸入遊戲編號：", valid)
                            name, info = items[int(idx)-1]

                            resp = await send_req({"kind":"game_details","token":token,"name":name})
                            if resp.get("ok"):
                                d = resp["details"]
                                print(f"\n遊戲：{name}")
                                print(f"作者：{d.get('author','?')}")
                                print(f"狀態：{d.get('status','active')}")
                                print(f"最新版本：{info.get('latest')}")

                                # ✅ 顯示平均評分與評論數
                                avg = d.get("avg_rating")
                                cnt = d.get("review_count", 0)
                                if avg is not None and cnt > 0:
                                    print(f"平均評分：{avg} 分（{cnt} 則評論）")
                                else:
                                    print("平均評分：尚無評論")

                                # ✅ 顯示每一則評論（簡單版）
                                reviews = d.get("reviews", {})
                                if reviews:
                                    print("\n--- 評論列表 ---")
                                    for user, rv in reviews.items():
                                        print(f"- {user}：{rv.get('rating', '?')} 分")
                                        text = (rv.get("text") or "").strip()
                                        if text:
                                            print(f"  {text}")
                                else:
                                    print("\n目前還沒有任何評論。")
                            else:
                                print(resp.get("error"))
                            input("\n(按 Enter 繼續) ")


                        elif c2 == "3":
                            # 下載 / 更新遊戲
                            games = await fetch_playable_games(token)
                            items = print_game_menu(games)
                            if not items:
                                input("\n目前沒有可下載的遊戲。(按 Enter 繼續) ")
                                continue

                            valid = set(str(i) for i in range(1, len(items)+1))
                            idx = ask_choice("請輸入要下載的遊戲編號：", valid)
                            name, info = items[int(idx)-1]

                            print(f"\n正在向伺服器請求 {name} 最新版本安裝包...")
                            resp = await send_req({
                                "kind": "download_game",
                                "token": token,
                                "name": name
                            })
                            if not resp.get("ok"):
                                print("✗ 無法下載：", resp.get("error"))
                                input("\n(按 Enter 繼續) ")
                                continue

                            version = resp["version"]
                            zip_b64 = resp["zip_b64"]
                            data = base64.b64decode(zip_b64.encode("utf-8"))

                            # 目標目錄：player/downloads/<player>/<game>/<version>/
                            base_dir = DOWNLOADS_ROOT / player / name

                            # ✅ 先把這個玩家這款遊戲的舊版本全部刪掉
                            if base_dir.exists():
                                for sub in base_dir.iterdir():
                                    if sub.is_dir():
                                        shutil.rmtree(sub, ignore_errors=True)

                            dest = base_dir / version
                            dest.mkdir(parents=True, exist_ok=True)

                            safe_extract_zip(data, dest)

                            print(f"✓ 已下載 {name}@{version} 到 {dest}")
                            print("  之前的舊版本已自動清除。")
                            input("\n(按 Enter 繼續) ")


                        else:
                            break
                        
                elif choice == "2":
                    # 大廳
                    while True:
                        clear_screen()
                        print("=== 大廳 ===")
                        print(f"(Lobby Server: {LOBBY_HOST}:{LOBBY_PORT})")
                        print("1) 建立房間")
                        print("2) 查看房間列表")
                        print("3) 加入房間（輸入房間 ID）")
                        print("4) 返回")
                        c2 = ask_choice("選擇 (1-4): ", set("1234"))

                        if c2 == "1":
                            games = await fetch_playable_games(token)
                            items = print_game_menu(games)
                            if not items:
                                input("\n(按 Enter 繼續) ")
                                continue
                            
                            valid = set(str(i) for i in range(1, len(items) + 1))
                            idx = ask_choice("請輸入欲遊玩的遊戲編號：", valid)
                            name, info = items[int(idx) - 1]

                            # 一律使用伺服器宣告的最新版本
                            latest_ver = info.get("latest")
                            if not latest_ver:
                                print("❌ 此遊戲目前沒有可用版本。")
                                input("\n(按 Enter 繼續) ")
                                continue
                            
                            # 🔍 關鍵：建立房間前先確認「自己有沒有下載最新版」
                            if not has_local_game_version(player, name, latest_ver):
                                print("❌ 你目前尚未下載這款遊戲的最新版。")
                                print("   請先到『商城』→『下載 / 更新遊戲』下載後，再建立房間。")
                                input("\n(按 Enter 繼續) ")
                                continue
                            
                            # 有最新版才真的送 create_room
                            resp = await send_req({
                                "kind": "create_room",
                                "token": token,
                                "game": name,
                                "version": latest_ver,
                            })
                            if resp.get("ok"):
                                room_id = resp.get("room_id")
                                print(f"✓ 房間建立成功：{room_id}")
                                await asyncio.sleep(1)
                                await room_interface(token, player, room_id, resp)
                            else:
                                print(f"✗ {resp.get('error')}")
                                input("\n(按 Enter 繼續) ")


                        elif c2 == "2":
                            rooms = await fetch_rooms(token)
                            print_room_menu(rooms)
                            input("\n(按 Enter 繼續) ")

                        elif c2 == "3":
                            rooms = await fetch_rooms(token)
                            items = print_room_menu(rooms)
                            if not items:
                                input("\n目前沒有房間可以加入。(按 Enter 繼續) ")
                                continue
                            
                            print()
                            rid = input("請輸入要加入的房間 ID如：tetris-1765205455-7626（或 Enter 返回）：").strip()
                            if not rid:
                                continue
                            
                            r = rooms.get(rid)
                            if not r:
                                print("❌ 房間不存在")
                                input("\n(按 Enter 繼續) ")
                                continue
                            
                            game_name = r["game"]
                            room_ver  = r["version"]

                            # 確認遊戲仍為 active & 取得最新版本
                            games = await fetch_playable_games(token)
                            ginfo = games.get(game_name)
                            if not ginfo:
                                print("⚠ 此遊戲目前已下架或不可下載，無法加入新房間。")
                                input("\n(按 Enter 繼續) ")
                                continue
                            
                            latest_ver = ginfo.get("latest")
                            if room_ver != latest_ver:
                                print(f"⚠ 此房間使用舊版本 {room_ver}，目前最新版本為 {latest_ver}。")
                                print("   請先到『商城』下載 / 更新到最新版本，並加入使用最新版的房間。")
                                input("\n(按 Enter 繼續) ")
                                continue
                            
                            if not has_local_game_version(player, game_name, latest_ver):
                                print("❌ 你目前尚未下載此遊戲的最新版。")
                                print("   請先到『商城』下載 / 更新遊戲，再重新加入房間。")
                                input("\n(按 Enter 繼續) ")
                                continue
                            
                            join = await send_req({"kind": "join_room", "token": token, "room_id": rid})
                            if not join.get("ok"):
                                print(f"✗ {join.get('error')}")
                                input("\n(按 Enter 繼續) ")
                                continue
                            
                            print(f"✓ 成功加入房間：{rid}")
                            await asyncio.sleep(1)
                            await room_interface(token, player, rid, join)

                        else:
                            break
                        
                elif choice == "3":
                    # 我的紀錄 → 評分與評論
                    clear_screen()
                    print("=== 我的紀錄 → 評分與評論 ===")
                    print(f"(Lobby Server: {LOBBY_HOST}:{LOBBY_PORT})")

                    games = await fetch_playable_games(token)
                    items = print_game_menu(games)
                    if not items:
                        input("\n目前沒有可評分的遊戲（先去商城下載並遊玩吧）\n(按 Enter 返回) ")
                        continue
                    
                    valid = set(str(i) for i in range(1, len(items)+1))
                    idx = ask_choice("請選擇要評分的遊戲編號：", valid)
                    name, info = items[int(idx) - 1]
                    display_name = info.get("display_name", name)

                    print(f"\n選擇遊戲：{display_name} ({name})")
                    rating = input("評分 (1-5): ").strip()
                    text = input("短評 (可留空): ").strip()

                    try:
                        rating_int = int(rating)
                    except:
                        print("評分需為數字 1-5")
                        input("\n(按 Enter 繼續) ")
                        continue
                    
                    resp = await send_req({
                        "kind": "rate_game",
                        "token": token,
                        "name": name,
                        "rating": rating_int,
                        "text": text
                    })

                    if resp.get("ok"):
                        print("✓ 評論已送出")
                        avg = resp.get("avg_rating")
                        cnt = resp.get("count")
                        if avg is not None:
                            print(f"目前平均分數：{avg}（{cnt} 則評論）")
                    else:
                        print("✗ 無法送出評論：", resp.get("error"))

                    input("\n(按 Enter 繼續) ")

                elif choice == "4":
                    # 登出並回到登入選單
                    if token is not None:
                        try:
                            await send_req({"kind": "logout", "token": token})
                        except Exception:
                            pass
                    token = None
                    player = None
                    print("已登出，返回登入選單。")
                    input("\n(按 Enter 繼續) ")
                    break
                
                elif choice == "5":
                    # 直接離開程式
                    if token is not None:
                        try:
                            await send_req({"kind": "logout", "token": token})
                        except Exception:
                            pass
                    print("再見～")
                    return
    except (KeyboardInterrupt, asyncio.CancelledError, EOFError) as e:
        print(f"\n[系統] 偵測到中斷信號 ({type(e).__name__})，正在登出...")
    finally:
        if token is not None:
            try:
                print("[系統] 正在釋放 token...")
                async def safe_logout():
                    try:
                        return await send_req({"kind": "logout", "token": token})
                    except Exception as e:
                        return {"ok": False, "error": str(e)}
                logout_coro = safe_logout()
                try:
                    resp = await asyncio.wait_for(
                        asyncio.shield(logout_coro), 
                        timeout=3.0
                    )
                    if resp.get("ok"):
                        print("[系統] ✓ 已成功登出並釋放 token")
                    else:
                        print(f"[系統] 登出回應：{resp.get('msg', resp.get('error', 'unknown'))}")
                except asyncio.TimeoutError:
                    print("[系統] ⚠ 登出超時（server 可能已關閉），但已盡力釋放")
                except asyncio.CancelledError:
                    print("[系統] ⚠ 登出被中斷，但 token 應該已在釋放中")
                except Exception as e:
                    print(f"[系統] 登出時發生錯誤：{e}")
            except Exception as outer_e:
                print(f"[系統] 清理過程出錯：{outer_e}")
        
        print("[系統] 再見！")
            
def main():
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
