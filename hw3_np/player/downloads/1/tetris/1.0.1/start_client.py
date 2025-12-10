# --- HW3 uploaded_games bootstrap ---
import sys, os
GAME_ROOT = os.path.dirname(__file__)
sys.path.insert(0, GAME_ROOT)
sys.path.insert(0, os.path.join(GAME_ROOT, 'game'))
sys.path.insert(0, os.path.join(GAME_ROOT, 'common'))
# ------------------------------------
# developer\games\tetris\start_client.py
import argparse, threading, queue, time, sys
import pygame
from framing import recv_json, send_json
import asyncio

import atexit
import signal

print("[GUI] Script started", flush=True)

# 顏色定義
COLORS = {
    0: (40, 40, 40),      # 空格
    1: (0, 240, 240),     # I - 青色
    2: (0, 0, 240),       # J - 藍色
    3: (240, 160, 0),     # L - 橙色
    4: (240, 240, 0),     # O - 黃色
    5: (0, 240, 0),       # S - 綠色
    6: (160, 0, 240),     # T - 紫色
    7: (240, 0, 0),       # Z - 紅色
}

PIECE_NAMES = {1: 'I', 2: 'J', 3: 'L', 4: 'O', 5: 'S', 6: 'T', 7: 'Z'}

# 遊戲狀態
state = {
    "connected": False,
    "board_me": [[0]*10 for _ in range(20)],
    "board_op": [[0]*10 for _ in range(20)],
    "score": 0,
    "lines": 0,
    "level": 1,
    "my_role": None,
    "hold": None,
    "next_queue": [],
    "msg": "Connecting...",
    "remain_sec": 0,
    "is_spectator": False,
    "my_name": "You",
    "op_name": "Opponent",
    "current_drop_ms": 500,  # 🔧 當前掉落速度
    "gravity_plan": None,    # 🔧 節奏計劃
    # --- 新增：勝負顯示用 ---
    "winner_role": None,     # "P1" / "P2" / None
    "winner_reason": None,   # e.g. "topout @ P1", "higher score", "draw"
    "winner_name": None,     # 得勝者名稱（觀戰者用）
}


