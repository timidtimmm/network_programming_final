# server/dev_server.py - 完整修正版（含版本驗證 + version_hint）

import os, json, base64, socket, threading, time, traceback, zipfile, io, shutil
from pathlib import Path

from common import db
from common import auth

ROOT = Path(__file__).resolve().parents[1]   # 專案根目錄
SERVER_DIR = Path(__file__).resolve().parent # server/ 資料夾
GAMES_FILE = "games.json"
DEV_USERS_FILE = "dev_users.json"

UPLOADED_DIR = SERVER_DIR / "uploaded_games"

# ----------------- 共用工具：版本處理 ----------------- #

def parse_version(ver: str):
    """
    把 '1.2.3' 轉成 (1,2,3)。
    若格式錯誤 → 回傳 None。
    """
    if not isinstance(ver, str):
        return None
    parts = ver.strip().split(".")
    if len(parts) != 3:
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 for n in nums):
        return None
    return tuple(nums)  # (major, minor, patch)

def version_greater(v_new: str, v_old: str) -> bool:
    """
    v_new 是否嚴格大於 v_old？
    若任一解析失敗，保守回 False。
    """
    t_new = parse_version(v_new)
    t_old = parse_version(v_old)
    if not t_new or not t_old:
        return False
    return t_new > t_old  # Python tuple 比大小

def suggest_next_version(latest: str | None) -> str:
    """
    給現在 latest，建議下一個合法版本號。
    若 latest 不存在或格式錯誤 → 回傳 '1.0.0'。
    正常情況下：patch + 1。
    """
    t = parse_version(latest) if latest else None
    if not t:
        return "1.0.0"
    major, minor, patch = t
    patch += 1
    return f"{major}.{minor}.{patch}"

# ----------------- 初始化 ----------------- #

def ensure_dirs():
    UPLOADED_DIR.mkdir(parents=True, exist_ok=True)

def ensure_user_db():
    users = db.load(DEV_USERS_FILE, {})
    if not isinstance(users, dict):
        users = {}
        db.save(DEV_USERS_FILE, users)

# ----------------- 帳號相關 ----------------- #

def handle_register(payload):
    u = payload.get("username","").strip()
    p = payload.get("password","").strip()
    if not u or not p:
        return {"ok": False, "error": "缺少帳號或密碼"}
    users = db.load(DEV_USERS_FILE, {})
    if u in users:
        return {"ok": False, "error": "帳號已被使用"}
    users[u] = {"password": p}
    db.save(DEV_USERS_FILE, users)
    return {"ok": True, "msg": "註冊成功"}

def handle_login(payload):
    u = payload.get("username","").strip()
    p = payload.get("password","").strip()
    users = db.load(DEV_USERS_FILE, {})

    if u not in users or users[u].get("password") != p:
        return {"ok": False, "error": "帳號或密碼錯誤"}

    token = auth.issue_token(u, role="developer")
    if not token:
        return {"ok": False, "error": "帳號已在其他裝置登入"}

    return {
        "ok": True,
        "token": token,
        "user": u,
        "role": "developer",
        "msg": "登入成功"
    }

def handle_logout(payload):
    token = payload.get("token")
    if token:
        auth.revoke_token(token)
    return {"ok": True, "msg": "已登出"}

# ----------------- 上傳 / 版本管理 ----------------- #

def _extract_upload(name, version, zip_b64):
    try:
        raw = base64.b64decode(zip_b64.encode("utf-8"))
    except Exception as e:
        return False, f"zip base64 解析失敗: {e}"
    dst = UPLOADED_DIR / name / version
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(raw), "r") as z:
            z.extractall(dst)
        print(f"[DevServer] 已解壓遊戲到: {dst}")
        return True, str(dst)
    except Exception as e:
        return False, f"zip 解壓失敗: {e}"

