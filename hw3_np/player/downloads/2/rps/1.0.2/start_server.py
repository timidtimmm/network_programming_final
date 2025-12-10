#developer\games\rps\start_server.py
import os, socket, threading, json, time

HOST = os.getenv("GAME_HOST", "127.0.0.1")
PORT = int(os.getenv("GAME_PORT", "0"))
ROOM_ID = os.getenv("ROOM_ID", "room")
print(f"[HB-Server] Starting on {HOST}:{PORT} (room {ROOM_ID})", flush=True)

# 1=石頭, 2=布, 3=剪刀
HAND_CHOICES = [1, 2, 3]
# 1=上, 2=下, 3=左, 4=右
DIR_CHOICES = [1, 2, 3, 4]

players = {}   # name -> {"conn":conn, "hand":None, "dir":None}
# 在全域變數區域添加
player_last_seen = {}  # ✅ 新增這一行
lock = threading.RLock()  # 改用 RLock

game_over = False
game_over_notified = False   # 只通知 lobby 一次就好
pointer_name = None   # 這一輪的指人者
loser_name = None     # 這一輪的被指者

def decide_hand(m1, m2):
    """剪刀石頭布判定: 回傳 0=平手, 1=第一個贏, 2=第二個贏"""
    if m1 == m2:
        return 0
    if (m1 == 1 and m2 == 3) or (m1 == 2 and m2 == 1) or (m1 == 3 and m2 == 2):
        return 1
    return 2

def send(conn, obj):
    try:
        conn.sendall((json.dumps(obj)+"\n").encode())
    except:
        pass

def recv(conn):
    buf = b""
    while True:
        d = conn.recv(1024)
        if not d:
            return None
        buf += d
        if b"\n" in buf:
            line, _ = buf.split(b"\n", 1)
            return json.loads(line.decode("utf-8"))

def get_lobby_connect_host():
    """
    取得用於連線回 Lobby 的實際 IP：
    - 優先使用 LOBBY_CONNECT_HOST（專門用於連線的 IP）
    - 否則使用 LOBBY_HOST，但若為 0.0.0.0 則改用 127.0.0.1
    """
    # 優先使用專門的連線位址
    connect_host = os.getenv("LOBBY_CONNECT_HOST")
    if connect_host:
        return connect_host
    
    # 否則使用 LOBBY_HOST，但需處理 0.0.0.0
    lobby_host = os.getenv("LOBBY_HOST")
    if not lobby_host:
        return None
    
    # 0.0.0.0 無法用於連線，改用 localhost
    if lobby_host == "0.0.0.0":
        return "127.0.0.1"
    
    return lobby_host

def notify_lobby_game_finished():
    """
    告訴 Lobby：這個 ROOM_ID 的一局已經結束（重設為 waiting/idle, 清空 ready）。
    不踢人，只是把房間回復可再準備下一局的狀態。
    """
    lobby_host = get_lobby_connect_host()
    lobby_port = os.getenv("LOBBY_PORT")
    room_id    = os.getenv("ROOM_ID")
    
    if not lobby_host or not lobby_port or not room_id:
        print("[HB-Server] Missing lobby connection info, skip notify", flush=True)
        return
    
    try:
        with socket.create_connection((lobby_host, int(lobby_port)), timeout=2) as s:
            msg = json.dumps({
                "kind": "game_finished",
                "room_id": room_id
                # 不帶 kick_all 或 kick_all=False → 只 reset，不踢人
            }) + "\n"
            s.sendall(msg.encode("utf-8"))
            try:
                s.recv(4096)  # best-effort 接一下回覆
            except Exception:
                pass
        print(f"[HB-Server] Notified lobby reset for {room_id}", flush=True)
    except Exception as e:
        print(f"[HB-Server] notify reset failed: {e}", flush=True)

