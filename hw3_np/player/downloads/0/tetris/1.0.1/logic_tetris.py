
from __future__ import annotations
import random
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict

# Board: height x width = 20 x 10
W, H = 10, 20

# Piece IDs: 0=empty, 1..7 = I,J,L,O,S,T,Z
PIECES = ['I','J','L','O','S','T','Z']
PID = {p:i+1 for i,p in enumerate(PIECES)}

# Rotation states for each piece as list of cell offsets (x,y)
# Origin near top-left of a 4x4 box; simple wall-kick: try x offsets [-2,-1,0,1,2]
SHAPES = {
    'I': [
        [(0,1),(1,1),(2,1),(3,1)],       # ----
        [(2,0),(2,1),(2,2),(2,3)],       # |
        [(0,2),(1,2),(2,2),(3,2)],
        [(1,0),(1,1),(1,2),(1,3)],
    ],
    'O': [
        [(1,0),(2,0),(1,1),(2,1)],
        [(1,0),(2,0),(1,1),(2,1)],
        [(1,0),(2,0),(1,1),(2,1)],
        [(1,0),(2,0),(1,1),(2,1)],
    ],
    'T': [
        [(1,0),(0,1),(1,1),(2,1)],
        [(1,0),(1,1),(2,1),(1,2)],
        [(0,1),(1,1),(2,1),(1,2)],
        [(1,0),(0,1),(1,1),(1,2)],
    ],
    'L': [
        [(2,0),(0,1),(1,1),(2,1)],
        [(1,0),(1,1),(1,2),(2,2)],
        [(0,1),(1,1),(2,1),(0,2)],
        [(0,0),(1,0),(1,1),(1,2)],
    ],
    'J': [
        [(0,0),(0,1),(1,1),(2,1)],
        [(1,0),(2,0),(1,1),(1,2)],
        [(0,1),(1,1),(2,1),(2,2)],
        [(1,0),(1,1),(0,2),(1,2)],
    ],
    'S': [
        [(1,0),(2,0),(0,1),(1,1)],
        [(1,0),(1,1),(2,1),(2,2)],
        [(1,1),(2,1),(0,2),(1,2)],
        [(0,0),(0,1),(1,1),(1,2)],
    ],
    'Z': [
        [(0,0),(1,0),(1,1),(2,1)],
        [(2,0),(1,1),(2,1),(1,2)],
        [(0,1),(1,1),(1,2),(2,2)],
        [(1,0),(0,1),(1,1),(0,2)],
    ],
}

@dataclass
class Active:
    shape: str
    rot: int
    x: int
    y: int
    can_hold: bool = True

@dataclass
class Snapshot:
    board: List[List[int]]
    active: Optional[Dict]
    hold: Optional[str]
    nextq: List[str]
    score: int
    lines: int
    level: int
    blocks_cleared: int = 0