def handle_upload_game(payload):
    """
    上傳/更新遊戲：
      - 版本格式必須是 major.minor.patch（例如 1.0.3）
      - 若遊戲已存在，新的版本號必須「嚴格大於」目前 latest
    """
    token = payload.get("token")
    tokinfo = auth.verify_token(token, role="developer")
    if not tokinfo:
        return {"ok": False, "error": "未登入"}
    developer = tokinfo["user"]

    name = payload.get("name","").strip()
    version = payload.get("version","").strip()
    manifest = payload.get("manifest", {})
    zip_b64 = payload.get("zip_b64","")

    if not name or not version or not manifest or not zip_b64:
        return {"ok": False, "error": "缺少必要欄位"}

    # 1) 檢查版本格式
    if not parse_version(version):
        return {
            "ok": False,
            "error": "版本格式錯誤，需為：major.minor.patch（例如 1.0.3）。",
            "suggested": "1.0.0"
        }

    games = db.load(GAMES_FILE, {})
    if not isinstance(games, dict):
        games = {}

    game = games.get(name)

    if not game:
        # 全新遊戲
        game = {
            "name": name,
            "author": developer,
            "status": "active",
            "versions": {},   # {version_str: {...}}
            "latest": version,
            "reviews": []
        }
    else:
        # 已存在遊戲 → 驗證作者 + 狀態
        if game.get("author") != developer:
            return {"ok": False, "error": "不是此遊戲作者，無法更新"}

        if game.get("status") != "active":
            # 一旦有新版本上傳，將遊戲狀態重新設為 active
            game["status"] = "active"

        current_latest = game.get("latest")
        if current_latest:
            if not parse_version(current_latest):
                # DB 裡有奇怪格式，保守處理：禁止更新，請人工修正
                return {
                    "ok": False,
                    "error": f"目前 DB 中 latest 版本號格式異常：{current_latest}，請聯絡助教或手動修正 games.json。"
                }
            # 版本必須嚴格遞增
            if not version_greater(version, current_latest):
                suggested = suggest_next_version(current_latest)
                return {
                    "ok": False,
                    "error": f"目前最新版本為 {current_latest}，新的版本號必須大於目前版本。",
                    "latest": current_latest,
                    "suggested": suggested
                }

    # 2) 實際解壓 ZIP 檔到 uploaded_games
    ok, msg = _extract_upload(name, version, zip_b64)
    if not ok:
        return {"ok": False, "error": msg}

    # 3) 更新 DB：版本列表 + latest
    if "versions" not in game or not isinstance(game["versions"], dict):
        game["versions"] = {}

    game["versions"][version] = {
        "manifest": manifest,
        "zip_b64": zip_b64
    }
    game["latest"] = version

    games[name] = game
    db.save(GAMES_FILE, games)

    print(f"[DevServer] 遊戲 {name}@{version} 上傳成功，status={game['status']}")
    return {
        "ok": True,
        "msg": "上傳/更新成功",
        "name": name,
        "latest": version,
        "status": game["status"]
    }

# ----------------- 下架 / 查詢遊戲 ----------------- #

def handle_remove_game(payload):
    token = payload.get("token")
    tokinfo = auth.verify_token(token, role="developer")
    if not tokinfo:
        return {"ok": False, "error": "未登入"}
    developer = tokinfo["user"]
    name = payload.get("name","").strip()

    games = db.load(GAMES_FILE, {})
    if name not in games:
        return {"ok": False, "error": "遊戲不存在"}
    game = games[name]
    if game.get("author") != developer:
        return {"ok": False, "error": "無權限下架此遊戲"}

    game["status"] = "removed"
    games[name] = game
    db.save(GAMES_FILE, games)
    return {
        "ok": True,
        "msg": "已下架。此遊戲不再出現在商城列表，且無法建立新房間。",
        "name": name,
        "status": game["status"]
    }

