# developer/developer_client.py - 穩定版（自動判斷連線目標 + 版本防呆）

import os, sys, json, asyncio, base64, zipfile, io, socket, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.load(open(ROOT / "config.json", "r", encoding="utf-8"))

SERVER_IP = CONFIG.get("server_ip") or ""

DEV_DIR = Path(__file__).resolve().parent         # developer/
GAMES_ROOT = DEV_DIR / "games"                    # developer/games
GAMES_ROOT.mkdir(parents=True, exist_ok=True)

_runtime_path = ROOT / "server" / "runtime_ports.json"
if _runtime_path.exists():
    SERVER_RUNTIME = json.load(open(_runtime_path, "r", encoding="utf-8"))
else:
    SERVER_RUNTIME = {}

# --------- 版本格式檢查：major.minor.patch ---------
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

def validate_version(version: str):
    if not version or not isinstance(version, str):
        return False, "版本號不能為空。"
    if not VERSION_RE.match(version):
        return False, "版本格式錯誤，需為：major.minor.patch（例如 1.0.3）。"
    return True, ""


def _pick_dev_target():
    endpoint_cfg = CONFIG.get("developer_endpoint", {})
    default_port = endpoint_cfg.get("port", 5501)

    # 若 config.json 有指定 server_ip，優先使用
    if SERVER_IP:
        return SERVER_IP, default_port

    # 若 runtime_ports.json 中有 developer_port，優先嘗試連到 localhost:developer_port
    if SERVER_RUNTIME:
        port = SERVER_RUNTIME.get("developer_port") or default_port
        try:
            s = socket.socket()
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            s.close()
            return "127.0.0.1", port
        except OSError:
            # 若 localhost 連不上，就退回 runtime_ports.json 的 host 或 config 的 host
            host = SERVER_RUNTIME.get("dev_host") or endpoint_cfg.get("host", "127.0.0.1")
            return host, port

    # 沒有 runtime_ports.json，就使用 config.json 的 developer_endpoint
    host = endpoint_cfg.get("host", "127.0.0.1")
    port = endpoint_cfg.get("port", 5501)
    return host, port

DEV_HOST, DEV_PORT = _pick_dev_target()

# 目前登入中的 developer token（提供中斷時在 main() 做額外清理用）
CURRENT_TOKEN = None


async def _read_json_line(reader: asyncio.StreamReader) -> dict:
    """
    手動累積直到遇到 '\\n'，避免 StreamReader.readline 的內建限制。
    一般情況 server 都會一行一個 JSON。
    """
    buf = b""
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            if not buf:
                raise EOFError("server closed connection with no data")
            break
        buf += chunk
        if b"\n" in buf:
            line, _ = buf.split(b"\n", 1)
            break
    return json.loads(line.decode("utf-8"))


async def send_req(obj: dict):
    reader, writer = await asyncio.open_connection(DEV_HOST, DEV_PORT)
    line = json.dumps(obj) + "\n"
    writer.write(line.encode("utf-8"))
    await writer.drain()
    # 使用自訂的 _read_json_line，避免回傳訊息過長的問題
    resp_obj = await _read_json_line(reader)
    writer.close()
    await writer.wait_closed()
    return resp_obj


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def ask_choice(prompt: str, valid: set[str]) -> str:
    while True:
        try:
            c = input(prompt).strip()
        except EOFError:
            # 讓上層 main() 統一處理 EOF / Ctrl+Z 等狀況
            raise
        if c in valid:
            return c
        print("無效的指令，請輸入：", "/".join(sorted(valid)))


