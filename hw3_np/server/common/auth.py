# server/common/auth.py
import secrets, time
from . import db

TOKENS_FILE = "tokens.json"
SESSION_TTL = 7 * 24 * 60 * 60  # Token 有效期 7 天

def _now():
    return int(time.time())

def issue_token(user: str, role: str):
    """
    單一 Session 策略：
    - 若此帳號已有有效 token，則拒絕新登入（不覆蓋）
    """
    tokens = db.load(TOKENS_FILE, {})
    now = _now()

    # 清掉過期 token，同時檢查是否有現存 Session
    for tok, info in list(tokens.items()):
        exp = info.get("expires_at", 0)
        if exp and exp < now:
            tokens.pop(tok)
        elif info.get("user") == user and info.get("role") == role:
            # 👇 已有有效登入 → 拒絕
            return None

    # 👇 尚未登入 → 生成 token
    tok = secrets.token_hex(16)
    tokens[tok] = {
        "user": user,
        "role": role,
        "issued_at": now,
        "expires_at": now + SESSION_TTL
    }
    db.save(TOKENS_FILE, tokens)
    return tok


def verify_token(token: str, role: str | None = None):
    tokens = db.load(TOKENS_FILE, {})
    info = tokens.get(token)
    if not info:
        return None

    now = _now()
    if info["expires_at"] < now:
        tokens.pop(token, None)
        db.save(TOKENS_FILE, tokens)
        return None

    if role and info.get("role") != role:
        return None

    return info


def revoke_token(token: str):
    tokens = db.load(TOKENS_FILE, {})
    if token in tokens:
        tokens.pop(token)
        db.save(TOKENS_FILE, tokens)
        return True
    return False
