"""Local account and per-player profile storage for the Spiral Warrior test server.

Passwords are stored as PBKDF2 hashes. Bearer tokens are random and only their
SHA-256 hashes are kept in SQLite. This module is intended for the local beta
backend; put the service behind HTTPS and review deployment settings before
exposing it to the internet.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


PBKDF2_ROUNDS = 310_000
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
# Keep fresh account profiles aligned with ws_probe_server.PROFILE_SCHEMA_VERSION.
# Explicit starter levels distinguish a newly created account from old profiles
# whose starter levels were previously implicit and level 10.
INITIAL_PROFILE_SCHEMA_VERSION = 10
STARTER_PART_IDS = (2001, 2002, 2003, 2004, 2005, 2006, 2028, 2029, 2030)
DIAMOND_ITEM_ID = 1227894834
GOLD_ITEM_ID = 1227894833
INITIAL_GOLD = 1500
INITIAL_GEMS = 100
TUTORIAL_UPGRADE_PART_ID = 2001
TUTORIAL_UPGRADE_PART_PIECES = 2
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,24}$")
EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@.]+(?:\.[^\s@.]+)+$")


class AccountError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class ProfileLock:
    """Serialize account-profile read/modify/write operations across both services."""

    def __init__(self, path: Path):
        self.path = path
        self._thread_lock = threading.RLock()
        self._local = threading.local()
        self._handle = None

    def __enter__(self):
        self._thread_lock.acquire()
        try:
            depth = getattr(self._local, "depth", 0)
            if depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = self.path.open("a+b")
                if os.name == "nt":
                    import msvcrt
                    self._handle.seek(0, os.SEEK_END)
                    if self._handle.tell() == 0:
                        self._handle.write(b"0")
                        self._handle.flush()
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            self._local.depth = depth + 1
        except Exception:
            if self._handle:
                self._handle.close()
                self._handle = None
            self._thread_lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            depth = max(0, getattr(self._local, "depth", 1) - 1)
            self._local.depth = depth
            if depth == 0 and self._handle:
                if os.name == "nt":
                    import msvcrt
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                self._handle.close()
                self._handle = None
        finally:
            self._thread_lock.release()


class AccountStore:
    def __init__(self, database: Path | str):
        self.database = Path(database).resolve()
        self.profile_root = self.database.parent / "accounts"
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.profile_root.mkdir(parents=True, exist_ok=True)
        self._locks_guard = threading.Lock()
        self._profile_locks: dict[int, ProfileLock] = {}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._transaction() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_id INTEGER UNIQUE,
                    username TEXT UNIQUE,
                    email TEXT UNIQUE,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_login_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_account_idx ON sessions(account_id);
                CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);
            """)
            connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _normalize_identifier(identifier: str) -> tuple[str | None, str | None]:
        value = identifier.strip()
        if "@" in value:
            email = value.casefold()
            if len(email) > 254 or not EMAIL_RE.fullmatch(email):
                raise AccountError("invalid_email", "Escribe un correo válido.")
            return None, email
        username = value.casefold()
        if not USERNAME_RE.fullmatch(value):
            raise AccountError("invalid_username", "El usuario debe tener entre 3 y 24 caracteres: letras, números, punto, guion o guion bajo.")
        return username, None

    @staticmethod
    def _validate_password(password: str) -> None:
        if not 8 <= len(password) <= 128:
            raise AccountError("invalid_password", "La contraseña debe tener entre 8 y 128 caracteres.")

    @staticmethod
    def _public_account(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "player_id": int(row["player_id"]),
            "username": row["username"],
            "email": row["email"],
        }

    def register(self, identifier: str, password: str) -> tuple[str, dict[str, Any]]:
        if not isinstance(identifier, str) or not isinstance(password, str):
            raise AccountError("invalid_request", "Falta el usuario o la contraseña.")
        username, email = self._normalize_identifier(identifier)
        self._validate_password(password)
        salt = secrets.token_bytes(16)
        password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
        now = int(time.time())
        with self._transaction() as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO accounts(username,email,password_salt,password_hash,created_at,last_login_at) VALUES(?,?,?,?,?,?)",
                    (username, email, salt, password_hash, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise AccountError("account_exists", "Ya existe una cuenta con ese usuario o correo.", 409) from exc
            account_id = int(cursor.lastrowid)
            player_id = 100_000_000 + account_id
            connection.execute("UPDATE accounts SET player_id=? WHERE id=?", (player_id, account_id))
            row = connection.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        assert row is not None
        self._ensure_profile(account_id, player_id)
        token = self._new_session(account_id)
        return token, self._public_account(row)

    def login(self, identifier: str, password: str) -> tuple[str, dict[str, Any]]:
        if not isinstance(identifier, str) or not isinstance(password, str):
            raise AccountError("invalid_credentials", "Usuario o contraseña incorrectos.", 401)
        username, email = self._normalize_identifier(identifier)
        if not password or len(password) > 128:
            raise AccountError("invalid_credentials", "Usuario o contraseña incorrectos.", 401)
        with self._transaction() as connection:
            if email:
                row = connection.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM accounts WHERE username=? OR email=? LIMIT 1",
                    (username, username),
                ).fetchone()
            if row is None:
                raise AccountError("invalid_credentials", "Usuario o contraseña incorrectos.", 401)
            candidate = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes(row["password_salt"]), PBKDF2_ROUNDS
            )
            if not hmac.compare_digest(candidate, bytes(row["password_hash"])):
                raise AccountError("invalid_credentials", "Usuario o contraseña incorrectos.", 401)
            now = int(time.time())
            connection.execute("UPDATE accounts SET last_login_at=? WHERE id=?", (now, row["id"]))
            account = self._public_account(row)
        self._ensure_profile(account["id"], account["player_id"])
        return self._new_session(account["id"]), account

    def _new_session(self, account_id: int) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = int(time.time())
        with self._transaction() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            connection.execute(
                "INSERT INTO sessions(token_hash,account_id,expires_at,created_at) VALUES(?,?,?,?)",
                (token_hash, account_id, now + SESSION_TTL_SECONDS, now),
            )
        return token

    def resolve(self, token: str | None) -> dict[str, Any] | None:
        if not token or len(token) > 128:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = int(time.time())
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT a.* FROM sessions s JOIN accounts a ON a.id=s.account_id WHERE s.token_hash=? AND s.expires_at>?",
                (token_hash, now),
            ).fetchone()
        return self._public_account(row) if row else None

    def profile_path(self, account_id: int) -> Path:
        return self.profile_root / str(int(account_id)) / "profile.json"

    def mail_path(self, account_id: int) -> Path:
        return self.profile_root / str(int(account_id)) / "mail.json"

    def lock_for(self, account_id: int) -> ProfileLock:
        account_id = int(account_id)
        with self._locks_guard:
            return self._profile_locks.setdefault(
                account_id,
                ProfileLock(self.profile_root / str(account_id) / ".profile.lock"),
            )

    def _ensure_profile(self, account_id: int, player_id: int) -> None:
        path = self.profile_path(account_id)
        with self.lock_for(account_id):
            if path.exists():
                return
            state = {
                "schema_version": INITIAL_PROFILE_SCHEMA_VERSION,
                "player_id": int(player_id),
                "inventory_items": {
                    "2009": 5, "2027": 5, "1194340405": 5,
                    str(GOLD_ITEM_ID): INITIAL_GOLD,
                    str(DIAMOND_ITEM_ID): INITIAL_GEMS,
                },
                "part_levels": {str(part_id): 1 for part_id in STARTER_PART_IDS},
                # Existing local client data requires two part-2001 pieces
                # for the first tutorial upgrade. This is a one-time new-
                # account allowance, distinct from any championship reward.
                "part_fragments": {str(TUTORIAL_UPGRADE_PART_ID): TUTORIAL_UPGRADE_PART_PIECES},
                "tutorial_upgrade_resources_granted": True,
                    # Community Championship fallback: seven blue tickets for
                    # a newly registered account. Never replenish on login.
                    "championship_state": {
                        "league_index": 1, "race_score": 0, "win_mask": 0,
                        "losses": 0, "wins": 0, "streak": 0,
                        "max_streak": 0, "vouchers": 7, "lose_times": {},
                        "active_match": None,
                        "period_started_at": int(time.time()),
                        "period_id": int(time.time()), "periods_completed": 0,
                        "last_promotion_period": 0, "last_period_rank": 0,
                    },
                "level": 1,
                "exp": 0,
                "power": 0,
                "task_score": 0,
                "task_score_exchange_process": 0,
                "achievement_total_score": 0,
                "achievement_reward_applied": False,
                "level_task_state": {},
                "progression_counters": {},
                "task_exchange_claims": [],
                "roguelike_keys": 4,
                "roguelike_map_state": None,
                "local_purchases": {"limits": {}, "recharge_marks": [], "processed_nonces": {}},
                "daily_task_claimed": False,
                "daily_gold_day": -1,
                "daily_gold_earned": 0,
                "save_boxes": {},
                "achievement_claimed": False,
                "guide_step": 0,
            }
            temporary = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
            temporary.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, path)