def handle_my_games(payload):
    """
    回傳開發者自己的遊戲列表，並把版本排序好（從小到大）。
    ⭐ 只回傳精簡資訊，不包含完整 manifest 和 zip_b64。
    """
    token = payload.get("token")
    tokinfo = auth.verify_token(token, role="developer")
    if not tokinfo:
        return {"ok": False, "error": "未登入"}
    developer = tokinfo["user"]
    games = db.load(GAMES_FILE, {})
    if not isinstance(games, dict):
        games = {}

    mine = {k:v for k,v in games.items() if v.get("author")==developer}

    # 版本排序 + 精簡輸出
    result = {}
    for name, info in mine.items():
        vers = info.get("versions", {}) or {}
        # 用 parse_version 排序，解析失敗的就丟到最後
        sorted_keys = sorted(
            vers.keys(),
            key=lambda x: parse_version(x) or (999999, 999999, 999999)
        )
        
        # ⭐ 只保留版本號和 display_name
        simplified_versions = {}
        for k in sorted_keys:
            ver_info = vers[k]
            manifest = ver_info.get("manifest", {})
            simplified_versions[k] = {
                "display_name": manifest.get("display_name", name),
                "type": manifest.get("type", "Unknown"),
                "max_players": manifest.get("max_players", 2)
            }
        
        result[name] = {
            "status": info.get("status", "active"),
            "latest": info.get("latest"),
            "versions": simplified_versions,
            "avg_rating": info.get("avg_rating"),
            "review_count": info.get("review_count", 0)
        }

    return {"ok": True, "games": result}

def handle_version_hint(payload):
    """
    查詢指定遊戲目前 latest + 建議下一個版本號。
    給 developer_client 在輸入版本前先提示用。
    """
    token = payload.get("token")
    tokinfo = auth.verify_token(token, role="developer")
    if not tokinfo:
        return {"ok": False, "error": "未登入"}
    developer = tokinfo["user"]

    name = payload.get("name","").strip()
    if not name:
        return {"ok": False, "error": "缺少遊戲名稱"}

    games = db.load(GAMES_FILE, {})
    if not isinstance(games, dict):
        games = {}

    game = games.get(name)
    if not game:
        # 完全沒有這款遊戲 → 視為新遊戲
        return {
            "ok": True,
            "exists": False,
            "name": name,
            "latest": None,
            "suggested": "1.0.0"
        }

    if game.get("author") != developer:
        return {"ok": False, "error": "你不是此遊戲作者"}

    latest = game.get("latest")
    suggested = suggest_next_version(latest)

    vers = game.get("versions", {}) or {}
    sorted_versions = sorted(
        vers.keys(),
        key=lambda x: parse_version(x) or (999999, 999999, 999999)
    )

    return {
        "ok": True,
        "exists": True,
        "name": name,
        "latest": latest,
        "suggested": suggested,
        "versions": sorted_versions,
        "status": game.get("status", "active")
    }

# ----------------- Server 迴圈 ----------------- #

def _handle_conn(conn, addr):
    try:
        data = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        if not data:
            conn.close()
            return

        line = data.split(b"\n",1)[0].decode("utf-8")
        req = json.loads(line)
        kind = req.get("kind")

        if kind == "register":
            resp = handle_register(req)
        elif kind == "login":
            resp = handle_login(req)
        elif kind == "upload_game":
            resp = handle_upload_game(req)
        elif kind == "remove_game":
            resp = handle_remove_game(req)
        elif kind == "logout":
            resp = handle_logout(req)
        elif kind == "my_games":
            resp = handle_my_games(req)
        elif kind == "version_hint":
            resp = handle_version_hint(req)
        else:
            resp = {"ok": False, "error": f"unknown kind: {kind}"}

        conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
    except Exception as e:
        traceback.print_exc()
        try:
            conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode("utf-8"))
        except Exception:
            pass
    finally:
        conn.close()

def serve(host, port, stop_event=None):
    ensure_user_db()
    ensure_dirs()

    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(128)
    s.settimeout(0.5)

    print(f"[DevServer] listening on {host}:{s.getsockname()[1]}")

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                print("[DevServer] stop_event set, exiting serve loop.")
                break

            try:
                conn, addr = s.accept()
            except socket.timeout:
                continue
            except OSError as e:
                print(f"[DevServer] Socket closed / error: {e}")
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
        print(f"[DevServer] Shutdown complete on {host}:{port}")
