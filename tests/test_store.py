import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from konklawe import store as store_module
from konklawe.events import AgentEvent, EventType, ParsedEvent, State
from konklawe.store import EventStore, Session


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[EventStore]:
    async with await EventStore.open(tmp_path / "data" / "konklawe.db") as opened:
        yield opened


async def make_session(store: EventStore, agent: str = "echo") -> Session:
    return await store.create_session(agent, "fake", Path("/work"))


def event(session: Session, text: str, turn_id: str | None = None) -> AgentEvent:
    return AgentEvent.from_parsed(
        ParsedEvent.text_delta(text, raw=f'{{"text": "{text}"}}'),
        session_id=session.id,
        turn_id=turn_id,
        agent=session.agent,
        provider=session.provider,
    )


async def collect(store: EventStore, **kwargs: object) -> list[AgentEvent]:
    return [e async for e in store.iter_events(**kwargs)]  # type: ignore[arg-type]


async def test_open_creates_wal_database(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "konklawe.db"
    async with await EventStore.open(path) as store:
        mode = await store._run(lambda c: c.execute("PRAGMA journal_mode").fetchone()[0])
        foreign_keys = await store._run(lambda c: c.execute("PRAGMA foreign_keys").fetchone()[0])

    assert path.is_file()
    assert mode == "wal"
    assert foreign_keys == 1


async def test_migrate_is_idempotent(store: EventStore) -> None:
    assert await store.migrate() == 1
    assert await store.migrate() == 1
    version = await store._run(lambda c: c.execute("SELECT version FROM schema_version").fetchall())
    assert [row[0] for row in version] == [1]


async def test_seq_is_increasing(store: EventStore) -> None:
    session = await make_session(store)

    stored = [await store.append_event(event(session, str(i))) for i in range(5)]

    seqs = [e.seq for e in stored]
    assert all(isinstance(s, int) for s in seqs)
    assert seqs == sorted(seqs) and len(set(seqs)) == 5
    assert stored[0].data == {"text": "0"}


async def test_iter_events_after_seq_and_by_session(store: EventStore) -> None:
    first = await make_session(store, "a")
    second = await make_session(store, "b")
    stored = []
    for i in range(6):
        stored.append(await store.append_event(event(first if i % 2 == 0 else second, str(i))))

    everything = await collect(store)
    assert [e.data["text"] for e in everything] == ["0", "1", "2", "3", "4", "5"]

    after = await collect(store, after_seq=stored[2].seq)
    assert [e.data["text"] for e in after] == ["3", "4", "5"]

    only_first = await collect(store, session_id=first.id)
    assert [e.data["text"] for e in only_first] == ["0", "2", "4"]
    assert all(e.agent == "a" for e in only_first)

    limited = await collect(store, session_id=second.id, after_seq=stored[1].seq, limit=1)
    assert [e.data["text"] for e in limited] == ["3"]


async def test_iter_events_pages_through_large_journal(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "PAGE_SIZE", 7)
    session = await make_session(store)
    stored = [await store.append_event(event(session, str(i))) for i in range(30)]

    assert len(await collect(store)) == 30
    assert len(await collect(store, limit=20)) == 20
    tail = await collect(store, limit=5, after_seq=stored[26].seq)
    assert [e.data["text"] for e in tail] == ["27", "28", "29"]


async def test_concurrent_appends_lose_nothing(store: EventStore) -> None:
    session = await make_session(store)

    async def writer(n: int) -> list[AgentEvent]:
        return [await store.append_event(event(session, f"{n}-{i}")) for i in range(25)]

    results = await asyncio.gather(*(writer(n) for n in range(20)))

    seqs = [e.seq for batch in results for e in batch]
    assert len(seqs) == len(set(seqs)) == 500
    stored = await collect(store, session_id=session.id)
    assert len(stored) == 500
    assert {e.data["text"] for e in stored} == {f"{n}-{i}" for n in range(20) for i in range(25)}


async def test_reopen_and_read(tmp_path: Path) -> None:
    path = tmp_path / "konklawe.db"
    async with await EventStore.open(path) as store:
        session = await make_session(store)
        turn = await store.create_turn(session.id, "fake", "hello")
        written = await store.append_event(event(session, "persisted", turn.id))

    async with await EventStore.open(path) as reopened:
        events = await collect(reopened)
        loaded = await reopened.get_session(session.id)

    assert events == [written]
    assert events[0].type is EventType.TEXT_DELTA
    assert events[0].raw == '{"text": "persisted"}'
    assert loaded is not None and loaded.turn_count == 1


async def test_raw_lines_can_be_skipped(tmp_path: Path) -> None:
    async with await EventStore.open(tmp_path / "k.db", store_raw=False) as store:
        session = await make_session(store)
        returned = await store.append_event(event(session, "x"))
        [stored] = await collect(store)

    assert returned.raw is None
    assert stored.raw is None


async def test_sessions(store: EventStore) -> None:
    session = await store.create_session("architekt", "claude", Path("/repo"))

    assert session.status is State.IDLE
    assert session.provider_session_id is None
    assert len(session.id) == 12

    updated = await store.update_session(
        session.id, status=State.LIMITED, provider_session_id="abc-123"
    )
    assert updated.status is State.LIMITED
    assert updated.provider_session_id == "abc-123"
    assert updated.provider == "claude"
    assert updated.workdir == Path("/repo")
    assert updated.updated_at >= session.updated_at

    other = await store.create_session("recenzent", "claude", Path("/repo"))
    assert [s.id for s in await store.list_sessions()] == [other.id, session.id]
    assert await store.get_session("missing") is None
    with pytest.raises(KeyError):
        await store.update_session("missing", status=State.IDLE)


async def test_turns(store: EventStore) -> None:
    session = await make_session(store)

    first = await store.create_turn(session.id, "fake", "one")
    second = await store.create_turn(session.id, "fake", "two")
    finished = await store.finish_turn(
        first.id, status=State.DONE, result_text="ok", is_error=False, usage={"output_tokens": 3}
    )

    assert (first.idx, second.idx) == (0, 1)
    assert first.status is State.STARTING
    assert finished.status is State.DONE
    assert finished.finished_at is not None
    assert (finished.result_text, finished.is_error, finished.usage) == (
        "ok",
        False,
        {"output_tokens": 3},
    )
    turns = await store.list_turns(session.id)
    assert [t.prompt for t in turns] == ["one", "two"]
    assert turns[1].finished_at is None
    assert (await store.get_session(session.id)).turn_count == 2  # type: ignore[union-attr]


async def test_event_requires_existing_session(store: EventStore) -> None:
    orphan = AgentEvent.from_parsed(
        ParsedEvent.stderr("x"), session_id="nope", turn_id=None, agent="a", provider="p"
    )

    with pytest.raises(Exception, match="FOREIGN KEY"):
        await store.append_event(orphan)
