# sample_games/rps/start_server.py
import os, socket, threading, json

HOST = os.getenv("GAME_HOST", "127.0.0.1")
PORT = int(os.getenv("GAME_PORT", "0"))
ROOM_ID = os.getenv("ROOM_ID", "room")
print(f"[RPS SERVER] Starting on {HOST}:{PORT} (room {ROOM_ID})", flush=True)

CHOICES = ["rock","paper","scissors"]
players = {}
lock = threading.Lock()

def decide(m1, m2):
    if m1 == m2: return 0
    if (m1=="rock" and m2=="scissors") or (m1=="scissors" and m2=="paper") or (m1=="paper" and m2=="rock"):
        return 1
    return 2

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

def handle(conn, addr):
    try:
        hello = recv(conn)
        name = hello.get("name","?")
        with lock:
            players[name] = {"conn": conn, "move": None}
        send(conn, {"msg":"welcome","room":ROOM_ID,"choices":CHOICES})
        # wait for 2 players
        while True:
            with lock:
                if len(players)>=2: break
        send(conn, {"msg":"ready"})
        while True:
            req = recv(conn)
            if not req: break
            if req.get("kind")=="move":
                mv = req.get("choice")
                if mv not in CHOICES:
                    send(conn, {"msg":"error","error":"invalid move"})
                    continue
                with lock:
                    players[name]["move"]=mv
                    # check both ready
                    allmoves = [v["move"] for v in players.values()]
                    if None not in allmoves and len(allmoves)>=2:
                        names = list(players.keys())[:2]
                        a,b = names[0], names[1]
                        r = decide(players[a]["move"], players[b]["move"])
                        if r==0:
                            for nm in (a,b):
                                send(players[nm]["conn"], {"msg":"result","result":"draw","moves":{a:players[a]["move"],b:players[b]["move"]}})
                        elif r==1:
                            send(players[a]["conn"], {"msg":"result","result":"win","moves":{a:players[a]["move"],b:players[b]["move"]}})
                            send(players[b]["conn"], {"msg":"result","result":"lose","moves":{a:players[a]["move"],b:players[b]["move"]}})
                        else:
                            send(players[b]["conn"], {"msg":"result","result":"win","moves":{a:players[a]["move"],b:players[b]["move"]}})
                            send(players[a]["conn"], {"msg":"result","result":"lose","moves":{a:players[a]["move"],b:players[b]["move"]}})
                        # reset for another round
                        players[a]["move"]=None
                        players[b]["move"]=None
            else:
                send(conn, {"msg":"error","error":"unknown"})
    finally:
        conn.close()
        with lock:
            if name in players: del players[name]

def serve():
    s = socket.socket()
    s.bind((HOST, PORT))
    s.listen(5)
    while True:
        c,a = s.accept()
        threading.Thread(target=handle, args=(c,a), daemon=True).start()

if __name__=="__main__":
    serve()
