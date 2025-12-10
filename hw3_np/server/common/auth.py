# common/auth.py
import uuid
import time
import threading

_LOCK = threading.RLock()

# token -> { "user": "abc", "role": "player", "ts": 12345 }
SESSIONS = {}

# (role, user) -> token
USER_ACTIVE = {}

# token 存活時間（None = 不檢查）
TOKEN_TTL = None


def _cleanup_expired():
    if TOKEN_TTL is None:
        return

    now = time.time()
    expired = []

    for token, info in list(SESSIONS.items()):
        if now - info["ts"] > TOKEN_TTL:
            expired.append(token)

    for token in expired:
        info = SESSIONS.pop(token, None)
        if not info:
            continue
        key = (info["role"], info["user"])
        if USER_ACTIVE.get(key) == token:
            USER_ACTIVE.pop(key, None)


def issue_token(user: str, role: str) -> str | None:
    """
    發 token：
      - 如果這個 user 在這個 role 已登入 → 回傳 None（拒絕新的登入）
      - 沒登入 → 建立新登入
    """
    _cleanup_expired()

    key = (role, user)

    with _LOCK:
        if key in USER_ACTIVE:
            # ★★★ 重要：拒絕新的登入，不踢掉舊的
            return None

        token = uuid.uuid4().hex
        info = {
            "user": user,
            "role": role,
            "ts": time.time(),
        }
        SESSIONS[token] = info
        USER_ACTIVE[key] = token
        return token


def verify_token(token: str | None, role: str | None = None):
    if not token:
        return None

    _cleanup_expired()

    with _LOCK:
        info = SESSIONS.get(token)
        if not info:
            return None

        if role and info["role"] != role:
            return None

        return dict(info)


def revoke_token(token: str | None):
    if not token:
        return

    with _LOCK:
        info = SESSIONS.pop(token, None)
        if not info:
            return

        key = (info["role"], info["user"])
        if USER_ACTIVE.get(key) == token:
            USER_ACTIVE.pop(key, None)