async def async_main():
    global CURRENT_TOKEN

    # ⭐ 外層 while True：支援「登出後回到登入畫面」
    while True:
        token = None
        developer = None
        CURRENT_TOKEN = None

        # ---------- 登入選單 ----------
        while token is None:
            clear_screen()
            print("=== 開發者平台登入 ===")
            print(f"(目前 Developer Server: {DEV_HOST}:{DEV_PORT})")
            print("1) 註冊")
            print("2) 登入")
            print("3) 離開")
            c = ask_choice("請選擇 (1-3): ", set("123"))

            if c == "1":
                u = input("帳號: ").strip()
                p = input("密碼: ").strip()
                resp = await send_req({"kind": "register", "username": u, "password": p})
                print(resp)
                input("\n(按 Enter 繼續) ")
            elif c == "2":
                u = input("帳號: ").strip()
                p = input("密碼: ").strip()
                resp = await send_req({"kind": "login", "username": u, "password": p})
                if resp.get("ok"):
                    token = resp["token"]
                    CURRENT_TOKEN = token
                    developer = u
                    print("登入成功")
                    input("\n(按 Enter 繼續) ")
                else:
                    print(resp)
                    input("\n(按 Enter 繼續) ")
            else:
                # 選擇離開整個 developer client
                return

        # ---------- 主選單 ----------
        while token is not None:
            clear_screen()
            print("=== 開發者主選單 ===")
            print(f"(Developer Server: {DEV_HOST}:{DEV_PORT})")
            print("1) 上傳/更新遊戲")
            print("2) 查看我的遊戲")
            print("3) 下架遊戲")
            print("4) 登出")
            print("5) 離開")
            choice = ask_choice("請選擇 (1-5): ", set("12345"))

            # 1) 上傳 / 更新
            if choice == "1":
                game_name = input("遊戲名稱: ").strip()
                if not game_name:
                    print("❌ 遊戲名稱不可空白")
                    input("\n(按 Enter 繼續) ")
                    continue

                # 先問 server：這款遊戲目前 latest 是啥？建議下一版？
                hint = await send_req({
                    "kind": "version_hint",
                    "token": token,
                    "name": game_name
                })

                if not hint.get("ok"):
                    print("✗ 無法取得版本資訊：", hint.get("error"))
                    input("\n(按 Enter 繼續) ")
                    continue

                if not hint.get("exists"):
                    print(f"📦 這是一款新遊戲：{game_name}")
                    print("   建議初始版本號：1.0.0")
                    suggested = "1.0.0"
                else:
                    latest = hint.get("latest")
                    suggested = hint.get("suggested", "1.0.0")
                    print(f"📦 遊戲 {game_name} 目前最新版本為：{latest}")
                    print(f"   建議下一個版本號：{suggested}")
                    vers = hint.get("versions") or []
                    if vers:
                        print(f"   目前已有版本列表：{vers}")

                # 準備遊戲資料夾 / manifest
                game_dir = GAMES_ROOT / game_name
                manifest_path = game_dir / "manifest.json"

                if not game_dir.exists():
                    print("❌ 找不到遊戲資料夾：", game_dir)
                    input("\n(按 Enter 繼續) ")
                    continue

                if not manifest_path.exists():
                    print("❌ 遊戲資料夾缺少 manifest.json：", manifest_path)
                    input("\n(按 Enter 繼續) ")
                    continue

                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except Exception as e:
                    print(f"讀取 manifest.json 失敗：{e}")
                    input("\n(按 Enter 繼續) ")
                    continue

                # 先把整個資料夾壓成 zip（只做一次）
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for path in game_dir.rglob("*"):
                        if path.is_file():
                            rel = path.relative_to(game_dir)
                            z.write(path, rel.as_posix())
                zip_bytes = buf.getvalue()
                zip_b64 = base64.b64encode(zip_bytes).decode("utf-8")

                # 進入版本號輸入迴圈
                while True:
                    ver_input = input(f"版本號（例如 1.0.0；直接 Enter 使用建議值 {suggested}）: ").strip()
                    if not ver_input:
                        version = suggested
                        print(f"→ 使用版本號：{version}")
                    else:
                        version = ver_input

                    print(f"\n正在上傳 {game_name}@{version} ...")
                    resp = await send_req({
                        "kind": "upload_game",
                        "token": token,
                        "name": game_name,
                        "version": version,
                        "manifest": manifest,
                        "zip_b64": zip_b64
                    })

                    if resp.get("ok"):
                        print(f"✓ 上傳成功：{resp.get('name')} 最新版 {resp.get('latest')} (status={resp.get('status')})")
                        input("\n(按 Enter 繼續) ")
                        break  # 離開版本號輸入迴圈

                    # 失敗情況 → 顯示錯誤與建議
                    err = resp.get("error", "未知錯誤")
                    print("✗ 上傳失敗：", err)

                    latest = resp.get("latest")
                    suggested2 = resp.get("suggested")
                    if latest and suggested2:
                        print(f"  目前最新版本為 {latest}，建議下一個可用版本號：{suggested2}")
                        suggested = suggested2  # 更新建議值

                    retry = ask_choice("要重新輸入版本號並重試嗎？(y/n): ", set(["y", "Y", "n", "N"]))
                    if retry.lower() != "y":
                        break

                # 回到主選單
                continue

            # 2) 查看我的遊戲
            elif choice == "2":
                resp = await send_req({"kind": "my_games", "token": token})
                if resp.get("ok"):
                    games = resp.get("games", {})
                    if not games:
                        print("你還沒有上傳任何遊戲")
                    else:
                        for name, info in games.items():
                            print(f"\n{'='*50}")
                            print(f"遊戲：{name}")
                            print(f"  狀態：{info.get('status')}")
                            print(f"  最新版本：{info.get('latest')}")

                            # ⭐ 顯示版本號列表
                            versions = info.get('versions', {})
                            version_list = list(versions.keys())
                            print(f"  版本列表：{version_list}")

                            # ⭐ 顯示每個版本的詳細資訊
                            if versions:
                                print(f"  版本詳情：")
                                for ver, ver_info in versions.items():
                                    display_name = ver_info.get('display_name', name)
                                    game_type = ver_info.get('type', 'Unknown')
                                    max_players = ver_info.get('max_players', '?')
                                    print(f"    - {ver}: {display_name} [{game_type}, {max_players}人]")

                            # ⭐ 顯示評分（如果有）
                            avg = info.get('avg_rating')
                            count = info.get('review_count', 0)
                            if avg:
                                print(f"  評分：{avg} ⭐ ({count} 則評論)")
                            else:
                                print(f"  評分：尚無評論")

                            print(f"{'='*50}")
                else:
                    print(resp)
                input("\n(按 Enter 繼續) ")

            # 3) 下架遊戲
            elif choice == "3":
                game_name = input("要下架的遊戲名稱: ").strip()
                resp = await send_req({
                    "kind": "remove_game",
                    "token": token,
                    "name": game_name
                })
                print(resp)
                input("\n(按 Enter 繼續) ")

            # 4) 登出 → 回登入畫面，而不是離開程式
            elif choice == "4":
                if token is not None:
                    resp = await send_req({"kind": "logout", "token": token})
                    print(resp.get("msg", "已登出"))
                token = None
                CURRENT_TOKEN = None
                developer = None
                input("\n(按 Enter 返回登入介面) ")
                # 跳出「主選單 while」，回到外層 while True，重新進登入選單
                break

            # 5) 離開程式
            elif choice == "5":
                if token is not None:
                    try:
                        await send_req({"kind": "logout", "token": token})
                    except Exception:
                        # 若 server 已掛掉，就算了，離開即可
                        pass
                CURRENT_TOKEN = None
                print("再見～")
                return
        # end of 「主選單 while」，如果是因為選 4) 登出，就會回到外層 while True，重新顯示登入選單


def main():
    global CURRENT_TOKEN

    try:
        asyncio.run(async_main())
    except (KeyboardInterrupt, EOFError):
        # 這裡負責處理 Ctrl+C / EOF 的「最後收尾」
        if CURRENT_TOKEN is None:
            # 代表沒有登入，或已正常登出，不需要額外處理
            print("\n[系統] 再見！")
            return

        async def _cleanup():
            global CURRENT_TOKEN
            try:
                print("\n[系統] 正在釋放 token...")
                resp = await send_req({"kind": "logout", "token": CURRENT_TOKEN})
                if resp.get("ok"):
                    print("[系統] 已成功登出並釋放 token")
                else:
                    print(f"[系統] 登出回應：{resp}")
            except Exception as e:
                print(f"[系統] 登出時發生錯誤（server 可能已關閉）：{e}")
            finally:
                CURRENT_TOKEN = None
                print("[系統] 再見！")

        # asyncio.run() 在前一個 loop 已經結束後，可以再次呼叫
        asyncio.run(_cleanup())


if __name__ == "__main__":
    main()