def notify_lobby_game_finished_kick_all():
    """告訴 Lobby：這一局結束，請立刻把人踢出並關房"""
    lobby_host = get_lobby_connect_host()
    lobby_port = os.getenv("LOBBY_PORT")
    room_id    = os.getenv("ROOM_ID")
    
    if not lobby_host or not lobby_port or not room_id:
        print("[HB-Server] Missing lobby connection info, skip notify kick_all", flush=True)
        return
    
    try:
        with socket.create_connection((lobby_host, int(lobby_port)), timeout=2) as s:
            s.sendall((json.dumps({
                "kind": "game_finished",
                "room_id": room_id,
                "kick_all": True
            }) + "\n").encode("utf-8"))
            try:
                s.recv(4096)  # best-effort
            except Exception:
                pass
        print(f"[HB-Server] Notified lobby kick_all for {room_id}", flush=True)
    except Exception as e:
        print(f"[HB-Server] notify kick_all failed: {e}", flush=True)

# 在 notify_lobby_game_finished_kick_all() 後面新增
def check_player_timeout():
    """檢查玩家是否逾時掉線"""
    TIMEOUT_SEC = 30
    
    with lock:
        if not game_over and len(players) == 2:
            now = time.time()
            names = list(players.keys())[:2]
            
            for name in names:
                last_seen = player_last_seen.get(name, now)
                if now - last_seen > TIMEOUT_SEC:
                    print(f"[HB-Server] {name} timeout, opponent wins", flush=True)
                    
                    winner = names[1] if name == names[0] else names[0]
                    
                    for nm in names:
                        if nm in players:
                            send(players[nm]["conn"], {
                                "msg": "result",
                                "result": "win" if nm == winner else "lose",
                                "winner": winner,
                                "reason": f"{name} disconnected"
                            })
                    
                    globals()['game_over'] = True
                    notify_lobby_game_finished_kick_all()
                    return True
    
    return False

