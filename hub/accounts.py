"""Built-in accounts: passwords, sessions and sign-in."""

import hashlib
import re
import secrets

from . import db
from .util import now
from .db import db_lock


SESSION_DAYS = 30
PBKDF2_ROUNDS = 600_000
MIN_PASSWORD = 10
LOCK_AFTER, LOCK_SECONDS = 5, 300
login_failures: dict[str, list] = {}  # username -> [count, locked_until]


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt, digest = stored.split("$")
        test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
    except ValueError:
        return False
    return secrets.compare_digest(test.hex(), digest)


DUMMY_HASH = None  # checked for unknown usernames, so timing doesn't reveal which accounts exist


def valid_password(password):
    if len(str(password or "")) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    return str(password)


def user_count() -> int:
    return db.conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]


def admin_count() -> int:
    return db.conn.execute("SELECT COUNT(*) n FROM users WHERE role='admin'").fetchone()["n"]


def public_user(row):
    return {k: row[k] for k in ("id", "username", "role", "created_at", "last_login")}


def create_user(username, password, role="member"):
    username = str(username or "").strip()
    if not re.fullmatch(r"[\w.@+-]{2,64}", username):
        raise ValueError("username must be 2-64 letters, digits or . @ + - _")
    if role not in ("admin", "member"):
        raise ValueError("role must be admin or member")
    pw = hash_password(valid_password(password))
    uid = secrets.token_hex(6)
    with db_lock:
        if db.conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError("that username is taken")
        db.conn.execute("INSERT INTO users(id, username, pw, role, created_at) VALUES (?,?,?,?,?)", (uid, username, pw, role, now()))
        db.conn.commit()
    return public_user(db.conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def new_session(user_id) -> str:
    token = secrets.token_urlsafe(32)
    with db_lock:
        db.conn.execute("DELETE FROM sessions WHERE expires < ?", (now(),))
        db.conn.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user_id, now() + SESSION_DAYS * 86400))
        db.conn.commit()
    return token


def session_row(token):
    if not token:
        return None
    return db.conn.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.hash=? AND s.expires>?",
                           (hashlib.sha256(token.encode()).hexdigest(), now())).fetchone()


def login(username, password):
    global DUMMY_HASH
    key = str(username or "").strip().lower()
    state = login_failures.get(key)
    if state and state[1] > now():
        raise PermissionError("too many attempts; try again in a few minutes")
    row = db.conn.execute("SELECT * FROM users WHERE username=?", (key,)).fetchone()
    DUMMY_HASH = DUMMY_HASH or hash_password(secrets.token_hex(8))
    good = check_password(str(password or ""), row["pw"] if row else DUMMY_HASH) and row is not None
    if not good:
        count = (state[0] if state else 0) + 1
        login_failures[key] = [count, now() + LOCK_SECONDS if count >= LOCK_AFTER else 0]
        if len(login_failures) > 10000:
            login_failures.clear()
        raise PermissionError("wrong username or password")
    login_failures.pop(key, None)
    with db_lock:
        db.conn.execute("UPDATE users SET last_login=? WHERE id=?", (now(), row["id"]))
        db.conn.commit()
    return public_user(row), new_session(row["id"])


def delete_user(uid, acting_id):
    row = db.conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        raise LookupError("user not found")
    if uid == acting_id:
        raise ValueError("you can't remove your own account")
    if row["role"] == "admin" and admin_count() <= 1:
        raise ValueError("this is the last admin")
    with db_lock:
        db.conn.execute("DELETE FROM users WHERE id=?", (uid,))
        db.conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        db.conn.commit()


def set_password(uid, password, keep_token=None):
    """Change a password and sign out every other session."""
    pw = hash_password(valid_password(password))
    keep = hashlib.sha256(keep_token.encode()).hexdigest() if keep_token else ""
    with db_lock:
        db.conn.execute("UPDATE users SET pw=? WHERE id=?", (pw, uid))
        db.conn.execute("DELETE FROM sessions WHERE user_id=? AND hash!=?", (uid, keep))
        db.conn.commit()
