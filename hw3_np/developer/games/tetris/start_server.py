# --- HW3 uploaded_games bootstrap ---
import sys, os
GAME_ROOT = os.path.dirname(__file__)
sys.path.insert(0, GAME_ROOT)
sys.path.insert(0, os.path.join(GAME_ROOT, 'game'))
sys.path.insert(0, os.path.join(GAME_ROOT, 'common'))
# ------------------------------------
# developer\games\tetris\start_server.py

import argparse, asyncio, time, random, json, subprocess, socket, sys, threading
from typing import Dict, Optional, List
from framing import recv_json, send_json
from logic_tetris import TetrisEngine, PID

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

def notify_lobby_game_finished_kick_all():
    """通知 lobby：這個 ROOM_ID 的一局已經結束，並請求踢出所有玩家"""
    lobby_host = get_lobby_connect_host()  # ⭐ 使用新函數
    lobby_port = os.getenv("LOBBY_PORT")
    room_id = os.getenv("ROOM_ID")
    if not lobby_host or not lobby_port or not room_id:
        return
    try:
        with socket.create_connection((lobby_host, int(lobby_port)), timeout=2) as s:
            msg = json.dumps({
                "kind": "game_finished",
                "room_id": room_id,
                "kick_all": True
            }) + "\n"
            s.sendall(msg.encode("utf-8"))
            try:
                s.recv(4096)
            except Exception:
                pass
        print(f"[GameServer] Notified lobby kick_all for {room_id}", flush=True)
    except Exception as e:
        print(f"[GameServer] notify kick_all failed: {e}", flush=True)

ARGS = None

TICK_MS = 50
SNAPSHOT_MS = 150

class Conn:
    def __init__(self, reader, writer, user_id: str, name: str, role: str, spectator: bool=False):
        self.reader = reader
        self.writer = writer
        self.user_id = user_id
        self.name = name
        self.role = role
        self.spectator = spectator
        self.seq_seen = -1

class GameRoom:
    def __init__(self, duration_sec: int = 60, drop_ms: int = 500, seed: Optional[int]=None, 
                 gravity_mode: str = "progressive", gravity_config: Optional[dict] = None):
        self.duration_sec = duration_sec
        self.gravity_mode = gravity_mode
        cfg = gravity_config or {}
        self.gravity_cfg = {
            "initialDropMs": cfg.get("initialDropMs", drop_ms),
            "minDropMs":     cfg.get("minDropMs", 100),
            "intervalSec":   cfg.get("intervalSec", 30),
            "stepMs":        cfg.get("stepMs", 50),
        }

        self.initial_drop_ms = self.gravity_cfg["initialDropMs"]
        self.drop_ms = self.initial_drop_ms

        self.seed = seed if seed is not None else random.randint(1, 2**31-1)
        self.engine = {
            "P1": TetrisEngine(self.seed),
            "P2": TetrisEngine(self.seed),
        }
        self.conns: Dict[str, Optional[Conn]] = {"P1": None, "P2": None}
        self.spectators: List[Conn] = []
        self.input_queues = {"P1": asyncio.Queue(), "P2": asyncio.Queue()}
        self.started = False
        self.start_ms = None
        self.last_drop_ms = {"P1": 0, "P2": 0}
        self.last_snapshot_ms = 0
        self.last_gravity_update_ms = 0
        self.done = False
        self.result = None
        self.accepting_connections = True  # 🔧 新增：是否接受新連接
        self.disconnect_timestamps = {"P1": None, "P2": None}  # ✅ 記錄掉線時間
        self.early_end_reason = None  # ✅ 提前結束原因
        self.early_winner = None      # ✅ 提前結束贏家

    def role_of(self, conn: Conn) -> str:
        return conn.role

    def ready(self) -> bool:
        return self.conns["P1"] is not None and self.conns["P2"] is not None

    def any_topout(self) -> bool:
        return self.engine["P1"].topout or self.engine["P2"].topout
    
    def update_gravity(self, elapsed_sec: int):
        if self.gravity_mode == "fixed":
            return False, self.drop_ms, self.drop_ms

        if self.gravity_mode == "progressive":
            interval_sec = max(1, int(self.gravity_cfg["intervalSec"]))
            step_ms = int(self.gravity_cfg["stepMs"])
            min_drop_ms = int(self.gravity_cfg["minDropMs"])

            intervals = elapsed_sec // interval_sec
            new_drop_ms = max(min_drop_ms, self.initial_drop_ms - intervals * step_ms)

            if new_drop_ms != self.drop_ms:
                old_drop_ms = self.drop_ms
                self.drop_ms = new_drop_ms
                return True, old_drop_ms, new_drop_ms

            return False, self.drop_ms, self.drop_ms

        # 🔧 level 模式 - 根據總消除行數加速
        elif self.gravity_mode == "level":
            min_drop_ms = int(self.gravity_cfg.get("minDropMs", 50))
            lines_per_speedup = int(self.gravity_cfg.get("linesPerSpeedup", 1))  # 每幾行加速一次
            step_ms = int(self.gravity_cfg.get("stepMs", 20))  # 每次加速多少 ms

            total_lines = self.engine["P1"].lines + self.engine["P2"].lines
            speedup_count = total_lines // lines_per_speedup
            new_drop_ms = max(min_drop_ms, self.initial_drop_ms - speedup_count * step_ms)

            if new_drop_ms != self.drop_ms:
                old_drop_ms = self.drop_ms
                self.drop_ms = new_drop_ms
                return True, old_drop_ms, new_drop_ms

            return False, self.drop_ms, self.drop_ms

        return False, self.drop_ms, self.drop_ms

