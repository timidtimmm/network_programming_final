# developer/games/threeplayer_rps/start_server.py - 完整版本
import os, socket, threading, json, time

HOST = os.getenv("GAME_HOST", "127.0.0.1")
PORT = int(os.getenv("GAME_PORT", "0"))
ROOM_ID = os.getenv("ROOM_ID", "room")
print(f"[RPS3-Server] Starting on {HOST}:{PORT} (room {ROOM_ID})", flush=True)

HAND_CHOICES = [1, 2, 3]  # 1=石頭, 2=布, 3=剪刀

players = {}  # name -> {"conn":conn, "hand":None, "eliminated":False}
player_last_seen = {}
lock = threading.RLock()
game_over = False

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

def decide_hand(m1, m2):
    """1=石頭, 2=布, 3=剪刀
    回傳: 0=平手, 1=第一個勝, 2=第二個勝"""
    if m1 == m2:
        return 0
    if (m1 == 1 and m2 == 3) or (m1 == 2 and m2 == 1) or (m1 == 3 and m2 == 2):
        return 1
    return 2

def try_resolve_round_if_ready():
    """玩家狀態改變（掉線/淘汰/出拳）時，嘗試結算當前輪次"""
    global game_over

    with lock:
        if game_over:
            return

        active = get_active_players()

        # 只剩一人 → 直接勝利
        if len(active) == 1:
            winner = active[0]
            for nm in players.keys():
                send(players[nm]["conn"], {
                    "msg": "result",
                    "result": "win" if nm == winner else "lose",
                    "winner": winner,
                    "reason": "Last player standing"
                })
            game_over = True
            notify_lobby_game_finished_kick_all()
            return

        # 兩人局：若兩人都已出拳 → 立刻判定
        if len(active) == 2:
            hands = {n: players[n]["hand"] for n in active if players[n]["hand"] is not None}
            if len(hands) != 2:
                return

            result = judge_two_players(hands)

            if result["result"] == "draw":
                for nm in active:
                    send(players[nm]["conn"], {
                        "msg": "round",
                        "result": "draw",
                        "hands": hands
                    })
                    players[nm]["hand"] = None
                return

            if result["result"] == "winner":
                winner = result["winner"]
                for nm in players.keys():
                    send(players[nm]["conn"], {
                        "msg": "result",
                        "result": "win" if nm == winner else "lose",
                        "winner": winner,
                        "hands": hands
                    })
                game_over = True
                notify_lobby_game_finished_kick_all()
                return

        # 三人局：若三人都已出拳 → 立刻判定
        if len(active) == 3:
            hands = {n: players[n]["hand"] for n in active if players[n]["hand"] is not None}
            if len(hands) != 3:
                return

            result = judge_three_players(hands)

            if result["result"] == "draw":
                for nm in active:
                    send(players[nm]["conn"], {
                        "msg": "round",
                        "result": "draw",
                        "hands": hands,
                        "reason": result.get("reason", "")
                    })
                    players[nm]["hand"] = None
                return

            if result["result"] == "winner":
                winner = result["winner"]
                eliminated = result.get("eliminated", [])
                for nm in eliminated:
                    if nm in players:
                        players[nm]["eliminated"] = True

                for nm in players.keys():
                    send(players[nm]["conn"], {
                        "msg": "result",
                        "result": "win" if nm == winner else "lose",
                        "winner": winner,
                        "hands": hands,
                        "reason": result.get("reason", "")
                    })

                game_over = True
                notify_lobby_game_finished_kick_all()
                return

            if result["result"] == "eliminate":
                eliminated = result.get("eliminated", [])
                for nm in eliminated:
                    if nm in players:
                        players[nm]["eliminated"] = True

                for nm in active:
                    send(players[nm]["conn"], {
                        "msg": "round",
                        "result": "eliminate",
                        "hands": hands,
                        "eliminated": eliminated,
                        "reason": result.get("reason", "")
                    })
                    players[nm]["hand"] = None

                return

