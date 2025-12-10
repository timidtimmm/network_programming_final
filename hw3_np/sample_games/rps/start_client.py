# sample_games/rps/start_client.py
import os, socket, json, threading, sys

HOST = os.getenv("GAME_HOST", "127.0.0.1")
PORT = int(os.getenv("GAME_PORT", "0"))
PLAYER = os.getenv("PLAYER_NAME", "player")
print(f"[RPS CLIENT] connecting to {HOST}:{PORT} as {PLAYER}", flush=True)

def send(conn, obj):
    conn.sendall((json.dumps(obj)+"\n").encode())

def recv(conn):
    buf=b""
    while True:
        d = conn.recv(1024)
        if not d: return None
        buf+=d
        if b"\n" in buf:
            line,_=buf.split(b"\n",1)
            return json.loads(line.decode())

def main():
    s=socket.socket()
    try:
        s.connect((HOST,PORT))
    except Exception as e:
        print("連線失敗:", e); return
    send(s, {"name": PLAYER})
    hello = recv(s)
    if not hello:
        print("無回應"); return
    print("進入房間:", hello.get("room"))
    choices = hello.get("choices",["rock","paper","scissors"])
    print("對手加入前請稍候...")
    ready = recv(s)
    if not ready or ready.get("msg")!="ready":
        print("等待失敗"); return
    print("對手已就緒！開始出拳。")

    try:
        while True:
            mv = ""
            while mv not in choices:
                mv = input(f"請輸入出拳 {choices}: ").strip().lower()
            send(s, {"kind":"move","choice":mv})
            res = recv(s)
            if not res:
                print("伺服器中斷"); break
            if res.get("msg")=="result":
                print("結果:", res["result"], " | 雙方:", res["moves"])
                print("下一回合！")
    finally:
        s.close()

if __name__=="__main__":
    main()