async def handle_client(room: GameRoom, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    conn = None
    try:
        # 🔧 遊戲結束後拒絕所有新連接
        if not room.accepting_connections:
            print(f"[GameServer] ✗ Rejected connection: game has ended")
            try:
                await send_json(writer, {
                    "type": "ERROR",
                    "code": "GameEnded",
                    "msg": "This game has already ended"
                })
            except:
                pass
            writer.close()
            await writer.wait_closed()
            return
        
        hello = await recv_json(reader)
        if hello.get("type") != "HELLO":
            await send_json(writer, {"type":"ERROR","code":"BadRequest","msg":"need HELLO"})
            writer.close()
            await writer.wait_closed()
            return
        
        # ⭐ 改用 username 作為識別
        username = str(hello.get("username", "player"))
        name = str(hello.get("name", username))  # name 可以是顯示名稱
        
        # 🔧 再次檢查（避免競態條件）
        if not room.accepting_connections:
            print(f"[GameServer] ✗ Rejected {name}: game ended during handshake")
            try:
                await send_json(writer, {
                    "type": "ERROR",
                    "code": "GameEnded",
                    "msg": "Game ended"
                })
            except:
                pass
            writer.close()
            await writer.wait_closed()
            return
        
        existing_role = None
        for role, c in room.conns.items():
            if c and c.user_id == username:
                try:
                    c.writer.close()
                    await c.writer.wait_closed()
                except:
                    pass
                existing_role = role
                break
        
        spectator = False
        if existing_role:
            role = existing_role
        elif room.conns["P1"] is None:
            role = "P1"
        elif room.conns["P2"] is None:
            role = "P2"
        else:
            spectator = True
            spec_num = len(room.spectators) + 1
            role = f"SPEC_{spec_num}"
        
        conn = Conn(reader, writer, username, name, role, spectator)
        
        if spectator:
            room.spectators.append(conn)
            print(f"[GameServer] 🎥 Spectator joined: {name} (userId={username}, role={role})")
        else:
            room.conns[role] = conn
            print(f"[GameServer] ✓ Player joined: {name} as {role} (userId={username})")
        
        await send_json(writer, {
            "type":"WELCOME",
            "role":role,
            "seed":room.seed,
            "bagRule":"7bag",
            "gravityPlan":{
                "mode": room.gravity_mode,
                "initialDropMs": room.gravity_cfg["initialDropMs"],
                "minDropMs": room.gravity_cfg["minDropMs"],
                "intervalSec": room.gravity_cfg["intervalSec"],
                "stepMs": room.gravity_cfg["stepMs"],
            },
            "rule":{"mode":"timer","durationSec":room.duration_sec},
            "spectator": spectator
        })
        
        if room.ready() and not room.started:
            room.started = True
            room.start_ms = int(time.time()*1000)
            room.last_drop_ms = {"P1": room.start_ms, "P2": room.start_ms}
            room.last_gravity_update_ms = room.start_ms
            print(f"[GameServer] ✓ Game started!")
        
        if not conn.spectator:
            room.disconnect_timestamps[conn.role] = None
        # 接收循環帶超時
        while not room.done:
            try:
                msg = await asyncio.wait_for(recv_json(reader), timeout=1.0)
                t = msg.get("type")
                
                if t == "INPUT" and not conn.spectator and room.started:
                    action = msg.get("action")
                    seq = int(msg.get("seq", 0))
                    if seq <= conn.seq_seen: 
                        continue
                    conn.seq_seen = seq
                    await room.input_queues[conn.role].put(action)
                
                elif t == "PING":
                    await send_json(writer, {"type":"PONG","t":msg.get("t")})
                
                elif t == "BYE":
                    print(f"[GameServer] Client {conn.name} sent BYE")
                    break
                
            except asyncio.TimeoutError:
                continue
            except ConnectionError:
                break
    
    except ConnectionError:
        pass  # 靜默處理正常斷線
    
    except Exception as e:
        print(f"[GameServer] Error handling client {conn.name if conn else 'unknown'}: {e}")
    
    finally:
        if conn:
            # ✅ 關鍵：玩家掉線時記錄時間戳
            if conn and not conn.spectator and room.started and not room.done:
                room.disconnect_timestamps[conn.role] = time.time()
                print(f"[GameServer] ⚠ {conn.name} ({conn.role}) disconnected during game", flush=True)

                # 檢查是否應提前結束
                await check_early_end(room)
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
            
            if conn.spectator:
                room.spectators = [s for s in room.spectators if s is not conn]
                if room.accepting_connections:  # 只在遊戲中才記錄
                    print(f"[GameServer] Spectator left: {conn.name}")
            else:
                for role, c in list(room.conns.items()):
                    if c is conn:
                        room.conns[role] = None
                        if room.accepting_connections:  # 只在遊戲中才記錄
                            print(f"[GameServer] Player left: {conn.name} ({role})")
async def check_early_end(room: GameRoom):
    """檢查是否因掉線而提前結束遊戲"""
    if room.done:
        return
    
    p1_disc = room.disconnect_timestamps.get("P1")
    p2_disc = room.disconnect_timestamps.get("P2")
    now = time.time()
    
    # ✅ 策略1：任一玩家掉線超過 3 秒 → 判負
    DISCONNECT_TIMEOUT = 3.0
    
    if p1_disc and (now - p1_disc) >= DISCONNECT_TIMEOUT:
        print(f"[GameServer] P1 disconnect timeout, P2 wins by forfeit", flush=True)
        room.early_end_reason = "P1 disconnected"
        room.early_winner = "P2"
        room.done = True
        return
    
    if p2_disc and (now - p2_disc) >= DISCONNECT_TIMEOUT:
        print(f"[GameServer] P2 disconnect timeout, P1 wins by forfeit", flush=True)
        room.early_end_reason = "P2 disconnected"
        room.early_winner = "P1"
        room.done = True
        return
    
    # ✅ 策略2：雙方都掉線 → 立即結束，比分數
    if p1_disc and p2_disc:
        print(f"[GameServer] Both players disconnected, ending immediately", flush=True)
        room.early_end_reason = "Both disconnected"
        room.done = True
        return

async def broadcast(conns, obj):
    """廣播給所有連線"""
    dead = []
    for c in conns:
        try:
            await send_json(c.writer, obj)
        except Exception:
            dead.append(c)
    
    for c in dead:
        try:
            c.writer.close()
            await c.writer.wait_closed()
        except:
            pass

async def game_loop(room: GameRoom):
    while not room.started:
        await asyncio.sleep(0.02)
        if room.ready() and not room.started:
            room.started = True
            now = int(time.time()*1000)
            room.start_ms = now
            room.last_drop_ms = {"P1": now, "P2": now}
            room.last_gravity_update_ms = now

    print(f"[GameServer] Game loop started")
    last_total_lines = 0  # 追蹤上次的總行數
    while not room.done:
        now = int(time.time()*1000)
        
        # ✅ 定期檢查掉線超時
        if now % 1000 < TICK_MS:  # 每秒檢查一次
            await check_early_end(room)
        
        if room.done:
            print(f"[GameServer] Early end detected, broadcasting results...", flush=True)
            break

        if now - room.start_ms >= room.duration_sec*1000:
            room.done = True
            break

        elapsed_sec = (now - room.start_ms) // 1000
        if room.gravity_mode == "progressive":
            if now - room.last_gravity_update_ms >= 30000:
                changed, old_ms, new_ms = room.update_gravity(elapsed_sec)
                if changed:
                    room.last_gravity_update_ms = now
                    print(f"[GameServer] Gravity update: {old_ms}ms -> {new_ms}ms (elapsed: {elapsed_sec}s)")
                    all_conns = [c for c in room.conns.values() if c is not None] + room.spectators
                    await broadcast(all_conns, {
                        "type":"GRAVITY_UPDATE",
                        "dropMs":new_ms,
                        "reason":f"Time {elapsed_sec}s",
                        "at":now
                    })
        elif room.gravity_mode == "level":
            current_total_lines = room.engine["P1"].lines + room.engine["P2"].lines
            if current_total_lines != last_total_lines:
                last_total_lines = current_total_lines
                changed, old_ms, new_ms = room.update_gravity(elapsed_sec)
                if changed:
                    print(f"[GameServer] Level update: {old_ms}ms -> {new_ms}ms (total lines: {current_total_lines})")
                    all_conns = [c for c in room.conns.values() if c is not None] + room.spectators
                    await broadcast(all_conns, {
                        "type":"GRAVITY_UPDATE",
                        "dropMs":new_ms,
                        "reason":f"{current_total_lines} lines cleared",
                        "at":now
                    })

        for role in ["P1","P2"]:
            eng = room.engine[role]
            for _ in range(8):
                if room.input_queues[role].empty(): 
                    break
                act = await room.input_queues[role].get()
                if act == "LEFT":
                    eng.move(-1, 0)
                elif act == "RIGHT":
                    eng.move(1, 0)
                elif act == "CW":
                    eng.rotate(+1)
                elif act == "CCW":
                    eng.rotate(-1)
                elif act == "SOFT":
                    eng.soft_drop()
                elif act == "HARD":
                    eng.hard_drop()
                elif act == "HOLD":
                    eng.hold_swap()

        for role in ["P1","P2"]:
            eng = room.engine[role]
            if now - room.last_drop_ms[role] >= room.drop_ms:
                room.last_drop_ms[role] = now
                eng.soft_drop()

        if room.any_topout():
            room.done = True

        if now - room.last_snapshot_ms >= SNAPSHOT_MS:
            room.last_snapshot_ms = now
            snap = {
                "type":"SNAPSHOT",
                "at":now,
                "remainMs":max(0, room.duration_sec*1000 - (now-room.start_ms)),
                "currentDropMs":room.drop_ms,
                "players":[]
            }
            for role in ["P1","P2"]:
                eng = room.engine[role]
                conn = room.conns.get(role)
                player_name = conn.name if conn else f"Player{role[-1]}"
                s = eng.snapshot()
                board = [row[:] for row in s.board]
                if eng.active is not None:
                    for (cx, cy) in eng._cells(eng.active):
                        if 0 <= cy < 20 and 0 <= cx < 10:
                            board[cy][cx] = PID[eng.active.shape]
                snap["players"].append({
                    "role": role,
                    "name": player_name,
                    "board": board,
                    "active": s.active,
                    "hold": s.hold,
                    "next": s.nextq,
                    "score": s.score,
                    "lines": s.lines,
                    "level": s.level,
                    "blocksCleared": s.blocks_cleared,
                })
            all_conns = [c for c in room.conns.values() if c is not None] + room.spectators
            await broadcast(all_conns, snap)

        await asyncio.sleep(TICK_MS/1000.0)

    # 🔧 遊戲結束：立即停止接受新連接
    print(f"[GameServer] ⚠ Game ended - STOPPING new connections")
    room.accepting_connections = False

    # ✅ 統一走同一個出口（只廣播一次，只通知一次）
    await broadcast_game_end(room)
    return


async def broadcast_game_end(room: GameRoom):
    """統一處理遊戲結束的邏輯"""
    # 停止接受新連線
    room.accepting_connections = False
    
    # 判定勝負
    p1 = room.engine["P1"]
    p2 = room.engine["P2"]
    p1_conn = room.conns.get("P1")
    p2_conn = room.conns.get("P2")
    
    winner_role = None
    win_detail = ""
    reason = "unknown"
    
     # ✅ 0) 優先處理離線判負
    if room.early_end_reason:
        reason = "forfeit"
        winner_role = room.early_winner
        win_detail = room.early_end_reason

        # 雙方都掉線 → 比分數，不然平手
        if winner_role is None:
            p1_score = int(p1.score)
            p2_score = int(p2.score)
            if p1_score != p2_score:
                winner_role = "P1" if p1_score > p2_score else "P2"
                win_detail = f"higher score after both disconnected ({p1_score} vs {p2_score})"
            else:
                win_detail = "draw (both disconnected with same score)"

    else:
        # ✅ 1) 正常結束（TopOut > Lines > Score > Draw）
        p1_top = bool(p1.topout)
        p2_top = bool(p2.topout)
        p1_lines = int(p1.lines)
        p2_lines = int(p2.lines)
        p1_score = int(p1.score)
        p2_score = int(p2.score)

        if p1_top != p2_top:
            winner_role = "P2" if p1_top else "P1"
            win_detail = "opponent top out"
            reason = "topout"

        elif p1_top and p2_top:
            if p1_lines != p2_lines:
                winner_role = "P1" if p1_lines > p2_lines else "P2"
                win_detail = f"more lines after simultaneous top out ({p1_lines} vs {p2_lines})"
            elif p1_score != p2_score:
                winner_role = "P1" if p1_score > p2_score else "P2"
                win_detail = f"higher score after simultaneous top out ({p1_score} vs {p2_score})"
            else:
                winner_role = None
                win_detail = "draw (simultaneous top out)"
            reason = "topout"

        else:
            if p1_lines != p2_lines:
                winner_role = "P1" if p1_lines > p2_lines else "P2"
                win_detail = f"more lines ({p1_lines} vs {p2_lines})"
            elif p1_score != p2_score:
                winner_role = "P1" if p1_score > p2_score else "P2"
                win_detail = f"higher score ({p1_score} vs {p2_score})"
            else:
                winner_role = None
                win_detail = "draw (same lines and score)"
            reason = "timeup"
    
    # 構建結果訊息
    results = [
        {
            "role":"P1",
            "username": p1_conn.user_id if p1_conn else None,
            "name": p1_conn.name if p1_conn else "Player1",
            "score": int(p1.score),
            "lines": int(p1.lines),
            "blocksCleared": int(p1.blocks_cleared),
            "maxCombo": int(p1.max_combo),
        },
        {
            "role":"P2",
            "username": p2_conn.user_id if p2_conn else None,
            "name": p2_conn.name if p2_conn else "Player2",
            "score": int(p2.score),
            "lines": int(p2.lines),
            "blocksCleared": int(p2.blocks_cleared),
            "maxCombo": int(p2.max_combo),
        },
    ]
    
    winner_username = (p1_conn.user_id if winner_role == "P1" and p1_conn else
                       p2_conn.user_id if winner_role == "P2" and p2_conn else None)
    
    msg = {
        "type": "MATCH_END",
        "reason": reason,
        "results": results,
        "winnerRole": winner_role,
        "winnerUsername": winner_username,
        "winDetail": win_detail
    }
    
    # ✅ 廣播給所有還連線的人
    all_conns = [c for c in room.conns.values() if c is not None] + room.spectators
    print(f"[GameServer] Broadcasting MATCH_END to {len(all_conns)} connections", flush=True)
    await broadcast(all_conns, msg)
    
    # 踢出觀戰者
    for spec in list(room.spectators):
        try:
            await send_json(spec.writer, {
                "type":"SPECTATOR_KICKED",
                "reason":"Game ended"
            })
            spec.writer.close()
            await spec.writer.wait_closed()
        except:
            pass
    room.spectators.clear()
    
    # ✅ 通知 Lobby 踢人並關房
    notify_lobby_and_close(room)
    
    # 給客戶端時間處理
    await asyncio.sleep(1.5)


def notify_lobby_and_close(room):
    """通知 Lobby 遊戲結束並踢出所有人"""
    try:
        lobby_host = get_lobby_connect_host() or getattr(ARGS, "lobbyHost", None)
        lobby_port = int(getattr(ARGS, "lobbyPort", 0) or os.getenv("LOBBY_PORT", "0"))
        
        if not lobby_host or not lobby_port:
            print(f"[GameServer] No lobby info, skip notify", flush=True)
            return
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((lobby_host, lobby_port))
        
        # 構建勝負資訊
        p1_conn = room.conns.get("P1")
        p2_conn = room.conns.get("P2")
        winner_role = room.early_winner if room.early_end_reason else None
        
        payload = {
            "kind": "game_finished",
            "room_id": ARGS.roomId,
            "kick_all": True,  # ✅ 關鍵：要求踢出所有人
            "reason": room.early_end_reason or "game_end",
            "winnerRole": winner_role,
            "winnerUsername": (p1_conn.user_id if winner_role == "P1" and p1_conn else
                               p2_conn.user_id if winner_role == "P2" and p2_conn else None),
        }
        
        msg = json.dumps(payload, ensure_ascii=False) + "\n"
        sock.sendall(msg.encode("utf-8"))
        print(f"[GameServer] ✓ Notified lobby (kick_all=True)", flush=True)
        
        try:
            resp_data = sock.recv(4096).decode("utf-8").strip()
            if resp_data:
                resp = json.loads(resp_data)
                print(f"[GameServer] Lobby response: {resp}", flush=True)
        except:
            pass
        
        sock.close()
        
    except Exception as e:
        print(f"[GameServer] Failed to notify lobby: {e}", flush=True)
        
async def main():
    ap = argparse.ArgumentParser()
    
    # 🔧 從環境變數讀取預設值
    default_port = int(os.getenv("GAME_PORT", "0"))
    default_room_id = os.getenv("ROOM_ID", "0")
    
    # 🔧 關鍵：優先使用 LOBBY_CONNECT_HOST，並處理 0.0.0.0
    default_lobby_host = os.getenv("LOBBY_CONNECT_HOST")
    if not default_lobby_host:
        default_lobby_host = os.getenv("LOBBY_HOST", "127.0.0.1")
        if default_lobby_host == "0.0.0.0":
            default_lobby_host = "127.0.0.1"
    
    default_lobby_port = int(os.getenv("LOBBY_PORT", "13001"))
    
    # 🔧 移除 required=True，改用 default
    ap.add_argument("--port", type=int, default=default_port)
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--dropMs", type=int, default=500)
    ap.add_argument("--roomId", default=default_room_id)
    ap.add_argument("--lobbyHost", default=default_lobby_host)
    ap.add_argument("--lobbyPort", type=int, default=default_lobby_port)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--gravityMode", default="progressive")
    ap.add_argument("--gravityConfig", default=None)
    args = ap.parse_args()

    global ARGS
    ARGS = args
    
    # 🔧 驗證 port
    if args.port == 0:
        print("[GameServer] Error: No port specified (use --port or GAME_PORT env)", flush=True)
        sys.exit(1)
    
    gravity_config = None
    if args.gravityConfig:
        try:
            gravity_config = json.loads(args.gravityConfig)
        except:
            gravity_config = {
                "initialDropMs": args.dropMs,
                "minDropMs": 100,
                "intervalSec": 30
            }
    
    room = GameRoom(
        duration_sec=args.duration, 
        drop_ms=args.dropMs, 
        seed=args.seed,
        gravity_mode=args.gravityMode,
        gravity_config=gravity_config
    )

    server = None
    
    async def _handle(r, w):
        await handle_client(room, r, w)

    try:
        server = await asyncio.start_server(_handle, host="0.0.0.0", port=args.port)
        print(f"[GameServer] Listening @ {args.port}  seed={room.seed}  dropMs={room.drop_ms}  duration={room.duration_sec}s")
        
        # 並行運行伺服器和遊戲循環
        async with server:
            loop_task = asyncio.create_task(game_loop(room))
            serve_task = asyncio.create_task(server.serve_forever())
            
            # 等待遊戲循環結束
            await loop_task
            
            # 遊戲結束後立即關閉伺服器
            print("[GameServer] ⚠ Closing server socket...")
            server.close()
            await server.wait_closed()
            
            # 取消 serve_forever
            serve_task.cancel()
            try:
                await serve_task
            except asyncio.CancelledError:
                pass
            
            print("[GameServer] ✓ Server closed")
            await asyncio.sleep(2.0)
            print("[GameServer] Closing remaining connections...")
            for role, conn in room.conns.items():
                if conn:
                    try:
                        conn.writer.close()
                        await conn.writer.wait_closed()
                    except:
                        pass
            print("[GameServer] ✓✓✓ Shutdown complete, exiting...")
    
    except Exception as e:
        print(f"[GameServer] Fatal error: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        if server:
            server.close()
            try:
                await server.wait_closed()
            except:
                pass
        print("[GameServer] Process terminating...")
        sys.exit(0)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[GameServer] Interrupted")
    finally:
        print("[GameServer] Bye")
        sys.exit(0)