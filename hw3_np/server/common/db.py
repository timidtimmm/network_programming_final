# server/common/db.py
import json, threading
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DATA_DIR.mkdir(exist_ok=True, parents=True)
_lock = threading.RLock()

def _path(name: str) -> Path:
    return DATA_DIR / name

def load(name: str, default=None):
    p = _path(name)
    with _lock:
        if not p.exists():
            return default if default is not None else {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default if default is not None else {}

def save(name: str, obj):
    p = _path(name)
    with _lock:
        p.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