def handle(conn, addr):
    global game_over, pointer_name, loser_name
    name = "?"
    
    try:
        # 1. 握手
        hello = recv(conn)
        if not hello:
            return
        name = hello.get("name", "?")

        with lock:
            players[name] = {"conn": conn, "hand": None, "dir": None}
            player_last_seen[name] = time.time()

        send(conn, {
            "msg": "welcome",
            "room": ROOM_ID,
            "hand_choices": HAND_CHOICES,
            "dir_choices": DIR_CHOICES
        })

        # 2. 等待兩位玩家
        while True:
            with lock:
                if len(players) >= 2:
                    break
            time.sleep(0.1)
        
        send(conn, {"msg": "ready"})

        # 3. 遊戲主迴圈
        while True:
            if game_over:
                break
            
            # ✅ 定期檢查逾時
            if check_player_timeout():
                break

            # ✅ 設定接收逾時
            try:
                conn.settimeout(5.0)
                req = recv(conn)
                
                if not req:
                    print(f"[HB-Server] {name} connection lost")
                    break
                
                # ✅ 更新最後活動時間
                with lock:
                    player_last_seen[name] = time.time()
                
            except socket.timeout:
                continue
            except ConnectionResetError:
                print(f"[HB-Server] {name} connection reset")
                break

            kind = req.get("kind")
            
            # ========== 第一階段：猜拳決定指人者 ==========
            if kind == "hand":
                choice = req.get("choice")
                if choice not in HAND_CHOICES:
                    send(conn, {"msg": "error", "error": "無效的手勢"})
                    continue
                
                with lock:
                    players[name]["hand"] = choice
                    
                    # 檢查是否兩位玩家都出拳了
                    names = list(players.keys())[:2]
                    if all(players[n]["hand"] is not None for n in names):
                        # 兩位都出了，判定結果
                        p1, p2 = names[0], names[1]
                        m1 = players[p1]["hand"]
                        m2 = players[p2]["hand"]
                        
                        result = decide_hand(m1, m2)
                        
                        hands_info = {p1: m1, p2: m2}
                        
                        if result == 0:
                            # 平手，重新猜拳
                            for n in names:
                                players[n]["hand"] = None
                                send(players[n]["conn"], {
                                    "msg": "round",
                                    "phase": "hand",
                                    "result": "draw",
                                    "hands": hands_info
                                })
                        else:
                            # 有勝負
                            winner = p1 if result == 1 else p2
                            loser = p2 if result == 1 else p1
                            
                            pointer_name = winner
                            loser_name = loser
                            
                            # 廣播結果
                            for n in names:
                                send(players[n]["conn"], {
                                    "msg": "round",
                                    "phase": "hand",
                                    "result": "point",
                                    "pointer": pointer_name,
                                    "hands": hands_info
                                })
                            
                            # 重置手勢，準備下一階段
                            for n in names:
                                players[n]["hand"] = None
            
            # ========== 第二階段：指方向 / 轉頭 ==========
            elif kind == "dir":
                choice = req.get("choice")
                if choice not in DIR_CHOICES:
                    send(conn, {"msg": "error", "error": "無效的方向"})
                    continue
                
                with lock:
                    players[name]["dir"] = choice
                    
                    # 檢查是否兩位玩家都選了方向
                    names = list(players.keys())[:2]
                    if all(players[n]["dir"] is not None for n in names):
                        # 兩位都選了，判定結果
                        pointer_dir = players[pointer_name]["dir"]
                        loser_dir = players[loser_name]["dir"]
                        
                        if pointer_dir == loser_dir:
                            # 指中了！遊戲結束
                            game_over = True
                            
                            for n in names:
                                is_winner = (n == pointer_name)
                                send(players[n]["conn"], {
                                    "msg": "result",
                                    "result": "win" if is_winner else "lose",
                                    "winner": pointer_name,
                                    "pointer_dir": pointer_dir,
                                    "loser_dir": loser_dir,
                                    "reason": "指中了！"
                                })
                            
                            # 通知 Lobby 踢人
                            notify_lobby_game_finished_kick_all()
                        else:
                            # 沒指中，重新開始下一輪
                            pointer_name = None
                            loser_name = None
                            
                            for n in names:
                                players[n]["dir"] = None
                                send(players[n]["conn"], {
                                    "msg": "round",
                                    "phase": "dir",
                                    "result": "miss",
                                    "pointer_dir": pointer_dir,
                                    "loser_dir": loser_dir
                                })
            
            if game_over:
                break

        # 4. ✅ 遊戲結束：延遲清理
        if game_over:
            print(f"[HB-Server] Game finished, notifying players...", flush=True)
            
            def delayed_cleanup():
                time.sleep(5)
                
                with lock:
                    if name in players:
                        try:
                            players[name]["conn"].close()
                        except:
                            pass
                        del players[name]
                    
                    if name in player_last_seen:
                        del player_last_seen[name]
                    
                    if len(players) == 0:
                        print("[HB-Server] All players cleaned up, shutting down...", flush=True)
                        os._exit(0)
            
            threading.Thread(target=delayed_cleanup, daemon=True).start()
            
            while True:
                time.sleep(1)
    
    except ConnectionResetError:
        print(f"[HB-Server] {name} connection reset")
    
    finally:
        # 異常清理邏輯（保持原樣）
        try:
            conn.close()
        except:
            pass
        
        with lock:
            if not game_over and name in players:
                del players[name]
                
                if name in player_last_seen:
                    del player_last_seen[name]
                
                if len(players) == 1:
                    remaining = list(players.keys())[0]
                    print(f"[HB-Server] {name} disconnected, {remaining} wins by forfeit", flush=True)
                    
                    try:
                        send(players[remaining]["conn"], {
                            "msg": "result",
                            "result": "win",
                            "winner": remaining,
                            "reason": f"{name} disconnected"
                        })
                        
                        time.sleep(0.5)
                        send(players[remaining]["conn"], {"msg": "game_over"})
                    except:
                        pass
                    
                    game_over = True
                    notify_lobby_game_finished_kick_all()
                    
                    def final_cleanup():
                        time.sleep(5)
                        print("[HB-Server] Shutting down after forfeit...", flush=True)
                        os._exit(0)
                    
                    threading.Thread(target=final_cleanup, daemon=True).start()
            
            elif game_over and len(players) == 0:
                print("[HB-Server] Last player disconnected, shutting down...", flush=True)
                os._exit(0)
                
def serve():
    s = socket.socket()
    s.bind((HOST, PORT))
    s.listen(5)
    print(f"[HB-Server] Listening...", flush=True)
    while True:
        c, a = s.accept()
        print(f"[HB-Server] New connection from {a}", flush=True)
        threading.Thread(target=handle, args=(c, a), daemon=True).start()

if __name__ == "__main__":
    serve()