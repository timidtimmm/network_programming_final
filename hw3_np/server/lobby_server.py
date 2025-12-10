# server/lobby_server.py - 修正版（版本號一致性 + 遊戲結束自動 reset）
import os, json, socket, threading, subprocess, time, random, traceback, base64, zipfile, io, re
from pathlib import Path
from common import db, auth

# Lobby 自己的對外 host/port（讓遊戲 server 知道要打回哪裡）
LOBBY_HOST = None
LOBBY_PORT = None

# ✅ 修改：uploaded_games 放在 server/ 資料夾內
ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = Path(__file__).resolve().parent
CONF = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

# ✅ 修改：使用 public_host 作為房間的對外 IP
def _detect_local_ip_candidates():
    """
    取得本機可能的 IP（不用外網服務）
    用 UDP trick 拿到目前對外路由會選的介面 IP
    """
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return ips
def _is_local_ip(ip: str) -> bool:
    """
    用 OS 來判斷這個 IP 是否屬於「本機」。
    只要能 bind 成功，就代表這台機器真的擁有這個 IP。
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((ip, 0))
        s.close()
        return True
    except OSError:
        return False

def pick_public_host(conf):
    # 1) 手動指定就尊重（也允許環境變數覆蓋，Demo 超穩）
    manual = (os.getenv("PUBLIC_HOST") or conf.get("public_host") or "").strip()
    if manual:
        return manual

    public_list = conf.get("public_hosts") or []

    # 2) ✅ 最重要：直接檢查哪個 140.113.17.1X 是本機擁有的
    for ip in public_list:
        if _is_local_ip(ip):
            return ip

    # 3) 退一步：用你原本 UDP trick 當備援
    local_ips = _detect_local_ip_candidates()
    for ip in local_ips:
        if ip in set(public_list):
            return ip

    # 4) ❗不要再 fallback 到 sorted(...)[0] 了
    #    否則你會把所有機器都宣告成 .11
    return "127.0.0.1"


PUBLIC_HOST = pick_public_host(CONF)
print(f"[Lobby] PUBLIC_HOST selected: {PUBLIC_HOST}", flush=True)

GAMES_FILE = "games.json"
PLAYER_USERS_FILE = "player_users.json"
ROOMS_FILE = "rooms.json"
UPLOADED = SERVER_DIR / "uploaded_games"

# === SSE 訂閱管理 ===
room_subscribers = {}
subscribers_lock = threading.RLock()

def subscribe_room(room_id, conn):
    with subscribers_lock:
        if room_id not in room_subscribers:
            room_subscribers[room_id] = []
        room_subscribers[room_id].append(conn)

def unsubscribe_room(room_id, conn):
    with subscribers_lock:
        if room_id in room_subscribers:
            try:
                room_subscribers[room_id].remove(conn)
            except ValueError:
                pass

def broadcast_room_update(room_id):
    with subscribers_lock:
        if room_id not in room_subscribers:
            return
        
        rooms = db.load(ROOMS_FILE, {})
        if room_id not in rooms:
            return
        
        room_data = rooms[room_id]
        message = json.dumps({"event": "room_update", "room": room_data}, ensure_ascii=False)
        
        dead_conns = []
        for conn in room_subscribers[room_id]:
            try:
                conn.sendall((message + "\n").encode("utf-8"))
            except Exception:
                dead_conns.append(conn)
        
        for conn in dead_conns:
            room_subscribers[room_id].remove(conn)

# === 版本號處理函數 ===
def _semver_key(v: str):
    """改進版：正規化版本號以統一比較"""
    # 移除所有非數字和點的字元
    v_clean = re.sub(r'[^0-9.]', '', v)
    parts = v_clean.split('.')
    
    # 補齊到至少3位，並轉換為整數
    result = []
    for i in range(3):
        if i < len(parts) and parts[i]:
            result.append(int(parts[i]))
        else:
            result.append(0)
    
    return tuple(result)

def normalize_version(v: str) -> str:
    """正規化版本號：1.01 → 1.0.1, 1.1 → 1.1.0"""
    key = _semver_key(v)
    return f"{key[0]}.{key[1]}.{key[2]}"

def _scan_uploaded_games():
    """
    掃描檔案系統，回傳可用遊戲
    同時正規化版本號以避免 1.0.1 和 1.01 被視為不同版本
    """
    games = {}
    if not UPLOADED.exists():
        return games
    
    for gdir in UPLOADED.iterdir():
        if not gdir.is_dir(): 
            continue
        
        versions_raw = []
        version_map = {}  # {normalized: original_folder_name}
        
        for vdir in gdir.iterdir():
            if vdir.is_dir() and (vdir / "manifest.json").exists():
                raw_version = vdir.name
                normalized = normalize_version(raw_version)
                
                # 如果正規化版本已存在，保留較新的資料夾
                if normalized in version_map:
                    print(f"[Warning] 發現重複版本號：{raw_version} 和 {version_map[normalized]} 都對應到 {normalized}")
                    # 可選：比較修改時間，保留較新的
                    old_path = gdir / version_map[normalized]
                    new_path = vdir
                    if new_path.stat().st_mtime > old_path.stat().st_mtime:
                        version_map[normalized] = raw_version
                else:
                    version_map[normalized] = raw_version
                    versions_raw.append(normalized)
        
        if versions_raw:
            versions_raw.sort(key=_semver_key, reverse=True)
            games[gdir.name] = {
                "versions": versions_raw,
                "latest": versions_raw[0],
                "version_map": version_map  # 保存映射關係
            }
    
    return games

def ensure_user_db():
    users = db.load(PLAYER_USERS_FILE, {})
    if not isinstance(users, dict):
        users = {}
        db.save(PLAYER_USERS_FILE, users)

def handle_register(payload):
    u = payload.get("username","").strip()
    p = payload.get("password","").strip()
    if not u or not p:
        return {"ok": False, "error": "缺少帳號或密碼"}
    users = db.load(PLAYER_USERS_FILE, {})
    if u in users:
        return {"ok": False, "error": "帳號已被使用"}
    users[u] = {"password": p}
    db.save(PLAYER_USERS_FILE, users)
    return {"ok": True, "msg": "註冊成功"}

def handle_login(payload):
    u = payload.get("username","").strip()
    p = payload.get("password","").strip()
    users = db.load(PLAYER_USERS_FILE, {})

    if u not in users or users[u].get("password") != p:
        return {"ok": False, "error": "帳號或密碼錯誤"}

    token = auth.issue_token(u, role="player")
    if not token:
        return {"ok": False, "error": "帳號已在其他裝置登入"}

    return {
        "ok": True,
        "token": token,
        "user": u,
        "role": "player",
        "msg": "登入成功"
    }

def handle_list_games(payload):
    """列出遊戲 - 只顯示檔案系統中實際存在且 active 的遊戲"""
    fs_games = _scan_uploaded_games()
    db_games = db.load(GAMES_FILE, {})
    
    result = {}
    for name, fs_info in fs_games.items():
        db_info = db_games.get(name, {})
        status = db_info.get("status", "active")
        
        # 只回傳 active 狀態的遊戲
        if status == "active":
            # 取得實際版本數（從 db 中讀取）
            db_versions = db_info.get("versions", {})
            actual_versions = []
            
            # 只列出檔案系統中實際存在的版本
            for norm_ver in fs_info["versions"]:
                if norm_ver in db_versions:
                    actual_versions.append(norm_ver)
            
            if not actual_versions:
                actual_versions = fs_info["versions"]
            
            result[name] = {
                "versions": actual_versions,
                "latest": fs_info["latest"],
                "author": db_info.get("author"),
                "display_name": db_info.get("versions", {}).get(
                    fs_info["latest"], {}
                ).get("manifest", {}).get("display_name", name),
                # ✅ 新增：讓商城可以先看到評分概況
                "avg_rating": db_info.get("avg_rating"),
                "review_count": db_info.get("review_count", 0),
            }
    
    print(f"[Lobby] 回傳 {len(result)} 個 active 遊戲：{list(result.keys())}")
    return {"ok": True, "games": result}

def handle_player_ready(payload):
    token = payload.get("token")
    t = auth.verify_token(token, role="player")
    if not t:
        return {"ok": False, "error": "未登入"}
    player = t["user"]
    
    room_id = payload.get("room_id","").strip()
    rooms = db.load(ROOMS_FILE, {})
    if room_id not in rooms:
        return {"ok": False, "error": "房間不存在"}
    
    r = rooms[room_id]
    
    if player not in r.get("players", []):
        return {"ok": False, "error": "你不在此房間內"}
    
    ready_players = r.get("ready_players", [])
    if player not in ready_players:
        ready_players.append(player)
        r["ready_players"] = ready_players
        
        if len(ready_players) == len(r.get("players", [])):
            r["status"] = "ready"
            
        db.save(ROOMS_FILE, rooms)
        broadcast_room_update(room_id)
    
    return {"ok": True, "msg": "已標記為就緒", "ready_players": ready_players}

def handle_player_unready(payload):
    token = payload.get("token")
    t = auth.verify_token(token, role="player")
    if not t:
        return {"ok": False, "error": "未登入"}
    player = t["user"]
    
    room_id = payload.get("room_id","").strip()
    rooms = db.load(ROOMS_FILE, {})
    if room_id not in rooms:
        return {"ok": False, "error": "房間不存在"}
    
    r = rooms[room_id]
    
    ready_players = r.get("ready_players", [])
    if player in ready_players:
        ready_players.remove(player)
        r["ready_players"] = ready_players
        
        if r.get("status") == "ready":
            r["status"] = "waiting"
            
        db.save(ROOMS_FILE, rooms)
        broadcast_room_update(room_id)
    
    return {"ok": True, "msg": "已取消就緒"}

def handle_rate_game(payload):
    token = payload.get("token")
    t = auth.verify_token(token, role="player")
    if not t:
        return {"ok": False, "error": "未登入"}
    user = t["user"]

    name = (payload.get("name") or "").strip()
    rating = payload.get("rating")
    text = (payload.get("text") or "").strip()

    if not name:
        return {"ok": False, "error": "缺少遊戲名稱"}

    try:
        rating = int(rating)
        if rating < 1 or rating > 5:
            raise ValueError
    except Exception:
        return {"ok": False, "error": "評分必須是 1~5 的整數"}

    # 檢查是否玩過
    users = db.load(PLAYER_USERS_FILE, {})
    played = users.get(user, {}).get("played", {})
    # 若 played 不是 dict（例如 list），也先修正一下
    if not isinstance(played, dict):
        played = {}
    played_ok = bool(played.get(name, 0))
    if not played_ok:
        return {"ok": False, "error": "必須先玩過此遊戲才能留言/評分"}

    # 讀取遊戲資料
    games = db.load(GAMES_FILE, {})
    if name not in games:
        return {"ok": False, "error": "遊戲不存在"}

    g = games[name]

    # ⭐ 關鍵：reviews 一律用 dict，舊的 list 直接丟掉重建
    reviews = g.get("reviews")
    if not isinstance(reviews, dict):
        reviews = {}

    reviews[user] = {
        "rating": rating,
        "text": text,
        "ts": int(time.time())
    }
    g["reviews"] = reviews

    # 重新計算平均分數
    if reviews:
        s = sum(r["rating"] for r in reviews.values())
        n = len(reviews)
        g["avg_rating"] = round(s / n, 2)
        g["review_count"] = n
    else:
        g["avg_rating"] = None
        g["review_count"] = 0

    games[name] = g
    db.save(GAMES_FILE, games)

    return {
        "ok": True,
        "msg": "已送出評論/評分",
        "avg_rating": g.get("avg_rating"),
        "count": g.get("review_count")
    }

def handle_logout(payload):
    token = payload.get("token")
    if token:
        auth.revoke_token(token)
    return {"ok": True, "msg": "已登出"}

def handle_subscribe_room(payload, conn):
    token = payload.get("token")
    t = auth.verify_token(token, role="player")
    if not t:
        return {"ok": False, "error": "未登入"}
    
    room_id = payload.get("room_id","").strip()
    rooms = db.load(ROOMS_FILE, {})
    if room_id not in rooms:
        return {"ok": False, "error": "房間不存在"}
    
    subscribe_room(room_id, conn)
    # ✅ 這裡多把目前房間狀態回傳給訂閱者
    return {
        "ok": True,
        "msg": "已訂閱房間更新",
        "room_id": room_id,
        "room": rooms[room_id],
    }

def handle_game_details(payload):
    name = payload.get("name","").strip()
    games = db.load(GAMES_FILE, {})
    if name not in games:
        return {"ok": False, "error": "遊戲不存在"}
    
    game_data = games[name]
    
    # ✅ 關鍵修改：移除 zip_b64，只保留 manifest
    # 避免回傳資料過大導致 framing 錯誤
    cleaned_data = {
        "status": game_data.get("status"),
        "author": game_data.get("author"),
        "latest": game_data.get("latest"),
        "avg_rating": game_data.get("avg_rating"),
        "review_count": game_data.get("review_count", 0),
        "reviews": game_data.get("reviews", {}),
        "versions": {}
    }
    
    # 只複製每個版本的 manifest，不包含 zip_b64
    for ver, ver_data in game_data.get("versions", {}).items():
        cleaned_data["versions"][ver] = {
            "manifest": ver_data.get("manifest", {}),
            "uploaded_at": ver_data.get("uploaded_at"),
            # ❌ 不包含 "zip_b64"
        }
    
    return {"ok": True, "details": cleaned_data}

def handle_download_game(payload):
    name = payload.get("name","").strip()
    games = db.load(GAMES_FILE, {})
    if name not in games:
        return {"ok": False, "error": "遊戲不存在"}
    g = games[name]
    if g.get("status") != "active":
        return {"ok": False, "error": "此遊戲已下架"}
    version = g.get("latest")
    if not version or version not in g.get("versions", {}):
        return {"ok": False, "error": "無可下載版本"}
    pkg = g["versions"][version]
    return {"ok": True, "name": name, "version": version, "manifest": pkg["manifest"], "zip_b64": pkg["zip_b64"]}

def _find_free_port(min_port=10000, max_port=65535):
    """為遊戲房間分配 10000 以上的 port"""
    import socket
    import random
    
    for _ in range(100):
        port = random.randint(min_port, max_port)
        try:
            s = socket.socket()
            # ✅ 修改：綁定 0.0.0.0 以接受外部連線
            s.bind(("0.0.0.0", port))
            actual_port = s.getsockname()[1]
            s.close()
            return actual_port
        except OSError:
            continue
    
    s = socket.socket()
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    
    if port < min_port:
        return _find_free_port(min_port, max_port)
    
    return port

def handle_list_rooms(payload):
    rooms = db.load(ROOMS_FILE, {})
    return {"ok": True, "rooms": rooms}

def handle_create_room(payload):
    token = payload.get("token")
    t = auth.verify_token(token, role="player")
    if not t:
        return {"ok": False, "error": "未登入"}
    session_user = t["user"]

    req_game = (payload.get("game") or "").strip()
    if not req_game:
        return {"ok": False, "error": "缺少遊戲名稱"}

    # 1) 掃檔案系統：確認這個遊戲真的有被上傳
    fs_games = _scan_uploaded_games()
    if req_game not in fs_games:
        return {"ok": False, "error": "遊戲不存在或不可用"}

    # 2) 檢查 DB：遊戲必須存在，且 status = active
    db_games = db.load(GAMES_FILE, {})
    ginfo = db_games.get(req_game)
    if not ginfo or ginfo.get("status", "active") != "active":
        return {"ok": False, "error": "此遊戲已下架，無法建立新的房間"}

    # 3) 從 DB 讀出「最新版本」並正規化
    db_latest_raw = ginfo.get("latest")
    if not db_latest_raw:
        return {"ok": False, "error": "找不到此遊戲的最新版本資訊"}

    db_latest = normalize_version(db_latest_raw)

    # 4) 檔案系統也必須有這個最新版本
    fs_info = fs_games[req_game]
    fs_versions = set(fs_info["versions"])
    if db_latest not in fs_versions:
        return {"ok": False, "error": f"伺服器缺少最新版本檔案（{db_latest}）"}

    # 5) 若 payload 有帶 version，且 != 最新版本 → 直接拒絕
    req_version_raw = (payload.get("version") or "").strip()
    if req_version_raw:
        req_norm = normalize_version(req_version_raw)
        if req_norm != db_latest:
            return {
                "ok": False,
                "error": f"此遊戲只能使用最新版本 {db_latest} 建立房間，"
                         f"請先到商城下載/更新後再試"
            }

    # 6) 一律改用「最新版本」開房
    version = db_latest

    # 7) 找到實際資料夾名稱
    version_map = fs_info.get("version_map", {})
    actual_folder = version_map.get(version, version)

    game_root = UPLOADED / req_game / actual_folder
    manifest_path = game_root / "manifest.json"
    if not manifest_path.exists():
        return {"ok": False, "error": "伺服器缺少遊戲檔案"}

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest.get("entry_server", "start_server.py")
    max_players = manifest.get("max_players", 2)

    # ✅ 伺服器綁定和客戶端連線位址要分開處理
    server_bind_host = "0.0.0.0"  # 伺服器綁定在所有介面
    client_connect_host = PUBLIC_HOST  # 客戶端用 public_host 連線
    
    port = _find_free_port()
    room_id = f"{req_game}-{int(time.time())}-{random.randint(1000, 9999)}"

    cwd = game_root
    if not (cwd / entry).exists():
        return {"ok": False, "error": f"缺少 server entry: {entry}"}

    env = os.environ.copy()
    lobby_connect_host = LOBBY_HOST
    if LOBBY_HOST == "0.0.0.0":
        lobby_connect_host = PUBLIC_HOST if PUBLIC_HOST != "127.0.0.1" else "127.0.0.1"
    
    env.update({
        "GAME_HOST": server_bind_host,  # ← 遊戲伺服器綁定用
        "GAME_PORT": str(port),
        "ROOM_ID": room_id,
        "GAME_NAME": req_game,
        "GAME_VERSION": version,
        "LOBBY_HOST": LOBBY_HOST,
        "LOBBY_CONNECT_HOST": lobby_connect_host,
        "LOBBY_PORT": str(LOBBY_PORT or 0),
    })

    print(f"[Lobby] 啟動遊戲伺服器：{req_game}@{version} on {server_bind_host}:{port}", flush=True)
    
    # ✅ 啟動遊戲伺服器
    proc = subprocess.Popen(
        [__import__("sys").executable, entry],
        cwd=str(cwd), 
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1
    )

    # ✅ 關鍵修改：等待伺服器真正啟動（最多等 10 秒）
    server_ready = False
    print(f"[Lobby] 等待遊戲伺服器啟動...", flush=True)
    
    for attempt in range(50):  # 50 次 × 0.2 秒 = 10 秒
        try:
            test_sock = socket.socket()
            test_sock.settimeout(0.5)
            # ✅ 嘗試連線到伺服器綁定的 port
            test_sock.connect(("127.0.0.1", port))
            test_sock.close()
            server_ready = True
            print(f"[Lobby] ✓ 遊戲伺服器已就緒（嘗試 {attempt + 1} 次，耗時 {(attempt + 1) * 0.2:.1f}秒）", flush=True)
            break
        except (ConnectionRefusedError, OSError, socket.timeout):
            time.sleep(0.2)
            # 檢查進程是否還活著
            if proc.poll() is not None:
                print(f"[Lobby] ✗ 遊戲伺服器進程意外終止（退出碼：{proc.returncode}）", flush=True)
                break
    
    if not server_ready:
        print(f"[Lobby] ✗ 遊戲伺服器啟動超時或失敗", flush=True)
        try:
            proc.kill()
            proc.wait(timeout=2)
        except:
            pass
        return {"ok": False, "error": "遊戲伺服器啟動失敗，請稍後再試"}

    # ✅ 伺服器就緒後才儲存房間資訊
    rooms = db.load(ROOMS_FILE, {})
    rooms[room_id] = {
        "game": req_game,
        "version": version,
        "host": client_connect_host,  # ← 客戶端連線用這個
        "port": port,
        "status": "waiting",
        "owner": session_user,
        "start": {"state": "idle"},
        "players": [session_user],
        "ready_players": [],
        "max_players": max_players,
        "pid": proc.pid,
    }
    db.save(ROOMS_FILE, rooms)
    
    print(f"[Lobby] ✓ 房間 {room_id} 建立完成", flush=True)
    return {"ok": True, "room_id": room_id, **rooms[room_id]}

def _mark_played(game_name: str, players: list[str]):
    users = db.load(PLAYER_USERS_FILE, {})
    changed = False
    for u in players:
        rec = users.get(u, {})
        played = rec.get("played", {})
        played[game_name] = int(played.get(game_name, 0)) + 1
        rec["played"] = played
        users[u] = rec
        changed = True
    if changed:
        db.save(PLAYER_USERS_FILE, users)

def handle_join_room(payload):
    token = payload.get("token")
    t = auth.verify_token(token, role="player")
    if not t:
        return {"ok": False, "error": "未登入"}
    player = t["user"]
    room_id = payload.get("room_id","").strip()
    rooms = db.load(ROOMS_FILE, {})
    if room_id not in rooms:
        return {"ok": False, "error": "房間不存在"}
    
    r = rooms[room_id]
    
    # ✅ 檢查人數上限
    current_players = r.get("players", [])
    max_players = r.get("max_players", 2)
    
    # 如果玩家已經在房間裡，允許重新加入（斷線重連）
    if player not in current_players:
        if len(current_players) >= max_players:
            return {
                "ok": False, 
                "error": f"房間已滿 ({len(current_players)}/{max_players} 人)"
            }
        
        # 有空位才加入
        current_players.append(player)
        r["players"] = current_players
        db.save(ROOMS_FILE, rooms)
        broadcast_room_update(room_id)
    
    return {"ok": True, "room_id": room_id, **r}

def handle_leave_room(payload):
    token = payload.get("token")
    t = auth.verify_token(token, role="player")
    if not t:
        return {"ok": False, "error": "未登入"}

    player = t["user"]
    room_id = (payload.get("room_id") or "").strip()

    rooms = db.load(ROOMS_FILE, {})
    if room_id not in rooms:
        return {"ok": False, "error": "房間不存在"}

    r = rooms[room_id]
    players = r.get("players", [])
    ready_players = r.get("ready_players", [])

    if player not in players:
        return {"ok": True, "msg": "已離開房間"}

    if player in players:
        players.remove(player)
    if player in ready_players:
        ready_players.remove(player)

    r["players"] = players
    r["ready_players"] = ready_players

    # ✅ 如果沒人，關房
    if not players:
        rooms.pop(room_id, None)
        db.save(ROOMS_FILE, rooms)
        return {"ok": True, "msg": "房間已關閉"}

    # ✅ NEW：如果離開的是房主，把房主換成剩下的第一個人
    if r.get("owner") == player:
        new_owner = players[0]
        r["owner"] = new_owner
        # 房主換人時，把開始提議狀態清空比較安全
        r["start"] = {"state": "idle"}

    # ✅ 若原本在 in_game，有人離開就視為本局結束
    if r.get("status") == "in_game":
        r["status"] = "waiting"
        r["start"] = {"state": "idle"}

    rooms[room_id] = r
    db.save(ROOMS_FILE, rooms)
    broadcast_room_update(room_id)

    return {"ok": True, "msg": "已離開房間"}

def handle_game_finished(payload):
    """遊戲 server 呼叫：某個 room 的一局已經結束了"""
    room_id = (payload.get("room_id") or "").strip()
    if not room_id:
        return {"ok": False, "error": "缺少 room_id"}

    rooms = db.load(ROOMS_FILE, {})
    if room_id not in rooms:
        return {"ok": False, "error": "房間不存在"}

    r = rooms[room_id]

    # ✅ 若有要求 kick_all：直接踢 & 關房
    if bool(payload.get("kick_all")):
        print(f"[Lobby] Kicking all players from room {room_id}", flush=True)

        # 1) 清空玩家並標記為 closed
        r["players"] = []
        r["ready_players"] = []
        r["status"] = "closed"
        rooms[room_id] = r
        db.save(ROOMS_FILE, rooms)
        broadcast_room_update(room_id)

        # 2) 給 SSE 一點時間推送
        time.sleep(0.5)

        # 3) 刪除房間
        rooms.pop(room_id, None)
        db.save(ROOMS_FILE, rooms)

        print(f"[Lobby] Room {room_id} closed and removed", flush=True)
        return {"ok": True, "msg": "room closed (kicked all)"}

    # 沒帶 kick_all：僅重設
    print(f"[Lobby] Resetting room {room_id}", flush=True)
    r["status"] = "waiting"
    r["start"] = {"state": "idle"}
    r["ready_players"] = []
    rooms[room_id] = r
    db.save(ROOMS_FILE, rooms)
    broadcast_room_update(room_id)

    return {"ok": True, "msg": "room reset"}

def start_room_monitor():
    """後臺線程：監控長時間未結束的房間"""
    def monitor():
        while True:
            try:
                time.sleep(10)  # 每 10 秒檢查一次
                
                rooms = db.load(ROOMS_FILE, {})
                now = time.time()
                
                for room_id, r in list(rooms.items()):
                    status = r.get("status")
                    
                    # ✅ 檢查 in_game 狀態的房間是否超時
                    if status == "in_game":
                        # 假設遊戲最長 5 分鐘
                        start_ts = r.get("start", {}).get("ts", now)
                        if now - start_ts > 300:  # 5 分鐘
                            print(f"[Lobby] ⚠ Room {room_id} timeout (5min), force closing", flush=True)
                            
                            # 強制關閉房間
                            r["players"] = []
                            r["ready_players"] = []
                            r["status"] = "closed"
                            rooms[room_id] = r
                            db.save(ROOMS_FILE, rooms)
                            broadcast_room_update(room_id)
                            
                            time.sleep(0.5)
                            rooms.pop(room_id, None)
                            db.save(ROOMS_FILE, rooms)
                    
                    # ✅ 檢查空房間（沒有玩家的房間）
                    elif len(r.get("players", [])) == 0:
                        print(f"[Lobby] Removing empty room {room_id}", flush=True)
                        rooms.pop(room_id, None)
                        db.save(ROOMS_FILE, rooms)
            
            except Exception as e:
                print(f"[Lobby] Monitor error: {e}", flush=True)
    
    t = threading.Thread(target=monitor, daemon=True)
    t.start()
    print("[Lobby] Room monitor started", flush=True)

def serve(host, port, stop_event=None):
    global LOBBY_HOST, LOBBY_PORT
    LOBBY_HOST = host
    LOBBY_PORT = port

    ensure_user_db()
    
    # ✅ 新增：啟動房間監控
    start_room_monitor()
    print(f"[Lobby] Running with server_host={host}, PUBLIC_HOST={PUBLIC_HOST}", flush=True)

    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(128)

    # 短 timeout：避免 accept 永遠卡住
    s.settimeout(0.5)
    
    print(f"[LobbyServer] listening on {host}:{s.getsockname()[1]}")

    try:
        while True:
            # 外部要求停止時，跳出主迴圈
            if stop_event is not None and stop_event.is_set():
                print("[LobbyServer] stop_event set, exiting serve loop.")
                break

            try:
                conn, addr = s.accept()
            except socket.timeout:
                # 定期醒來檢查 stop_event
                continue
            except OSError as e:
                # socket 已關閉或其他錯誤，結束迴圈
                print(f"[LobbyServer] Socket closed / error: {e}")
                break

            threading.Thread(
                target=_handle_conn,
                args=(conn, addr),
                daemon=True
            ).start()
    finally:
        try:
            s.close()
        except Exception:
            pass
        print(f"[LobbyServer] Shutdown complete on {host}:{port}")

def handle_propose_start(payload):
    token = payload.get("token")
    t = auth.verify_token(token, role="player")
    if not t:
        return {"ok": False, "error": "未登入"}
    user = t["user"]

    room_id = (payload.get("room_id") or "").strip()
    rooms = db.load(ROOMS_FILE, {})
    if room_id not in rooms:
        return {"ok": False, "error": "房間不存在"}

    r = rooms[room_id]
    if r.get("owner") != user:
        return {"ok": False, "error": "只有房主可以發起開始"}

    players = r.get("players", [])
    max_players = r.get("max_players", 2)  # ✅ 讀取房間的 max_players
    
    # ✅ 修改：使用動態人數檢查，而非寫死 2
    if len(players) < max_players:
        return {
            "ok": False, 
            "error": f"人數不足，需要 {max_players} 人才能開始（目前 {len(players)} 人）"
        }

    r["start"] = {"state": "proposed", "by": user, "ts": int(time.time())}
    r["status"] = "waiting"
    db.save(ROOMS_FILE, rooms)
    broadcast_room_update(room_id)
    return {"ok": True, "msg": "已送出開始提議"}

def handle_respond_start(payload):
    token = payload.get("token")
    t = auth.verify_token(token, role="player")
    if not t:
        return {"ok": False, "error": "未登入"}
    user = t["user"]

    room_id = (payload.get("room_id") or "").strip()
    accept = bool(payload.get("accept"))
    rooms = db.load(ROOMS_FILE, {})
    if room_id not in rooms:
        return {"ok": False, "error": "房間不存在"}

    r = rooms[room_id]
    
    if r.get("owner") == user:
        return {"ok": False, "error": "房主不需要回覆開始提議"}

    if r.get("start", {}).get("state") != "proposed":
        return {"ok": False, "error": "目前沒有開始提議"}

    # ❌ 拒絕：立即結束提議
    if not accept:
        r["start"] = {
            "state": "rejected", 
            "by": r.get("owner"), 
            "rejected_by": user,  # ✅ 記錄誰拒絕的
            "ts": int(time.time())
        }
        db.save(ROOMS_FILE, rooms)
        broadcast_room_update(room_id)  # ✅ 確保廣播
        return {"ok": True, "msg": "已拒絕開始"}

    # ✅ 同意：記錄此玩家的同意狀態
    start_data = r.get("start", {})
    if "responses" not in start_data:
        start_data["responses"] = {}
    
    start_data["responses"][user] = True
    r["start"] = start_data
    
    # ✅ 檢查是否所有房客都同意了
    players = r.get("players", [])
    owner = r.get("owner")
    guests = [p for p in players if p != owner]
    
    responses = start_data.get("responses", {})
    all_agreed = all(responses.get(guest, False) for guest in guests)
    
    if all_agreed:
        # ✅ 所有房客都同意了，可以開始
        r["start"] = {"state": "agreed", "by": owner, "ts": int(time.time())}
        r["status"] = "in_game"
        r["ready_players"] = []
        db.save(ROOMS_FILE, rooms)
        broadcast_room_update(room_id)

        try:
            _mark_played(r["game"], players)
        except Exception:
            pass

        return {"ok": True, "msg": "對局開始"}
    else:
        # ✅ 關鍵修正：即使還沒全部同意，也要廣播更新
        db.save(ROOMS_FILE, rooms)
        broadcast_room_update(room_id)  # ⭐ 這裡是關鍵！
        
        not_responded = [g for g in guests if not responses.get(g, False)]
        agreed_count = len(guests) - len(not_responded)
        total_guests = len(guests)
        
        return {
            "ok": True, 
            "msg": f"已記錄你的同意，等待其他玩家回應（{agreed_count}/{total_guests}）\n等待中：{', '.join(not_responded)}"
        }

def _handle_conn(conn, addr):
    data = b""
    try:
        timeout_count = 0
        max_timeout = 3
        conn.settimeout(2.0)

        # ✅ 讀到 \n 或多次 timeout 為止
        while timeout_count < max_timeout:
            try:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
                timeout_count = 0
            except socket.timeout:
                timeout_count += 1

        if not data:
            # ⭐ 防止空內容直接 json.loads
            return

        line = data.split(b"\n", 1)[0].decode("utf-8", errors="ignore").strip()

        if not line:
            print(f"[LobbyServer] Received empty message from {addr}", flush=True)
            return

        print(f"[LobbyServer] Received from {addr}: {line[:120]}", flush=True)

        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"[LobbyServer] ✗ JSON decode error from {addr}: {e}", flush=True)
            print(f"[LobbyServer] Raw data (first 200 bytes): {data[:200]}", flush=True)
            try:
                conn.sendall((json.dumps({"ok": False, "error": "Invalid JSON"}, ensure_ascii=False) + "\n").encode("utf-8"))
            except Exception:
                pass
            return

        kind = req.get("kind")

        if kind == "register":
            resp = handle_register(req)
        elif kind == "login":
            resp = handle_login(req)
        elif kind == "list_games":
            resp = handle_list_games(req)
        elif kind == "game_details":
            resp = handle_game_details(req)
        elif kind == "download_game":
            resp = handle_download_game(req)
        elif kind == "list_rooms":
            resp = handle_list_rooms(req)
        elif kind == "create_room":
            resp = handle_create_room(req)
        elif kind == "join_room":
            resp = handle_join_room(req)
        elif kind == "leave_room":
            resp = handle_leave_room(req)
        elif kind == "player_ready":
            resp = handle_player_ready(req)
        elif kind == "player_unready":
            resp = handle_player_unready(req)
        elif kind == "propose_start":
            resp = handle_propose_start(req)
        elif kind == "respond_start":
            resp = handle_respond_start(req)
        elif kind == "logout":
            resp = handle_logout(req)
        elif kind == "rate_game":
            resp = handle_rate_game(req)

        elif kind == "game_finished":
            print(f"[LobbyServer] Processing game_finished: {req}", flush=True)
            resp = handle_game_finished(req)

        elif kind == "subscribe_room":
            resp = handle_subscribe_room(req, conn)
            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
            if resp.get("ok"):
                # ✅ 保持連線作為 SSE 通道
                while True:
                    time.sleep(10)
            return

        else:
            resp = {"ok": False, "error": f"unknown kind: {kind}"}

        conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))

    except Exception as e:
        print(f"[LobbyServer] ✗ Error handling connection from {addr}: {e}", flush=True)
        traceback.print_exc()
        try:
            conn.sendall((json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception:
            pass

    finally:
        try:
            conn.close()
        except Exception:
            pass