class TetrisEngine:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.board = [[0 for _ in range(W)] for _ in range(H)]
        self.hold: Optional[str] = None
        self.queue: List[str] = []
        self.active: Optional[Active] = None
        self.score = 0
        self.lines = 0
        self.level = 1
        self.blocks_cleared = 0
        self.topout = False
        self.combo = 0          # 當前 combo 連續數
        self.max_combo = 0      # 最高 combo
        self.last_cleared = False
        self._fill_queue()
        self.spawn()
        
    def clear_lines(self):
        cleared = 0
        # 原本的消行邏輯，例如:
        new_board = []
        for row in self.board:
            if all(row):
                cleared += 1
            else:
                new_board.append(row)
        if cleared > 0:
            # 有消行
            self.lines += cleared
            self.score += self._score_for_lines(cleared)

            # combo update
            if self.last_cleared:
                self.combo += 1
            else:
                self.combo = 1
            self.max_combo = max(self.max_combo, self.combo)
            self.last_cleared = True
        else:
            # 沒有消行就重設 combo
            self.last_cleared = False
            self.combo = 0

    def _fill_queue(self):
        while len(self.queue) < 8:
            bag = PIECES[:]
            self.rng.shuffle(bag)
            self.queue.extend(bag)

    def spawn(self):
        self._fill_queue()
        shape = self.queue.pop(0)
        # spawn near top center
        a = Active(shape=shape, rot=0, x=3, y=0, can_hold=True)
        if self._collides(a, dx=0, dy=0, droplast=False):
            # try spawn higher (negative y) to allow entry
            a.y = -1
            if self._collides(a, 0, 0, False):
                self.topout = True
                self.active = None
                return
        self.active = a

    def _cells(self, a: Active, rot=None, x=None, y=None):
        rot = a.rot if rot is None else rot
        x = a.x if x is None else x
        y = a.y if y is None else y
        pts = SHAPES[a.shape][rot]
        for (dx, dy) in pts:
            yield (x + dx, y + dy)

    def _collides(self, a: Active, dx: int, dy: int, droplast: bool) -> bool:
        for (cx, cy) in self._cells(a, x=a.x+dx, y=a.y+dy):
            if cx < 0 or cx >= W or cy >= H:
                return True
            if cy >= 0 and self.board[cy][cx] != 0:
                return True
        return False

    def move(self, dx: int, dy: int):
        if not self.active: return False
        a = self.active
        if not self._collides(a, dx, dy, False):
            a.x += dx; a.y += dy
            return True
        return False

    def rotate(self, dir: int):
        if not self.active: return False
        a = self.active
        newr = (a.rot + dir) % 4
        # simple kicks
        for kick in [0, -1, 1, -2, 2]:
            if not self._collides(a, dx=kick, dy=0, droplast=False) and \
               not any(self._out_of_bounds(nx, ny) or (ny>=0 and self.board[ny][nx]!=0)
                       for (nx, ny) in self._cells(a, rot=newr, x=a.x+kick, y=a.y)):
                a.rot = newr; a.x += kick
                return True
        return False

    def _out_of_bounds(self, x, y):
        return x<0 or x>=W or y>=H

    def soft_drop(self):
        if not self.active: return False
        if self.move(0, 1): return True
        self.lock()
        return False

    def hard_drop(self):
        if not self.active: return
        while self.move(0,1): pass
        self.lock()

    def hold_swap(self):
        if not self.active or not self.active.can_hold: return
        cur = self.active.shape
        if self.hold is None:
            self.hold = cur
            self.spawn()
        else:
            self.active.shape, self.hold = self.hold, cur
            self.active.rot = 0
            self.active.x, self.active.y = 3, 0
            # adjust spawn collision rules
            if self._collides(self.active, 0, 0, False):
                self.active.y = -1
                if self._collides(self.active, 0, 0, False):
                    self.topout = True
                    self.active = None; return
        if self.active:
            self.active.can_hold = False

    def lock(self):
        if not self.active: return
        a = self.active
        pid = PID[a.shape]
        for (cx, cy) in self._cells(a):
            if cy < 0:  # locked above top -> topout
                self.topout = True
                self.active = None
                return
            self.board[cy][cx] = pid
    
        # === 清行 ===
        cleared = 0
        new_board = []
        for r in range(H):
            if all(self.board[r][c] != 0 for c in range(W)):
                cleared += 1
            else:
                new_board.append(self.board[r])
        while len(new_board) < H:
            new_board.insert(0, [0]*W)
        self.board = new_board
    
        if cleared:
            # 累計行數 / 方塊數 / 分數 / 等級
            self.lines += cleared
            self.blocks_cleared += cleared * W
            self.score += [0,100,300,500,800][cleared]
            self.level = (self.lines // 10) + 1
    
            # === Combo / MaxCombo 更新（重點）===
            if self.last_cleared:
                self.combo += 1          # 連續清行 → combo+1
            else:
                self.combo = 1           # 這是新一段 combo 的第一下
            if self.combo > self.max_combo:
                self.max_combo = self.combo
            self.last_cleared = True
        else:
            # 沒清行就中斷 combo
            self.combo = 0
            self.last_cleared = False
    
        # 下一顆
        self.spawn()


    def snapshot(self) -> Snapshot:
        act = None
        if self.active:
            act = {"shape": self.active.shape, "x": self.active.x, "y": self.active.y, "rot": self.active.rot}
        return Snapshot(
            board=[row[:] for row in self.board],
            active=act,
            hold=self.hold,
            nextq=self.queue[:3],
            score=self.score,
            lines=self.lines,
            level=self.level,
            # extra
            blocks_cleared=self.blocks_cleared,
        )
