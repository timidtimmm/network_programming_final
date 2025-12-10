# sample_games/rps/start_client.py - 完整修正版

import os, socket, json, sys, time
import atexit
import signal

HOST = os.getenv("GAME_HOST", "127.0.0.1")
PORT = int(os.getenv("GAME_PORT", "0"))
PLAYER = os.getenv("PLAYER_NAME", "player")
print(f"[HB-Client] connecting to {HOST}:{PORT} as {PLAYER}", flush=True)

HAND_CHOICES = ["1", "2", "3"]
DIR_CHOICES  = ["1", "2", "3", "4"]

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
    """改進版：捕獲 KeyboardInterrupt"""
    while True:
        try:
            c = input(prompt).strip()
            if c in valid:
                return c
        except KeyboardInterrupt:
            # ✅ Ctrl+C 時拋出異常，讓外層處理
            print("\n[Client] 偵測到 Ctrl+C，正在退出...")
            raise
        except EOFError:
            # Ctrl+D 或管道關閉
            raise KeyboardInterrupt

def main():
    s = None
    
    def cleanup():
        if s:
            try:
                # ✅ 嘗試發送退出訊息（如果伺服器有實作）
                try:
                    send(s, {"kind": "quit"})
                except:
                    pass
                s.close()
            except:
                pass
        print("已離開遊戲。")
    
    atexit.register(cleanup)
    
    def signal_handler(sig, frame):
        print(f"\n[Client] 收到中斷訊號 {sig}，正在退出...")
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
        # 1. 握手
        send(s, {"name": PLAYER})
        hello = recv(s)
        if not hello:
            print("無回應")
            return

        print("進入房間:", hello.get("room"))
        print("手勢編號: 1=石頭 , 2=布 , 3=剪刀")
        print("方向編號: 1=上 , 2=下 , 3=左 , 4=右")
        print("對手加入前請稍候...")

        ready = recv(s)
        if not ready or ready.get("msg") != "ready":
            print("等待失敗")
            return

        print("對手已就緒！開始遊戲。")
        print("(按 Ctrl+C 可隨時退出)")  # ✅ 提示用戶

        game_finished = False

        while not game_finished:
            # ---------- 第一階段：猜拳決定指人者 ----------
            mv = ask_choice("請輸入手勢 (1=石頭, 2=布, 3=剪刀): ", HAND_CHOICES)
            send(s, {"kind": "hand", "choice": int(mv)})

            print("✅ 你已決定出拳，正在等待對手出拳...", flush=True)

            pointer = None
            hands = None

            # 等待伺服器回應這次出拳的結果
            while True:
                msg = recv(s)
                if not msg:
                    print("伺服器中斷")
                    return

                if msg.get("msg") == "round" and msg.get("phase") == "hand":
                    print("✅ 對手已決定出拳！", flush=True)

                    hands = msg.get("hands", {})
                    if msg.get("result") == "draw":
                        print(f"本輪出拳平手，重新猜拳。雙方手勢: {hands}")
                        break
                    elif msg.get("result") == "point":
                        pointer = msg.get("pointer")
                        print(f"本輪出拳結果，指人者為: {pointer}")
                        print(f"雙方手勢: {hands}")
                        break

                elif msg.get("msg") == "result":
                    res = msg.get("result")
                    winner = msg.get("winner")
                    reason = msg.get("reason", "")
                    pdir = msg.get("pointer_dir")
                    ldir = msg.get("loser_dir")
                    
                    print(f"最終結果：winner={winner}, 指人方向={pdir}, 被指方向={ldir}")
                    if reason:
                        print(f"原因：{reason}")
                    
                    if res == "win":
                        print("🎉 你贏了！")
                    else:
                        print("😢 你輸了！")
                    game_finished = True
                    break

                elif msg.get("msg") == "game_over":
                    game_finished = True
                    break

            if game_finished:
                break

            if pointer is None:
                continue

            is_pointer = (pointer == PLAYER)
            role = "指人者" if is_pointer else "被指者"
            print(f"你在本輪的角色是：{role}")

            # ---------- 第二階段：指方向 / 轉頭 ----------
            d = ask_choice("請輸入方向 (1=上, 2=下, 3=左, 4=右): ", DIR_CHOICES)
            send(s, {"kind": "dir", "choice": int(d)})

            print("✅ 你已決定方向，正在等待對手動作...", flush=True)

            # 等待這次指向的結果
            while True:
                msg = recv(s)
                if not msg:
                    print("伺服器中斷")
                    return

                if msg.get("msg") == "round" and msg.get("phase") == "dir":
                    print("✅ 對手也已決定方向！", flush=True)

                    if msg.get("result") == "miss":
                        print("方向沒有對到，重新開始下一輪黑白猜！")
                        break

                elif msg.get("msg") == "result":
                    res = msg.get("result")
                    winner = msg.get("winner")
                    reason = msg.get("reason", "")
                    pdir = msg.get("pointer_dir")
                    ldir = msg.get("loser_dir")
                    
                    print(f"最終結果：winner={winner}, 指人方向={pdir}, 被指方向={ldir}")
                    if reason:
                        print(f"原因：{reason}")
                    
                    if res == "win":
                        print("🎉 你贏了！")
                    else:
                        print("😢 你輸了！")
                        
                    game_finished = True
                    print("5 秒後自動關閉視窗...")
                    print("離開房間回到大廳...")
                    time.sleep(5)
                    break

                elif msg.get("msg") == "game_over":
                    game_finished = True
                    break

    except KeyboardInterrupt:
        # ✅ 關鍵：捕獲 Ctrl+C
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