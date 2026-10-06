"""Event journal: SQLite in WAL mode holding sessions, turns and events."""

import asyncio
import json
import sqlite3
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Self, TypeVar

from pydantic import BaseModel

from konklawe.events import AgentEvent, State, utc_now

T = TypeVar("T")

PAGE_SIZE = 500

MIGRATIONS: list[str] = [
    # Version 1.
    """
    CREATE TABLE sessions (
      id                  TEXT PRIMARY KEY,
      agent               TEXT NOT NULL,
      provider            TEXT NOT NULL,
      provider_session_id TEXT,
      workdir             TEXT NOT NULL,
      status              TEXT NOT NULL,
      created_at          TEXT NOT NULL,
      updated_at          TEXT NOT NULL
    );

    CREATE TABLE turns (
      id           TEXT PRIMARY KEY,
      session_id   TEXT NOT NULL REFERENCES sessions(id),
      idx          INTEGER NOT NULL,
      provider     TEXT NOT NULL,
      prompt       TEXT NOT NULL,
      status       TEXT NOT NULL,
      started_at   TEXT NOT NULL,
      finished_at  TEXT,
      result_text  TEXT,
      is_error     INTEGER,
      usage_json   TEXT,
      UNIQUE (session_id, idx)
    );

    CREATE TABLE events (
      seq        INTEGER PRIMARY KEY AUTOINCREMENT,
      ts         TEXT NOT NULL,
      session_id TEXT NOT NULL REFERENCES sessions(id),
      turn_id    TEXT REFERENCES turns(id),
      agent      TEXT NOT NULL,
      provider   TEXT NOT NULL,
      type       TEXT NOT NULL,
      data_json  TEXT NOT NULL,
      raw        TEXT
    );
    CREATE INDEX idx_events_session_seq ON events(session_id, seq);
    """,
]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Session(BaseModel):
    id: str
    agent: str
    provider: str  # current executor
    provider_session_id: str | None
    workdir: Path
    status: State
    created_at: datetime
    updated_at: datetime
    turn_count: int = 0


class Turn(BaseModel):
    id: str
    session_id: str
    idx: int
    provider: str  # executor of this particular turn
    prompt: str
    status: State
    started_at: datetime
    finished_at: datetime | None = None
    result_text: str | None = None
    is_error: bool | None = None
    usage: dict[str, Any] | None = None


_SESSION_COLUMNS = """
    s.id, s.agent, s.provider, s.provider_session_id, s.workdir, s.status,
    s.created_at, s.updated_at,
    (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id) AS turn_count
"""