def apply_snapshot(msg: dict):
    """更新遊戲狀態"""
    players = msg.get("players", [])
    if not players:
        return

    # 根據自己的角色正確顯示
    my_role = state.get("my_role")

    # 如果是觀戰者，顯示 P1 在左邊，P2 在右邊
    if my_role and my_role.startswith("SPEC"):
        by_role = {p.get("role"): p for p in players}
        me_p = by_role.get("P1", players[0])
        op_p = by_role.get("P2", players[1])

        if me_p:
            state["board_me"] = me_p.get("board", [[0]*10 for _ in range(20)])
            state["my_name"] = me_p.get("name", "P1")
            state["score"] = me_p.get("score", 0)
            state["lines"] = me_p.get("lines", 0)
            state["level"] = me_p.get("level", 1)
            state["hold"] = me_p.get("hold")
            state["next_queue"] = me_p.get("next", [])

        if op_p:
            state["board_op"] = op_p.get("board", [[0]*10 for _ in range(20)])
            state["op_name"] = op_p.get("name", "P2")
    else:
        # 玩家模式：左邊自己，右邊對手
        by_role = {p.get("role"): p for p in players}
        op_role = "P2" if my_role == "P1" else "P1"

        me_p = by_role.get(my_role, players[0])
        op_p = by_role.get(op_role)

        if "board" in me_p:
            state["board_me"] = me_p["board"]
        state["score"] = me_p.get("score", 0)
        state["lines"] = me_p.get("lines", 0)
        state["level"] = me_p.get("level", 1)
        state["hold"] = me_p.get("hold")
        state["next_queue"] = me_p.get("next", [])
        state["my_name"] = me_p.get("name", my_role)

        if op_p and "board" in op_p:
            state["board_op"] = op_p["board"]
            state["op_name"] = op_p.get("name", op_role)

    # 🔧 更新當前掉落速度
    current_drop_ms = msg.get("currentDropMs")
    if current_drop_ms:
        state["current_drop_ms"] = current_drop_ms

    remain_ms = msg.get("remainMs", 0)
    state["remain_sec"] = max(0, remain_ms // 1000)


def start_network_thread(host, port, me_user, me_name, inbox: queue.Queue, outbox: queue.Queue):
    """背景網路執行緒（遊戲結束後停止重連）"""
    print(f"[GUI] Starting network thread to {host}:{port}", flush=True)

    async def net_main():
        attempts = 0
        game_ended = False  # ⭐ 關鍵旗標

        while attempts < 50 and not game_ended:
            writer = None
            send_task = None
            try:
                print(f"[GUI] Connecting... (attempt {attempts+1})", flush=True)
                reader, writer = await asyncio.open_connection(host, int(port))
                print(f"[GUI] Connected!", flush=True)
                inbox.put({"type": "NET", "sub": "CONNECTED"})

                # 發送 HELLO
                await send_json(writer, {
                    "type": "HELLO",
                    "version": 1,
                    "roomId": 0,
                    "username": me_user,
                    "name": me_name
                })
                await writer.drain()
                print(f"[GUI] HELLO sent", flush=True)

                # 建立發送任務
                async def send_loop():
                    while True:
                        try:
                            msg = outbox.get(timeout=0.1)
                            await send_json(writer, msg)
                            if msg.get("type") != "INPUT":
                                print(f"[GUI] Sent: {msg.get('type')}", flush=True)
                        except queue.Empty:
                            await asyncio.sleep(0.01)
                        except Exception as e:
                            print(f"[GUI][send_loop] error: {e}", flush=True)
                            break

                send_task = asyncio.create_task(send_loop())

                # 接收迴圈
                msg_count = 0
                while True:
                    m = await recv_json(reader)
                    msg_count += 1
                    t = m.get("type")

                    if t != "SNAPSHOT" or msg_count % 30 == 0:
                        print(f"[GUI] Received #{msg_count}: {t}", flush=True)

                    inbox.put(m)

                    # ⭐ 收到結束訊號 → 停止重連
                    if t in ("MATCH_END", "SPECTATOR_KICKED"):
                        game_ended = True
                        print(f"[GUI] Game ended, stopping reconnection attempts", flush=True)
                        break

            except Exception as e:
                if game_ended:
                    print(f"[GUI] Connection closed after game end", flush=True)
                    break

                attempts += 1
                print(f"[GUI] Connection error: {e} (attempt {attempts})", flush=True)
                inbox.put({"type": "NET", "sub": "ERROR", "detail": f"{e} (try#{attempts})"})

                if attempts >= 50:
                    break

                await asyncio.sleep(0.5)  # ⭐ 放慢重連

            finally:
                if send_task:
                    send_task.cancel()
                    try:
                        await send_task
                    except Exception:
                        pass

                if writer:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

    def runner():
        try:
            asyncio.run(net_main())
        except Exception as e:
            print(f"[GUI] Runner error: {e}", flush=True)
            inbox.put({"type": "NET", "sub": "ERROR", "detail": str(e)})

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    print(f"[GUI] Network thread started", flush=True)
    return th


def draw_cell(surf, x, y, w, h, color):
    """繪製單個方塊（帶立體效果）"""
    pygame.draw.rect(surf, color, (x, y, w, h))
    lighter = tuple(min(255, c + 40) for c in color)
    pygame.draw.line(surf, lighter, (x, y), (x + w, y), 2)
    pygame.draw.line(surf, lighter, (x, y), (x, y + h), 2)
    darker = tuple(max(0, c - 40) for c in color)
    pygame.draw.line(surf, darker, (x + w, y), (x + w, y + h), 2)
    pygame.draw.line(surf, darker, (x, y + h), (x + w, y + h), 2)


def draw_board(surf, board, x0, y0, cell_size=24, title=""):
    """繪製遊戲板"""
    if title:
        font = pygame.font.SysFont(None, 24)
        txt = font.render(title, True, (255, 255, 255))
        surf.blit(txt, (x0, y0 - 25))

    bg_rect = pygame.Rect(x0 - 2, y0 - 2, 10 * cell_size + 4, 20 * cell_size + 4)
    pygame.draw.rect(surf, (60, 60, 60), bg_rect)
    pygame.draw.rect(surf, (100, 100, 100), bg_rect, 2)

    for row in range(20):
        for col in range(10):
            val = board[row][col]
            color = COLORS.get(val, (100, 100, 100))
            cell_x = x0 + col * cell_size
            cell_y = y0 + row * cell_size

            if val == 0:
                pygame.draw.rect(surf, color, (cell_x, cell_y, cell_size - 1, cell_size - 1))
            else:
                draw_cell(surf, cell_x, cell_y, cell_size - 1, cell_size - 1, color)


def draw_piece_preview(surf, piece_name, x, y, cell_size=20):
    """繪製方塊預覽（Hold 或 Next）"""
    if not piece_name:
        return

    shapes = {
        'I': [(0, 1), (1, 1), (2, 1), (3, 1)],
        'O': [(1, 0), (2, 0), (1, 1), (2, 1)],
        'T': [(1, 0), (0, 1), (1, 1), (2, 1)],
        'L': [(2, 0), (0, 1), (1, 1), (2, 1)],
        'J': [(0, 0), (0, 1), (1, 1), (2, 1)],
        'S': [(1, 0), (2, 0), (0, 1), (1, 1)],
        'Z': [(0, 0), (1, 0), (1, 1), (2, 1)],
    }

    piece_id = None
    for pid, name in PIECE_NAMES.items():
        if name == piece_name:
            piece_id = pid
            break

    if piece_id is None:
        return

    color = COLORS.get(piece_id, (150, 150, 150))
    cells = shapes.get(piece_name, [])

    for (cx, cy) in cells:
        draw_cell(surf, x + cx * cell_size, y + cy * cell_size,
                  cell_size - 2, cell_size - 2, color)


def pygame_main(host, port, me_user, me_name):
    print(f"[GUI] pygame_main started: {me_name} @ {host}:{port} (user={me_user})", flush=True)

    pygame.init()
    print(f"[GUI] pygame initialized", flush=True)

    W, H = 900, 600
    screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption(f"Tetris - {me_name}")
    print(f"[GUI] Window created: {W}x{H}", flush=True)

    clock = pygame.time.Clock()
    font = pygame.font.SysFont(None, 24)
    font_large = pygame.font.SysFont(None, 32)
    font_title = pygame.font.SysFont(None, 48)

    inbox = queue.Queue()
    outbox = queue.Queue()
    net_thread = start_network_thread(host, port, me_user, me_name, inbox, outbox)
    # ✅ 註冊退出處理器
    def cleanup():
        """確保退出時發送 BYE 訊息"""
        try:
            outbox.put({"type": "BYE"}, timeout=0.5)
            time.sleep(0.2)  # 給網路線程時間傳送
        except:
            pass
    
    atexit.register(cleanup)
    
    # ✅ 捕獲 Ctrl+C
    def signal_handler(sig, frame):
        print(f"[GUI] Caught signal {sig}, cleaning up...", flush=True)
        cleanup()
        pygame.quit()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    running = True
    seq = 0

    # 遊戲結束相關變數
    game_ended = False
    game_end_time = 0
    match_results = []

    # 供結束畫面使用
    winner_banner = ""       # 顯示：YOU WIN / YOU LOSE / DRAW
    winner_detail = ""       # 顯示：topout @ P1 / higher score / more lines / draw

    # 🔧 單調時鐘驅動
    last_drop_time = 0
    game_start_ticks = 0
    server_start_ms = 0

    # 鍵盤映射
    key_actions = {
        pygame.K_LEFT: "LEFT",
        pygame.K_RIGHT: "RIGHT",
        pygame.K_DOWN: "SOFT",
        pygame.K_UP: "CW",
        pygame.K_z: "CCW",
        pygame.K_SPACE: "HARD",
        pygame.K_c: "HOLD",
    }

    frame_count = 0
    print(f"[GUI] Entering main loop", flush=True)

    while running:
        frame_count += 1
        if frame_count % 300 == 0:
            print(f"[GUI] Frame {frame_count}, FPS: {clock.get_fps():.1f}", flush=True)

        # 1) 處理事件
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                print(f"[GUI] QUIT event", flush=True)
                cleanup()  # ✅ 主動清理
                running = False
            elif e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE:
                    print(f"[GUI] ESC pressed, closing", flush=True)
                    cleanup()  # ✅ 主動清理
                    running = False
                    continue

                if not game_ended and not state.get("is_spectator"):
                    action = key_actions.get(e.key)
                    if action:
                        outbox.put({
                            "type": "INPUT",
                            "username": me_user,
                            "seq": seq,
                            "ts": int(time.time() * 1000),
                            "action": action
                        })
                        seq += 1

        # 2) 處理網路訊息
        try:
            for _ in range(50):
                m = inbox.get_nowait()
                t = m.get("type")

                if t == "NET" and m.get("sub") == "CONNECTED":
                    state["connected"] = True
                    state["msg"] = "Connected. Waiting..."
                    print(f"[GUI] State: Connected", flush=True)

                elif t == "NET" and m.get("sub") == "ERROR":
                    state["msg"] = f"Error: {m.get('detail')}"
                    print(f"[GUI] State: Error - {m.get('detail')}", flush=True)

                elif t == "WELCOME":
                    state["my_role"] = m.get("role", "P1")
                    is_spectator = m.get("spectator", False)

                    # 🔧 接收節奏計劃
                    gravity_plan = m.get("gravityPlan", {})
                    state["gravity_plan"] = gravity_plan

                    if gravity_plan:
                        state["current_drop_ms"] = gravity_plan.get("initialDropMs", 500)
                        print(f"[GUI] Gravity plan received: {gravity_plan}", flush=True)

                    if is_spectator:
                        state["msg"] = f"🎥 觀戰模式 (只能觀看)"
                        print(f"[GUI] Spectator mode activated", flush=True)
                    else:
                        state["msg"] = f"Welcome! You are {state['my_role']}"
                        print(f"[GUI] State: Welcome as {state['my_role']}", flush=True)

                    state["is_spectator"] = is_spectator

                    # 🔧 初始化遊戲時鐘
                    game_start_ticks = pygame.time.get_ticks()
                    server_start_ms = int(time.time() * 1000)
                    last_drop_time = 0

                elif t == "SNAPSHOT":
                    apply_snapshot(m)
                    if not game_ended:
                        state["msg"] = f"Playing... {state['remain_sec']}s left"

                elif t == "GRAVITY_UPDATE":
                    new_drop_ms = m.get("dropMs")
                    reason = m.get("reason", "")
                    if new_drop_ms:
                        old_drop_ms = state.get("current_drop_ms", 500)
                        state["current_drop_ms"] = new_drop_ms
                        print(f"[GUI] Gravity updated: {old_drop_ms}ms -> {new_drop_ms}ms ({reason})", flush=True)
                        state["msg"] = f"⚡ Speed up! {new_drop_ms}ms"

                elif t == "MATCH_END":
                    reason = m.get("reason", "")
                    results = m.get("results", [])

                    # 新欄位：winnerRole / winnerUserId / winDetail
                    winner_role = m.get("winnerRole")      # 可能為 None（平手）
                    winner_username = m.get("winnerUsername")  # ⭐ 改名
                    win_detail = m.get("winDetail", "")

                    print(f"[GUI] Game Over: {reason}", flush=True)
                    print(f"[GUI] Results: {results}", flush=True)
                    print(f"[GUI] Winner: role={winner_role} user={winner_username} detail={win_detail}", flush=True)

                    state["winner_role"] = winner_role
                    state["winner_reason"] = win_detail if win_detail else reason

                    # 觀戰者：顯示贏家名稱；玩家：顯示 YOU WIN / YOU LOSE / DRAW
                    my_role = state.get("my_role")
                    if winner_role is None:
                        winner_banner = "DRAW"
                        state["winner_name"] = None
                    else:
                        # 找贏家名稱
                        win_name = None
                        for r in results:
                            if r.get("role") == winner_role:
                                win_name = r.get("name") or winner_role
                                break
                        state["winner_name"] = win_name

                        if my_role and not my_role.startswith("SPEC"):
                            if winner_role == my_role:
                                winner_banner = "YOU WIN"
                            else:
                                winner_banner = "YOU LOSE"
                        else:
                            winner_banner = f"{win_name} WINS" if win_name else f"{winner_role} WINS"

                    match_results = results
                    game_ended = True
                    game_end_time = time.time()

                    try:
                        outbox.put({"type": "BYE"})
                    except:
                        pass

                elif t == "SPECTATOR_KICKED":
                    reason = m.get("reason", "")
                    print(f"[GUI] Spectator kicked: {reason}", flush=True)
                    state["msg"] = f"You have been kicked: {reason}"
                    game_ended = True
                    game_end_time = time.time()

        except queue.Empty:
            pass

        # 遊戲結束後 5 秒自動關閉
        if game_ended and time.time() - game_end_time > 5:
            print(f"[GUI] Auto-closing after game end", flush=True)
            running = False

        # 🔧 單調時鐘預期掉落（僅視覺，不送命令）
        if not game_ended and game_start_ticks > 0:
            current_ticks = pygame.time.get_ticks()
            elapsed_game_ms = current_ticks - game_start_ticks
            current_drop_ms = state.get("current_drop_ms", 500)
            if elapsed_game_ms - last_drop_time >= current_drop_ms:
                last_drop_time = elapsed_game_ms

        # 3) 繪圖
        screen.fill((20, 20, 30))

        # 我的遊戲板（左側）
        my_display_name = state.get("my_name", "You")
        draw_board(screen, state["board_me"], 40, 80, cell_size=24, title=f"{my_display_name}")

        # 對手遊戲板（右側，縮小）
        op_display_name = state.get("op_name", "Opponent")
        draw_board(screen, state["board_op"], 550, 80, cell_size=16, title=f"{op_display_name}")

        # Hold 區域
        hold_x, hold_y = 340, 80
        pygame.draw.rect(screen, (60, 60, 60), (hold_x - 5, hold_y - 25, 90, 110))
        txt = font.render("HOLD", True, (255, 255, 255))
        screen.blit(txt, (hold_x, hold_y - 22))
        if state["hold"]:
            draw_piece_preview(screen, state["hold"], hold_x + 10, hold_y + 10, cell_size=16)

        # Next 區域
        next_x, next_y = 340, 220
        pygame.draw.rect(screen, (60, 60, 60), (next_x - 5, next_y - 25, 90, 200))
        txt = font.render("NEXT", True, (255, 255, 255))
        screen.blit(txt, (next_x, next_y - 22))
        for i, piece in enumerate(state["next_queue"][:3]):
            draw_piece_preview(screen, piece, next_x + 10, next_y + 10 + i * 60, cell_size=14)

        # 分數資訊
        info_y = 450
        info_texts = [
            f"Score: {state['score']}",
            f"Lines: {state['lines']}",
            f"Level: {state['level']}",
            f"Time: {state['remain_sec']}s",
            f"Speed: {state.get('current_drop_ms', 500)}ms",
        ]
        for i, text in enumerate(info_texts):
            txt = font_large.render(text, True, (255, 255, 100))
            screen.blit(txt, (40, info_y + i * 30))

        # 狀態訊息
        msg_txt = font.render(state["msg"], True, (200, 200, 200))
        screen.blit(msg_txt, (40, 30))
        if state.get("is_spectator"):
            spectator_txt = font_large.render("*** SPECTATOR MODE ***", True, (255, 200, 0))
            screen.blit(spectator_txt, (40, 55))

        # 控制說明
        if state.get("is_spectator"):
            controls = [
                "Spectator Mode:",
                "You can only watch",
                "ESC : Quit",
            ]
        else:
            controls = [
                "Controls:",
                "← → : Move",
                "↓ : Soft Drop",
                "↑ : Rotate CW",
                "Z : Rotate CCW",
                "Space : Hard Drop",
                "C : Hold",
                "ESC : Quit",
            ]
        for i, text in enumerate(controls):
            txt = font.render(text, True, (150, 150, 150))
            screen.blit(txt, (700, 380 + i * 22))

        # 如果遊戲結束，顯示結果覆蓋層（加入勝負橫幅）
        if game_ended:
            overlay = pygame.Surface((W, H))
            overlay.set_alpha(210)
            overlay.fill((0, 0, 0))
            screen.blit(overlay, (0, 0))

            y_offset = 130

            # 勝負橫幅
            if state["winner_role"] is None:
                banner_text = "DRAW"
                banner_color = (255, 215, 0)
            else:
                my_role = state.get("my_role")
                if my_role and not my_role.startswith("SPEC"):
                    banner_text = "YOU WIN" if state["winner_role"] == my_role else "YOU LOSE"
                    banner_color = (0, 255, 120) if banner_text == "YOU WIN" else (255, 80, 80)
                else:
                    # 觀戰者：顯示贏家名稱
                    win_name = state.get("winner_name") or state["winner_role"]
                    banner_text = f"{win_name} WINS"
                    banner_color = (0, 255, 120)

            title = font_title.render(banner_text, True, banner_color)
            title_rect = title.get_rect(center=(W//2, y_offset))
            screen.blit(title, title_rect)
            y_offset += 60

            # 勝利原因
            reason_text = state.get("winner_reason") or "game end"
            reason_txt = font.render(f"Reason: {reason_text}", True, (220, 220, 220))
            reason_rect = reason_txt.get_rect(center=(W//2, y_offset))
            screen.blit(reason_txt, reason_rect)
            y_offset += 20

            # 詳細成績
            y_offset += 40
            for result in match_results:
                role = result.get("role", "?")
                name = result.get("name", role)
                score = result.get("score", 0)
                lines = result.get("lines", 0)
                blocks = result.get("blocksCleared", 0)

                if role == state["my_role"] and not state.get("is_spectator"):
                    color = (0, 255, 100)
                    prefix = "YOU"
                else:
                    color = (200, 200, 200)
                    prefix = name

                result_text = f"{prefix}: Score {score} | Lines {lines} | Blocks {blocks}"
                txt = font_large.render(result_text, True, color)
                txt_rect = txt.get_rect(center=(W//2, y_offset))
                screen.blit(txt, txt_rect)
                y_offset += 36

            y_offset += 24
            remaining = max(0, int(5 - (time.time() - game_end_time)))
            if remaining > 0:
                countdown = font_large.render(f"Closing in {remaining}s...",
                                              True, (200, 200, 200))
            else:
                countdown = font_large.render("Closing...", True, (200, 200, 200))
            countdown_rect = countdown.get_rect(center=(W//2, y_offset))
            screen.blit(countdown, countdown_rect)

            y_offset += 32
            hint = font.render("(Press ESC to close now)", True, (150, 150, 150))
            hint_rect = hint.get_rect(center=(W//2, y_offset))
            screen.blit(hint, hint_rect)

        pygame.display.flip()
        clock.tick(60)

    print(f"[GUI] Exiting", flush=True)
    cleanup()  # ✅ 最後確保清理
    time.sleep(0.2)
    pygame.quit()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=False)
    ap.add_argument("--port", required=False)
    ap.add_argument("--user", default=None)  # ⭐ 改成 default=None
    ap.add_argument("--name", default=None)  # ⭐ 改成 default=None
    args = ap.parse_args()

    # ⭐ 優先使用環境變數中的真實帳號
    host = args.host or os.getenv("GAME_HOST", "127.0.0.1")
    port = args.port or os.getenv("GAME_PORT", "0")
    
    # ⭐ 關鍵：從環境變數讀取真實的玩家帳號
    user = args.user or os.getenv("PLAYER_USERNAME") or os.getenv("PLAYER_USER_ID", "guest")
    name = args.name or os.getenv("PLAYER_NAME") or user

    print(f"[GUI] Arguments: host={host} port={port} user={user} name={name}", flush=True)

    pygame_main(host, port, user, name)