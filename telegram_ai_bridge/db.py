from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .models import ActiveSession, LocalSession


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self) -> sqlite3.Connection:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_last_sessions (
                    chat_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    sessions_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS active_sessions (
                    chat_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    process_id INTEGER,
                    alias TEXT,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_message_at TEXT NOT NULL,
                    model TEXT
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    chat_id TEXT,
                    user_id TEXT,
                    event_type TEXT NOT NULL,
                    detail TEXT
                );

                CREATE TABLE IF NOT EXISTS chat_settings (
                    chat_id TEXT PRIMARY KEY,
                    trace_mode TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_active_bindings (
                    chat_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    process_id INTEGER,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_message_at TEXT NOT NULL,
                    model TEXT,
                    PRIMARY KEY (chat_id, alias)
                );

                CREATE TABLE IF NOT EXISTS chat_default_binding (
                    chat_id TEXT PRIMARY KEY,
                    alias TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS managed_worktrees (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    repo_root TEXT NOT NULL,
                    worktree_path TEXT NOT NULL,
                    branch TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "active_sessions", "model", "TEXT")
            self._ensure_column(conn, "chat_active_bindings", "model", "TEXT")
            self._ensure_column(conn, "chat_settings", "kill_switch", "TEXT")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def save_last_sessions(self, chat_id: int, sessions: list[LocalSession]) -> None:
        payload = []
        for s in sessions:
            row = asdict(s)
            row["last_active_at"] = s.last_active_at.isoformat()
            payload.append(row)
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO chat_last_sessions (chat_id, created_at, sessions_json)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    sessions_json = excluded.sessions_json
                """,
                (str(chat_id), datetime.now(timezone.utc).isoformat(), json.dumps(payload)),
            )

    def load_last_sessions(self, chat_id: int) -> list[LocalSession]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT sessions_json FROM chat_last_sessions WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
        if not row:
            return []
        out: list[LocalSession] = []
        for item in json.loads(row["sessions_json"]):
            item["last_active_at"] = datetime.fromisoformat(item["last_active_at"])
            out.append(LocalSession(**item))
        return out

    def upsert_active_session(self, session: ActiveSession) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO active_sessions (
                    chat_id, provider, session_id, cwd, process_id, alias, mode, status, created_at, last_message_at, model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    provider = excluded.provider,
                    session_id = excluded.session_id,
                    cwd = excluded.cwd,
                    process_id = excluded.process_id,
                    alias = excluded.alias,
                    mode = excluded.mode,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    last_message_at = excluded.last_message_at,
                    model = excluded.model
                """,
                (
                    session.telegram_chat_id,
                    session.provider,
                    session.session_id,
                    session.cwd,
                    session.process_id,
                    session.alias,
                    session.mode,
                    session.status,
                    session.created_at.isoformat(),
                    session.last_message_at.isoformat(),
                    session.model,
                ),
            )

    def get_active_session(self, chat_id: int) -> ActiveSession | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM active_sessions WHERE chat_id = ?",
                (str(chat_id),),
            ).fetchone()
        if not row:
            return None
        return ActiveSession(
            telegram_chat_id=row["chat_id"],
            provider=row["provider"],
            session_id=row["session_id"],
            cwd=row["cwd"],
            process_id=row["process_id"],
            alias=row["alias"],
            mode=row["mode"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_message_at=datetime.fromisoformat(row["last_message_at"]),
            model=row["model"],
        )

    def list_active_sessions(self) -> list[ActiveSession]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM active_sessions").fetchall()
        out: list[ActiveSession] = []
        for row in rows:
            out.append(
                ActiveSession(
                    telegram_chat_id=row["chat_id"],
                    provider=row["provider"],
                    session_id=row["session_id"],
                    cwd=row["cwd"],
                    process_id=row["process_id"],
                    alias=row["alias"],
                    mode=row["mode"],
                    status=row["status"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    last_message_at=datetime.fromisoformat(row["last_message_at"]),
                    model=row["model"],
                )
            )
        return out

    def delete_active_session(self, chat_id: int) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM active_sessions WHERE chat_id = ?", (str(chat_id),))

    def log_audit_event(
        self, event_type: str, detail: str, chat_id: int | str | None = None, user_id: int | str | None = None
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (created_at, chat_id, user_id, event_type, detail)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    str(chat_id) if chat_id is not None else None,
                    str(user_id) if user_id is not None else None,
                    event_type,
                    detail,
                ),
            )

    def set_trace_mode(self, chat_id: int, mode: str) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO chat_settings (chat_id, trace_mode)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    trace_mode = excluded.trace_mode
                """,
                (str(chat_id), mode),
            )

    def get_trace_mode(self, chat_id: int) -> str:
        with self._connection() as conn:
            row = conn.execute("SELECT trace_mode FROM chat_settings WHERE chat_id = ?", (str(chat_id),)).fetchone()
        if not row:
            return "off"
        value = str(row["trace_mode"]).strip().lower()
        return value if value in {"on", "off"} else "off"

    # Kill switch is stored as a global setting using the sentinel chat_id "global".
    _GLOBAL_CHAT_ID = "global"

    def get_kill_switch(self) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT kill_switch FROM chat_settings WHERE chat_id = ?",
                (self._GLOBAL_CHAT_ID,),
            ).fetchone()
        if not row or row["kill_switch"] is None:
            return False
        return str(row["kill_switch"]).strip().lower() == "on"

    def set_kill_switch(self, enabled: bool) -> None:
        value = "on" if enabled else "off"
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO chat_settings (chat_id, trace_mode, kill_switch)
                VALUES (?, '', ?)
                ON CONFLICT(chat_id) DO UPDATE SET kill_switch = excluded.kill_switch
                """,
                (self._GLOBAL_CHAT_ID, value),
            )

    def append_history(self, chat_id: int, alias: str, role: str, content: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO chat_history (chat_id, alias, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(chat_id), alias, role, content, datetime.now(timezone.utc).isoformat()),
            )

    def get_history(self, chat_id: int, alias: str, limit: int = 10) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT role, content, created_at FROM chat_history
                WHERE chat_id = ? AND alias = ?
                ORDER BY id DESC LIMIT ?
                """,
                (str(chat_id), alias, limit),
            ).fetchall()
        return [{"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in reversed(rows)]

    def save_worktree(self, chat_id: int, alias: str, repo_root: str, worktree_path: str, branch: str | None) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO managed_worktrees (chat_id, alias, repo_root, worktree_path, branch, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(chat_id), alias, repo_root, worktree_path, branch, datetime.now(timezone.utc).isoformat()),
            )

    def get_worktree(self, chat_id: int, alias: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM managed_worktrees WHERE chat_id = ? AND alias = ? ORDER BY id DESC LIMIT 1",
                (str(chat_id), alias),
            ).fetchone()
        if not row:
            return None
        return {"repo_root": row["repo_root"], "worktree_path": row["worktree_path"], "branch": row["branch"]}

    def delete_worktree(self, chat_id: int, alias: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM managed_worktrees WHERE chat_id = ? AND alias = ?",
                (str(chat_id), alias),
            )

    def upsert_binding(self, session: ActiveSession) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO chat_active_bindings (
                    chat_id, alias, provider, session_id, cwd, process_id, mode, status, created_at, last_message_at, model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, alias) DO UPDATE SET
                    provider = excluded.provider,
                    session_id = excluded.session_id,
                    cwd = excluded.cwd,
                    process_id = excluded.process_id,
                    mode = excluded.mode,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    last_message_at = excluded.last_message_at,
                    model = excluded.model
                """,
                (
                    session.telegram_chat_id,
                    session.alias or "default",
                    session.provider,
                    session.session_id,
                    session.cwd,
                    session.process_id,
                    session.mode,
                    session.status,
                    session.created_at.isoformat(),
                    session.last_message_at.isoformat(),
                    session.model,
                ),
            )

    def get_binding(self, chat_id: int, alias: str) -> ActiveSession | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM chat_active_bindings WHERE chat_id = ? AND alias = ?",
                (str(chat_id), alias),
            ).fetchone()
        if not row:
            return None
        return ActiveSession(
            telegram_chat_id=row["chat_id"],
            provider=row["provider"],
            session_id=row["session_id"],
            cwd=row["cwd"],
            process_id=row["process_id"],
            alias=row["alias"],
            mode=row["mode"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_message_at=datetime.fromisoformat(row["last_message_at"]),
            model=row["model"],
        )

    def list_bindings(self, chat_id: int) -> list[ActiveSession]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_active_bindings WHERE chat_id = ? ORDER BY created_at ASC",
                (str(chat_id),),
            ).fetchall()
        out: list[ActiveSession] = []
        for row in rows:
            out.append(
                ActiveSession(
                    telegram_chat_id=row["chat_id"],
                    provider=row["provider"],
                    session_id=row["session_id"],
                    cwd=row["cwd"],
                    process_id=row["process_id"],
                    alias=row["alias"],
                    mode=row["mode"],
                    status=row["status"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    last_message_at=datetime.fromisoformat(row["last_message_at"]),
                    model=row["model"],
                )
            )
        return out

    def list_all_bindings(self) -> list[ActiveSession]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM chat_active_bindings").fetchall()
        out: list[ActiveSession] = []
        for row in rows:
            out.append(
                ActiveSession(
                    telegram_chat_id=row["chat_id"],
                    provider=row["provider"],
                    session_id=row["session_id"],
                    cwd=row["cwd"],
                    process_id=row["process_id"],
                    alias=row["alias"],
                    mode=row["mode"],
                    status=row["status"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    last_message_at=datetime.fromisoformat(row["last_message_at"]),
                    model=row["model"],
                )
            )
        return out

    def delete_binding(self, chat_id: int, alias: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM chat_active_bindings WHERE chat_id = ? AND alias = ?", (str(chat_id), alias))

    def delete_bindings_for_chat(self, chat_id: int) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM chat_active_bindings WHERE chat_id = ?", (str(chat_id),))
            conn.execute("DELETE FROM chat_default_binding WHERE chat_id = ?", (str(chat_id),))

    def set_default_alias(self, chat_id: int, alias: str) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO chat_default_binding (chat_id, alias)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    alias = excluded.alias
                """,
                (str(chat_id), alias),
            )

    def get_default_alias(self, chat_id: int) -> str | None:
        with self._connection() as conn:
            row = conn.execute("SELECT alias FROM chat_default_binding WHERE chat_id = ?", (str(chat_id),)).fetchone()
        if not row:
            return None
        return str(row["alias"])
