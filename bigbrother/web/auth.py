"""Users, sessions and login rate limiting for the web app.

- Passwords are hashed with scrypt (stdlib), never stored or logged in clear.
- Session tokens are random; only their SHA-256 is stored, so a copy of the
  database can't be used to log in.
- Failed logins are rate limited per client address and per username.
- The first account is created on a setup page that needs a one-time code
  printed in the web service's log, so whoever reaches the page first can't
  claim the install unless they can also read the server's logs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import hmac
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash     TEXT NOT NULL,
    created_ts  REAL NOT NULL,
    pw_changed_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_ts  REAL NOT NULL,
    last_seen_ts REAL NOT NULL,
    expires_ts  REAL NOT NULL,
    persistent  INTEGER NOT NULL DEFAULT 0,
    ip          TEXT NOT NULL DEFAULT '',
    user_agent  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""

# Columns added after the first release (old databases are migrated on open).
USER_COLUMNS = {
    "role": "TEXT NOT NULL DEFAULT 'admin'",       # admin | member
    "totp_secret": "TEXT",                         # base32; set once two-factor is on
    "totp_pending": "TEXT",                        # during setup, before the first code
    "totp_last": "INTEGER NOT NULL DEFAULT 0",     # last 30 s step used (no replays)
    "recovery": "TEXT NOT NULL DEFAULT '[]'",      # SHA-256 of unused recovery codes
    "prefs": "TEXT NOT NULL DEFAULT '{}'",         # notification settings etc. (JSON)
}
ROLES = ("admin", "member")

USERNAME = re.compile(r"^[A-Za-z0-9_.@-]{2,64}$")
MIN_PASSWORD = 10
SESSION_DAYS = 30            # "remember me"
SHORT_SESSION_HOURS = 12     # browser session
TOUCH_EVERY = 300            # seconds between last-seen updates

# scrypt cost: ~50-100 ms per hash on a server core; memory 32 MB.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1


class AuthError(ValueError):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                        maxmem=64 * 1024 * 1024, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        want = base64.b64decode(dk)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(want))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


# A valid hash of a random password: checked when the username doesn't exist, so a
# failed login takes the same time either way (no username probing by timing).
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def check_new_password(password: str, username: str = "") -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Password must be at least {MIN_PASSWORD} characters.")
    if len(password) > 256:
        raise AuthError("Password is too long.")
    if username and password.lower() == username.lower():
        raise AuthError("Password can't be the same as the username.")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str = "admin"
    twofa: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass(frozen=True)
class Session:
    user: User
    token_hash: str
    expires_ts: float
    persistent: bool


class UserStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(users)")}
        for name, decl in USER_COLUMNS.items():
            if name not in cols:
                self._db.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")

    _USER_SQL = "SELECT id, username, role, totp_secret IS NOT NULL FROM users"

    @staticmethod
    def _user(row) -> User:
        return User(row[0], row[1], row[2], bool(row[3]))

    # -- users -------------------------------------------------------------------
    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def list(self) -> list[User]:
        with self._lock:
            rows = self._db.execute(self._USER_SQL + " ORDER BY username").fetchall()
        return [self._user(r) for r in rows]

    def get(self, username: str) -> User | None:
        with self._lock:
            row = self._db.execute(self._USER_SQL + " WHERE username = ?", (username,)).fetchone()
        return self._user(row) if row else None

    def overview(self) -> list[dict]:
        """Users with last activity, for the admin's Users page."""
        with self._lock:
            rows = self._db.execute(
                "SELECT u.username, u.role, u.totp_secret IS NOT NULL, u.created_ts, MAX(s.last_seen_ts), "
                "COUNT(s.token_hash) FROM users u LEFT JOIN sessions s ON s.user_id = u.id AND s.expires_ts > ? "
                "GROUP BY u.id ORDER BY u.username", (time.time(),)).fetchall()
        return [{"username": r[0], "role": r[1], "twofa": bool(r[2]), "created": r[3], "last_seen": r[4],
                 "devices": r[5]} for r in rows]

    def add(self, username: str, password: str, role: str = "admin") -> User:
        username = username.strip()
        if not USERNAME.match(username):
            raise AuthError("Username: 2-64 letters, digits, or . _ @ -")
        if role not in ROLES:
            raise AuthError("Role must be admin or member.")
        check_new_password(password, username)
        h, now = hash_password(password), time.time()
        with self._lock:
            try:
                cur = self._db.execute(
                    "INSERT INTO users (username, pw_hash, created_ts, pw_changed_ts, role) VALUES (?,?,?,?,?)",
                    (username, h, now, now, role))
            except sqlite3.IntegrityError as exc:
                raise AuthError("That username is taken.") from exc
        return User(cur.lastrowid, username, role)

    def _admins(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]

    def delete(self, username: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
            if row and row[0] == "admin" and self._admins() <= 1:
                raise AuthError("This is the only admin. Make someone else an admin first.")
            return self._db.execute("DELETE FROM users WHERE username = ?", (username,)).rowcount > 0

    def set_role(self, username: str, role: str) -> None:
        if role not in ROLES:
            raise AuthError("Role must be admin or member.")
        with self._lock:
            row = self._db.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
            if not row:
                raise AuthError("No such user.")
            if row[0] == "admin" and role != "admin" and self._admins() <= 1:
                raise AuthError("This is the only admin. Make someone else an admin first.")
            self._db.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))

    # -- preferences ----------------------------------------------------------------
    def prefs(self, username: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT prefs FROM users WHERE username = ?", (username,)).fetchone()
        try:
            return json.loads(row[0]) if row else {}
        except json.JSONDecodeError:
            return {}

    def update_prefs(self, username: str, changes: dict) -> dict:
        with self._lock:
            row = self._db.execute("SELECT prefs FROM users WHERE username = ?", (username,)).fetchone()
            if not row:
                raise AuthError("No such user.")
            prefs = json.loads(row[0] or "{}")
            for k, v in changes.items():
                if v is None:
                    prefs.pop(k, None)
                else:
                    prefs[k] = v
            self._db.execute("UPDATE users SET prefs = ? WHERE username = ?", (json.dumps(prefs), username))
        return prefs

    def all_prefs(self) -> dict[str, dict]:
        with self._lock:
            rows = self._db.execute("SELECT username, prefs FROM users").fetchall()
        out = {}
        for name, raw in rows:
            try:
                out[name] = json.loads(raw or "{}")
            except json.JSONDecodeError:
                out[name] = {}
        return out

    # -- two-factor sign-in (TOTP, RFC 6238) ---------------------------------------
    def totp_begin(self, username: str) -> str:
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        with self._lock:
            self._db.execute("UPDATE users SET totp_pending = ? WHERE username = ?", (secret, username))
        return secret

    def totp_confirm(self, username: str, code: str) -> list[str]:
        """Turn two-factor on if `code` matches the pending secret. Returns recovery codes
        (shown once)."""
        with self._lock:
            row = self._db.execute("SELECT totp_pending FROM users WHERE username = ?", (username,)).fetchone()
        if not row or not row[0]:
            raise AuthError("Start two-factor setup first.")
        step = totp_match(row[0], code)
        if step is None:
            raise AuthError("That code didn't match. Check the time on your phone and try the newest code.")
        codes = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(8)]
        with self._lock:
            self._db.execute(
                "UPDATE users SET totp_secret = totp_pending, totp_pending = NULL, totp_last = ?, recovery = ? "
                "WHERE username = ?", (step, json.dumps([_token_hash(c) for c in codes]), username))
        return codes

    def totp_disable(self, username: str) -> None:
        with self._lock:
            self._db.execute("UPDATE users SET totp_secret = NULL, totp_pending = NULL, recovery = '[]' "
                             "WHERE username = ?", (username,))

    def check_second_factor(self, user: User, code: str) -> bool:
        """A current authenticator code (each usable once) or an unused recovery code."""
        code = code.strip().replace(" ", "")
        with self._lock:
            row = self._db.execute("SELECT totp_secret, totp_last, recovery FROM users WHERE id = ?",
                                   (user.id,)).fetchone()
            if not row or not row[0]:
                return False
            secret, last, recovery = row
            step = totp_match(secret, code)
            if step is not None and step > last:
                self._db.execute("UPDATE users SET totp_last = ? WHERE id = ?", (step, user.id))
                return True
            hashes = json.loads(recovery or "[]")
            h = _token_hash(code.lower())
            if h in hashes:
                hashes.remove(h)
                self._db.execute("UPDATE users SET recovery = ? WHERE id = ?", (json.dumps(hashes), user.id))
                return True
        return False

    def set_password(self, username: str, password: str) -> None:
        check_new_password(password, username)
        h = hash_password(password)
        with self._lock:
            row = self._db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
            if not row:
                raise AuthError("No such user.")
            self._db.execute("UPDATE users SET pw_hash = ?, pw_changed_ts = ? WHERE id = ?",
                             (h, time.time(), row[0]))
            # A password change signs out every other device.
            self._db.execute("DELETE FROM sessions WHERE user_id = ?", (row[0],))

    def authenticate(self, username: str, password: str) -> User | None:
        with self._lock:
            row = self._db.execute("SELECT id, username, role, totp_secret IS NOT NULL, pw_hash FROM users "
                                   "WHERE username = ?", (username.strip(),)).fetchone()
        if row is None:
            verify_password(password, _DUMMY_HASH)
            return None
        return self._user(row) if verify_password(password, row[4]) else None

    # -- sessions ----------------------------------------------------------------
    def create_session(self, user: User, persistent: bool, ip: str = "", user_agent: str = "") -> str:
        token, now = secrets.token_urlsafe(32), time.time()
        ttl = SESSION_DAYS * 86400 if persistent else SHORT_SESSION_HOURS * 3600
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions (token_hash, user_id, created_ts, last_seen_ts, expires_ts, persistent, "
                "ip, user_agent) VALUES (?,?,?,?,?,?,?,?)",
                (_token_hash(token), user.id, now, now, now + ttl, int(persistent), ip[:64], user_agent[:200]))
            self._db.execute("DELETE FROM sessions WHERE expires_ts < ?", (now,))
        return token

    def session(self, token: str | None) -> Session | None:
        """Look up a session; slides its expiry forward while it's being used."""
        if not token or len(token) > 100:
            return None
        th, now = _token_hash(token), time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT u.id, u.username, s.expires_ts, s.persistent, s.last_seen_ts, u.role, "
                "u.totp_secret IS NOT NULL FROM sessions s "
                "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (th,)).fetchone()
            if row is None:
                return None
            uid, name, expires, persistent, last_seen, role, twofa = row
            if expires < now:
                self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
                return None
            if now - last_seen > TOUCH_EVERY:
                ttl = SESSION_DAYS * 86400 if persistent else SHORT_SESSION_HOURS * 3600
                expires = now + ttl
                self._db.execute("UPDATE sessions SET last_seen_ts = ?, expires_ts = ? WHERE token_hash = ?",
                                 (now, expires, th))
        return Session(User(uid, name, role, bool(twofa)), th, expires, bool(persistent))

    def end_session(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def end_all_sessions(self, user: User, except_hash: str | None = None) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                                    (user.id, except_hash or "")).rowcount

    def sessions_for(self, user: User) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT token_hash, created_ts, last_seen_ts, ip, user_agent FROM sessions "
                "WHERE user_id = ? AND expires_ts > ? ORDER BY last_seen_ts DESC", (user.id, time.time())).fetchall()
        return [{"id": r[0][:12], "created": r[1], "last_seen": r[2], "ip": r[3], "user_agent": r[4],
                 "hash": r[0]} for r in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()


class RateLimiter:
    """Failed-login limiter: at most `per_ip` failures per client and `per_user` per
    username within `window` seconds. Successful logins don't count."""

    def __init__(self, per_ip: int = 10, per_user: int = 10, window: float = 900, clock=time.monotonic):
        self.per_ip, self.per_user, self.window, self.clock = per_ip, per_user, window, clock
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key: str) -> deque[float]:
        q, cutoff = self._fails[key], self.clock() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def retry_after(self, ip: str, username: str) -> float:
        """Seconds until another attempt is allowed (0 = allowed now)."""
        with self._lock:
            waits = []
            for key, limit in ((f"ip:{ip}", self.per_ip), (f"user:{username.lower()}", self.per_user)):
                q = self._recent(key)
                if len(q) >= limit:
                    waits.append(q[0] + self.window - self.clock())
            return max(waits, default=0.0)

    def failed(self, ip: str, username: str) -> None:
        with self._lock:
            now = self.clock()
            self._fails[f"ip:{ip}"].append(now)
            self._fails[f"user:{username.lower()}"].append(now)
            if len(self._fails) > 10000:  # don't grow forever under a spray of usernames
                for k in [k for k, q in self._fails.items() if not self._recent(k)]:
                    del self._fails[k]


def new_setup_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def setup_code_matches(given: str, code: str) -> bool:
    norm = lambda s: re.sub(r"[^A-Z0-9]", "", s.upper())  # noqa: E731
    return bool(code) and hmac.compare_digest(norm(given), norm(code))


# -- TOTP -------------------------------------------------------------------------------

def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_match(secret_b32: str, code: str, now: float | None = None, drift: int = 1) -> int | None:
    """The 30 s step `code` belongs to (allowing one step of clock drift), or None."""
    code = code.strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return None
    step = int((now if now is not None else time.time()) // 30)
    for s in range(step - drift, step + drift + 1):
        if hmac.compare_digest(_hotp(secret_b32, s), code):
            return s
    return None


def totp_uri(secret_b32: str, username: str, issuer: str = "BigBrother") -> str:
    from urllib.parse import quote
    return (f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret_b32}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits=6&period=30")