class EventStore:
    """Journal access. All methods run SQLite calls in a worker thread under one lock.

    Use `await EventStore.open(path)` (or `async with`); construct directly only with an
    already configured connection.
    """

    def __init__(self, conn: sqlite3.Connection, *, store_raw: bool = True) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self._store_raw = store_raw

    @classmethod
    async def open(cls, path: Path, *, store_raw: bool = True) -> Self:
        def connect() -> sqlite3.Connection:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            return conn

        store = cls(await asyncio.to_thread(connect), store_raw=store_raw)
        await store.migrate()
        return store

    async def close(self) -> None:
        await self._run(lambda conn: conn.close())

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def locked() -> T:
            with self._lock:
                return fn(self._conn)

        return await asyncio.to_thread(locked)

    async def migrate(self) -> int:
        """Bring the schema up to date; returns the resulting schema version."""

        def migrate(conn: sqlite3.Connection) -> int:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            current = row["version"] if row else 0
            for version in range(current + 1, len(MIGRATIONS) + 1):
                with _transaction(conn):
                    for statement in _statements(MIGRATIONS[version - 1]):
                        conn.execute(statement)
                    conn.execute("DELETE FROM schema_version")
                    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            return len(MIGRATIONS)

        return await self._run(migrate)

    # Sessions

    async def create_session(
        self, agent: str, provider: str, workdir: Path, status: State = State.IDLE
    ) -> Session:
        now = utc_now()
        session = Session(
            id=new_id(),
            agent=agent,
            provider=provider,
            provider_session_id=None,
            workdir=workdir,
            status=status,
            created_at=now,
            updated_at=now,
        )

        def insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO sessions (id, agent, provider, provider_session_id, workdir, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session.id,
                    agent,
                    provider,
                    None,
                    str(workdir),
                    status.value,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

        await self._run(insert)
        return session

    async def update_session(
        self,
        session_id: str,
        *,
        status: State | None = None,
        provider: str | None = None,
        provider_session_id: str | None = None,
    ) -> Session:
        changes: dict[str, str] = {"updated_at": utc_now().isoformat()}
        if status is not None:
            changes["status"] = status.value
        if provider is not None:
            changes["provider"] = provider
        if provider_session_id is not None:
            changes["provider_session_id"] = provider_session_id
        assignments = ", ".join(f"{column} = ?" for column in changes)

        def update(conn: sqlite3.Connection) -> Session:
            cursor = conn.execute(
                f"UPDATE sessions SET {assignments} WHERE id = ?", (*changes.values(), session_id)
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown session '{session_id}'")
            return _fetch_session(conn, session_id)

        return await self._run(update)

    async def get_session(self, session_id: str) -> Session | None:
        def get(conn: sqlite3.Connection) -> Session | None:
            try:
                return _fetch_session(conn, session_id)
            except KeyError:
                return None

        return await self._run(get)

    async def list_sessions(self) -> list[Session]:
        """All sessions, most recently active first."""

        def list_all(conn: sqlite3.Connection) -> list[Session]:
            rows = conn.execute(
                f"SELECT {_SESSION_COLUMNS} FROM sessions s ORDER BY s.updated_at DESC, s.id"
            ).fetchall()
            return [_session_from_row(row) for row in rows]

        return await self._run(list_all)

    # Turns

    async def create_turn(
        self, session_id: str, provider: str, prompt: str, status: State = State.STARTING
    ) -> Turn:
        now = utc_now()

        def insert(conn: sqlite3.Connection) -> Turn:
            with _transaction(conn):
                row = conn.execute(
                    "SELECT COALESCE(MAX(idx), -1) + 1 AS idx FROM turns WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                turn = Turn(
                    id=new_id(),
                    session_id=session_id,
                    idx=row["idx"],
                    provider=provider,
                    prompt=prompt,
                    status=status,
                    started_at=now,
                )
                conn.execute(
                    "INSERT INTO turns (id, session_id, idx, provider, prompt, status, started_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        turn.id,
                        session_id,
                        turn.idx,
                        provider,
                        prompt,
                        status.value,
                        now.isoformat(),
                    ),
                )
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?", (now.isoformat(), session_id)
                )
            return turn

        return await self._run(insert)

    async def finish_turn(
        self,
        turn_id: str,
        *,
        status: State,
        result_text: str | None = None,
        is_error: bool | None = None,
        usage: dict[str, Any] | None = None,
    ) -> Turn:
        now = utc_now().isoformat()

        def finish(conn: sqlite3.Connection) -> Turn:
            cursor = conn.execute(
                "UPDATE turns SET status = ?, finished_at = ?, result_text = ?, is_error = ?,"
                " usage_json = ? WHERE id = ?",
                (
                    status.value,
                    now,
                    result_text,
                    None if is_error is None else int(is_error),
                    None if usage is None else json.dumps(usage, ensure_ascii=False),
                    turn_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown turn '{turn_id}'")
            return _turn_from_row(
                conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            )

        return await self._run(finish)

    async def list_turns(self, session_id: str) -> list[Turn]:
        def list_all(conn: sqlite3.Connection) -> list[Turn]:
            rows = conn.execute(
                "SELECT * FROM turns WHERE session_id = ? ORDER BY idx", (session_id,)
            ).fetchall()
            return [_turn_from_row(row) for row in rows]

        return await self._run(list_all)

    # Events

    async def append_event(self, event: AgentEvent) -> AgentEvent:
        """Store `event` and return it with the journal-assigned `seq`."""
        stored = (
            event
            if self._store_raw or event.raw is None
            else event.model_copy(update={"raw": None})
        )

        def insert(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "INSERT INTO events"
                " (ts, session_id, turn_id, agent, provider, type, data_json, raw)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored.ts.isoformat(),
                    stored.session_id,
                    stored.turn_id,
                    stored.agent,
                    stored.provider,
                    stored.type.value,
                    json.dumps(stored.data, ensure_ascii=False),
                    stored.raw,
                ),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

        seq = await self._run(insert)
        return stored.model_copy(update={"seq": seq})

    async def iter_events(
        self, session_id: str | None = None, after_seq: int = 0, limit: int | None = None
    ) -> AsyncIterator[AgentEvent]:
        """Yield events in `seq` order, optionally for one session, starting after `after_seq`."""
        remaining = limit
        cursor_seq = after_seq
        while remaining is None or remaining > 0:
            page = PAGE_SIZE if remaining is None else min(PAGE_SIZE, remaining)
            events = await self._run(
                lambda conn, after=cursor_seq, size=page: _fetch_events(
                    conn, session_id, after, size
                )
            )
            for event in events:
                yield event
            if len(events) < page:
                return
            assert events[-1].seq is not None
            cursor_seq = events[-1].seq
            if remaining is not None:
                remaining -= len(events)


def _statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";") if statement.strip()]


class _transaction:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK on an autocommit connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: object, *_: object) -> None:
        self._conn.execute("ROLLBACK" if exc_type else "COMMIT")


def _fetch_session(conn: sqlite3.Connection, session_id: str) -> Session:
    row = conn.execute(
        f"SELECT {_SESSION_COLUMNS} FROM sessions s WHERE s.id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown session '{session_id}'")
    return _session_from_row(row)


def _session_from_row(row: sqlite3.Row) -> Session:
    return Session.model_validate(dict(row))


def _turn_from_row(row: sqlite3.Row) -> Turn:
    values = dict(row)
    usage_json = values.pop("usage_json")
    values["usage"] = None if usage_json is None else json.loads(usage_json)
    if values["is_error"] is not None:
        values["is_error"] = bool(values["is_error"])
    return Turn.model_validate(values)


def _fetch_events(
    conn: sqlite3.Connection, session_id: str | None, after_seq: int, limit: int
) -> list[AgentEvent]:
    query = "SELECT * FROM events WHERE seq > ?"
    params: list[Any] = [after_seq]
    if session_id is not None:
        query += " AND session_id = ?"
        params.append(session_id)
    query += " ORDER BY seq LIMIT ?"
    params.append(limit)
    return [_event_from_row(row) for row in conn.execute(query, params).fetchall()]


def _event_from_row(row: sqlite3.Row) -> AgentEvent:
    return AgentEvent(
        seq=row["seq"],
        ts=datetime.fromisoformat(row["ts"]),
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        agent=row["agent"],
        provider=row["provider"],
        type=row["type"],
        data=json.loads(row["data_json"]),
        raw=row["raw"],
    )
