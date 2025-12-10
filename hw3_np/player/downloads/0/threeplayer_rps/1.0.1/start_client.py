import os, socket, json, sys, time
import atexit
import signal

HOST = os.getenv("GAME_HOST", "127.0.0.1")
PORT = int(os.getenv("GAME_PORT", "0"))
PLAYER = os.getenv("PLAYER_NAME", "player")
print(f"[RPS3-Client] connecting to {HOST}:{PORT} as {PLAYER}", flush=True)

HAND_CHOICES = ["1", "2", "3"]

def send(conn, obj):
    conn.sendall((json.dumps(obj) + "\n").encode())

def recv(conn):
    buf = b""
    while True:
        try:
            d = conn.recv(1024)
        except ConnectionResetError:
            return None
        if not d:
            return None
        buf += d
        if b"\n" in buf:
            line, _ = buf.split(b"\n", 1)
            return json.loads(line.decode("utf-8"))

def ask_choice(prompt, valid):
    while True:
        try:
            c = input(prompt).strip()
            if c in valid:
                return c
        except KeyboardInterrupt:
            print("\n[Client] 偵測到 Ctrl+C，正在退出...")
            raise

def main():
    s = None
    eliminated = False
    
    def cleanup():
        if s:
            try:
                s.close()
            except:
                pass
        print("已離開遊戲。")
    
    atexit.register(cleanup)
    
    def signal_handler(sig, frame):
        print(f"\n[Client] 收到中斷訊號，正在退出...")
        cleanup()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    s = socket.socket()
    
    try:
        s.connect((HOST, PORT))
    except Exception as e:
        print("連線失敗:", e)
        return

    try:
        # 握手
        send(s, {"name": PLAYER})
        hello = recv(s)
        if not hello:
            print("無回應")
            return

        print("進入房間:", hello.get("room"))
        print("手勢編號: 1=石頭, 2=布, 3=剪刀")
        print("等待其他玩家加入... (需要 3 人)")

        ready = recv(s)
        if not ready or ready.get("msg") != "ready":
            print("等待失敗")
            return

        players = ready.get("players", [])
        print(f"所有玩家已就緒：{', '.join(players)}")
        print("開始遊戲！")
        print("(按 Ctrl+C 可隨時退出)\n")

        game_finished = False

        while not game_finished:
            if eliminated:
                # 已被淘汰，只接收訊息
                msg = recv(s)
                if not msg:
                    print("伺服器中斷")
                    return
                
                if msg.get("msg") == "result":
                    res = msg.get("result")
                    winner = msg.get("winner")
                    reason = msg.get("reason", "")
                    
                    print(f"\n最終結果：winner={winner}")
                    if reason:
                        print(f"原因：{reason}")
                    
                    if res == "win":
                        print("🎉 你贏了！")
                    else:
                        print(f"😢 你輸了！")
                    
                    game_finished = True
                    print("5 秒後自動關閉視窗...")
                    time.sleep(5)
                    break
                
                elif msg.get("msg") == "game_over":
                    game_finished = True
                    break
                
                continue
            
            # 出拳
            mv = ask_choice("請輸入手勢 (1=石頭, 2=布, 3=剪刀): ", HAND_CHOICES)
            send(s, {"kind": "hand", "choice": int(mv)})
            print("✅ 你已決定出拳，等待其他玩家...", flush=True)

            # 等待結果
            while True:
                msg = recv(s)
                if not msg:
                    print("伺服器中斷")
                    return

                if msg.get("msg") == "round":
                    print("\n✅ 所有玩家都出拳了！", flush=True)
                    
                    hands = msg.get("hands", {})
                    result = msg.get("result")
                    
                    print(f"各玩家手勢：{hands}")
                    
                    if result == "draw":
                        reason = msg.get("reason", "")
                        print(f"本輪平手：{reason}")
                        print("重新出拳！\n")
                        break
                    
                    elif result == "eliminate":
                        eliminated_players = msg.get("eliminated", [])
                        reason = msg.get("reason", "")
                        
                        print(f"淘汰結果：{', '.join(eliminated_players)} 被淘汰")
                        print(f"原因：{reason}")
                        
                        if PLAYER in eliminated_players:
                            print("💀 你被淘汰了，等待遊戲結束...\n")
                            eliminated = True
                        else:
                            print("✅ 你還存活！繼續下一輪\n")
                        
                        break

                elif msg.get("msg") == "player_eliminated":
                    elim_name = msg.get("name")
                    reason = msg.get("reason", "")
                    print(f"⚠️ {elim_name} 已離開（{reason}）")

                elif msg.get("msg") == "result":
                    res = msg.get("result")
                    winner = msg.get("winner")
                    reason = msg.get("reason", "")
                    
                    print(f"\n最終結果：winner={winner}")
                    if reason:
                        print(f"原因：{reason}")
                    
                    if res == "win":
                        print("🎉 你贏了！")
                    else:
                        print("😢 你輸了！")
                    
                    game_finished = True
                    print("5 秒後自動關閉視窗...")
                    time.sleep(5)
                    break

                elif msg.get("msg") == "game_over":
                    game_finished = True
                    break

    except KeyboardInterrupt:
        print("\n[Client] 遊戲中斷，正在離開...")
        cleanup()
        sys.exit(0)
    
    except Exception as e:
        print(f"發生錯誤：{e}")
        import traceback
        traceback.print_exc()
    
    finally:
        cleanup()
        sys.exit(0)

if __name__ == "__main__":
    main()