def judge_three_players(hands):
    """
    判定三人剪刀石頭布結果
    hands: {name: hand_value, ...}
    回傳: {"result": "draw" | "eliminate" | "winner", "eliminated": [names], "winner": name, "reason": str}
    
    規則：
    - 三人相同 → 平手
    - 三種都有 → 平手
    - 只有兩種手勢：
      - 如果有 2 人輸 → 雙殺，遊戲直接結束，1 人獲勝
      - 如果有 1 人輸 → 淘汰該人，剩餘 2 人繼續
    """
    names = list(hands.keys())
    if len(names) != 3:
        return {"result": "error", "reason": "Not enough players"}
    
    values = list(hands.values())
    unique_values = set(values)
    
    # 三人出相同 → 平手
    if len(unique_values) == 1:
        return {"result": "draw", "reason": "All same"}
    
    # 三種都有 → 平手
    if len(unique_values) == 3:
        return {"result": "draw", "reason": "Rock-Paper-Scissors all present"}
    
    # ✅ 只有兩種手勢 → 判定勝負
    hand_map = {1: "Rock", 2: "Paper", 3: "Scissors"}
    
    # 找出哪個手勢是輸家
    losing_hand = None
    if 1 in unique_values and 2 in unique_values:  # 石頭 vs 布 → 石頭輸
        losing_hand = 1
    elif 2 in unique_values and 3 in unique_values:  # 布 vs 剪刀 → 布輸
        losing_hand = 2
    elif 3 in unique_values and 1 in unique_values:  # 剪刀 vs 石頭 → 剪刀輸
        losing_hand = 3
    
    # 計算輸家人數
    losers = [name for name, hand in hands.items() if hand == losing_hand]
    winners = [name for name, hand in hands.items() if hand != losing_hand]
    
    # ✅ 關鍵修改：如果有 2 人輸 → 雙殺，直接結束遊戲
    if len(losers) == 2:
        return {
            "result": "winner",
            "winner": winners[0],
            "eliminated": losers,
            "losing_hand": hand_map[losing_hand],
            "reason": f"Double kill! {winners[0]} wins by eliminating both opponents with {hand_map[hands[winners[0]]]}"
        }
    
    # ✅ 只有 1 人輸 → 淘汰該人，進入 2 人對決
    elif len(losers) == 1:
        return {
            "result": "eliminate",
            "eliminated": losers,
            "losing_hand": hand_map[losing_hand],
            "reason": f"{hand_map[losing_hand]} eliminated"
        }
    
    # 理論上不會到這裡
    return {"result": "error", "reason": "Unexpected game state"}

def judge_two_players(hands):
    """兩人對決"""
    names = list(hands.keys())
    if len(names) != 2:
        return {"result": "error"}
    
    a, b = names[0], names[1]
    result = decide_hand(hands[a], hands[b])
    
    if result == 0:
        return {"result": "draw"}
    elif result == 1:
        return {"result": "winner", "winner": a}
    else:
        return {"result": "winner", "winner": b}

def get_active_players():
    """取得未淘汰的玩家"""
    with lock:
        return [name for name, p in players.items() if not p.get("eliminated", False)]

def check_player_timeout():
    """檢查玩家逾時"""
    TIMEOUT_SEC = 30
    
    with lock:
        if game_over:
            return False
        
        active = get_active_players()
        if len(active) < 2:
            return False
        
        now = time.time()
        for name in active:
            last_seen = player_last_seen.get(name, now)
            if now - last_seen > TIMEOUT_SEC:
                print(f"[RPS3-Server] {name} timeout, eliminated", flush=True)
                
                players[name]["eliminated"] = True
                
                for nm in active:
                    if nm != name and nm in players:
                        send(players[nm]["conn"], {
                            "msg": "player_eliminated",
                            "name": name,
                            "reason": "timeout"
                        })
                
                remaining = get_active_players()
                if len(remaining) == 1:
                    winner = remaining[0]
                    print(f"[RPS3-Server] {winner} wins (others eliminated)", flush=True)
                    
                    for nm in players.keys():
                        if nm in players:
                            send(players[nm]["conn"], {
                                "msg": "result",
                                "result": "win" if nm == winner else "lose",
                                "winner": winner,
                                "reason": "opponents eliminated"
                            })
                    
                    globals()['game_over'] = True
                    notify_lobby_game_finished_kick_all()
                
                return True
    
    return False

def get_lobby_connect_host():
    connect_host = os.getenv("LOBBY_CONNECT_HOST")
    if connect_host:
        return connect_host
    lobby_host = os.getenv("LOBBY_HOST")
    if not lobby_host:
        return None
    if lobby_host == "0.0.0.0":
        return "127.0.0.1"
    return lobby_host

def notify_lobby_game_finished_kick_all():
    lobby_host = get_lobby_connect_host()
    lobby_port = os.getenv("LOBBY_PORT")
    room_id = os.getenv("ROOM_ID")
    
    if not lobby_host or not lobby_port or not room_id:
        return
    
    try:
        with socket.create_connection((lobby_host, int(lobby_port)), timeout=2) as s:
            s.sendall((json.dumps({
                "kind": "game_finished",
                "room_id": room_id,
                "kick_all": True
            }) + "\n").encode("utf-8"))
            try:
                s.recv(4096)
            except Exception:
                pass
        print(f"[RPS3-Server] Notified lobby kick_all for {room_id}", flush=True)
    except Exception as e:
        print(f"[RPS3-Server] notify kick_all failed: {e}", flush=True)

def handle(conn, addr):
    global game_over
    name = "?"
    
    try:
        # 1. 握手
        hello = recv(conn)
        if not hello:
            return
        name = hello.get("name", "?")

        with lock:
            players[name] = {"conn": conn, "hand": None, "eliminated": False}
            player_last_seen[name] = time.time()

        send(conn, {
            "msg": "welcome",
            "room": ROOM_ID,
            "hand_choices": HAND_CHOICES,
            "max_players": 3
        })

        # 2. 等待三位玩家
        while True:
            with lock:
                if len(players) >= 3:
                    break
            time.sleep(0.1)
        
        send(conn, {"msg": "ready", "players": list(players.keys())})

        # 3. 遊戲主迴圈
        while True:
            if game_over:
                break
            
            if check_player_timeout():
                break

            # 等待出拳
            try:
                conn.settimeout(5.0)
                req = recv(conn)
                
                if not req:
                    print(f"[RPS3-Server] {name} connection lost")
                    break
                
                with lock:
                    player_last_seen[name] = time.time()
                
            except socket.timeout:
                continue
            except ConnectionResetError:
                break

            if req.get("kind") != "hand":
                continue
            
            try:
                mv = int(req.get("choice"))
            except:
                send(conn, {"msg": "error", "error": "Invalid hand"})
                continue
            
            if mv not in HAND_CHOICES:
                send(conn, {"msg": "error", "error": "Invalid hand value"})
                continue
            
            # 紀錄出拳
            with lock:
                if name not in players or players[name].get("eliminated"):
                    continue
                
                players[name]["hand"] = mv
                
                # 檢查所有活躍玩家是否都出拳了
                active = get_active_players()
                hands = {n: players[n]["hand"] for n in active if players[n]["hand"] is not None}
                
                if len(hands) != len(active):
                    continue
                
                # ✅ 所有人都出拳了，判定結果
                if len(active) == 3:
                    # 三人模式
                    result = judge_three_players(hands)
                    
                    if result["result"] == "draw":
                        # 平手，重新出拳
                        for nm in active:
                            send(players[nm]["conn"], {
                                "msg": "round",
                                "result": "draw",
                                "hands": hands,
                                "reason": result["reason"]
                            })
                            players[nm]["hand"] = None
                    
                    elif result["result"] == "winner":
                        # ✅ 新增：雙殺直接獲勝
                        winner = result["winner"]
                        eliminated = result["eliminated"]
                        
                        print(f"[RPS3-Server] Double kill! {winner} wins the game!", flush=True)
                        
                        # 標記淘汰者
                        for nm in eliminated:
                            players[nm]["eliminated"] = True
                        
                        # 通知所有人遊戲結束
                        for nm in players.keys():
                            send(players[nm]["conn"], {
                                "msg": "result",
                                "result": "win" if nm == winner else "lose",
                                "winner": winner,
                                "hands": hands,
                                "reason": result["reason"]
                            })
                        
                        game_over = True
                        notify_lobby_game_finished_kick_all()
                        
                        time.sleep(0.5)
                        for nm in players.keys():
                            send(players[nm]["conn"], {"msg": "game_over"})
                    
                    elif result["result"] == "eliminate":
                        # 有人被淘汰（單殺），進入 2 人模式
                        eliminated = result["eliminated"]
                        
                        for nm in eliminated:
                            players[nm]["eliminated"] = True
                        
                        # 通知所有人
                        for nm in active:
                            send(players[nm]["conn"], {
                                "msg": "round",
                                "result": "eliminate",
                                "hands": hands,
                                "eliminated": eliminated,
                                "reason": result["reason"]
                            })
                            players[nm]["hand"] = None
                
                elif len(active) == 2:
                    # 兩人模式（單殺後的狀態）
                    result = judge_two_players(hands)
                    
                    if result["result"] == "draw":
                        for nm in active:
                            send(players[nm]["conn"], {
                                "msg": "round",
                                "result": "draw",
                                "hands": hands
                            })
                            players[nm]["hand"] = None
                    
                    elif result["result"] == "winner":
                        winner = result["winner"]
                        
                        # 遊戲結束
                        for nm in players.keys():
                            send(players[nm]["conn"], {
                                "msg": "result",
                                "result": "win" if nm == winner else "lose",
                                "winner": winner,
                                "hands": hands
                            })
                        
                        game_over = True
                        notify_lobby_game_finished_kick_all()
                        
                        time.sleep(0.5)
                        for nm in players.keys():
                            send(players[nm]["conn"], {"msg": "game_over"})
                
                elif len(active) == 1:
                    # 只剩一人（其他人都掉線），直接獲勝
                    winner = active[0]
                    
                    for nm in players.keys():
                        send(players[nm]["conn"], {
                            "msg": "result",
                            "result": "win" if nm == winner else "lose",
                            "winner": winner,
                            "reason": "Last player standing"
                        })
                    
                    game_over = True
                    notify_lobby_game_finished_kick_all()

            if game_over:
                break

        # 4. 遊戲結束清理
        if game_over:
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
                        print("[RPS3-Server] Shutting down...", flush=True)
                        os._exit(0)
            
            threading.Thread(target=delayed_cleanup, daemon=True).start()
            
            while True:
                time.sleep(1)
    
    except ConnectionResetError:
        print(f"[RPS3-Server] {name} connection reset")
    
    finally:
        try:
            conn.close()
        except:
            pass
        
        with lock:
            if not game_over and name in players:
                # 遊戲中掉線 → 標記淘汰
                players[name]["eliminated"] = True
                
                if name in player_last_seen:
                    del player_last_seen[name]
                
                active = get_active_players()
                
                # 通知其他人
                for nm in active:
                    if nm in players:
                        send(players[nm]["conn"], {
                            "msg": "player_eliminated",
                            "name": name,
                            "reason": "disconnected"
                        })
                
                try_resolve_round_if_ready()
            
            elif game_over and len(players) == 0:
                os._exit(0)

def serve():
    s = socket.socket()
    s.bind((HOST, PORT))
    s.listen(5)
    print(f"[RPS3-Server] Listening...", flush=True)
    while True:
        c, a = s.accept()
        print(f"[RPS3-Server] New connection from {a}", flush=True)
        threading.Thread(target=handle, args=(c, a), daemon=True).start()

if __name__ == "__main__":

    serve()
