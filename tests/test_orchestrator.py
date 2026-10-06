import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from konklawe.agents import AgentSpec
from konklawe.bus import EventBus
from konklawe.config import Settings
from konklawe.events import AgentEvent, EventType, State
from konklawe.orchestrator import Orchestrator, OrchestratorError
from konklawe.store import EventStore

FIXTURES = Path(__file__).parent / "fixtures" / "claude"


def fake_agent(name: str, **options: Any) -> AgentSpec:
    return AgentSpec(
        name=name,
        description="d",
        provider="fake",
        role_prompt="unused",
        source_path=Path(f"agents/{name}.md"),
        provider_options={
            "fixture": str(FIXTURES / "simple_text.jsonl"),
            "fixture_resume": str(FIXTURES / "resume_turn2.jsonl"),
            "delay_ms": 0,
            **options,
        },
    )


AGENTS = {
    "slow": fake_agent("slow", delay_ms=4),  # ~0.6 s per turn
    "quick": fake_agent("quick"),
    "stuck": fake_agent("stuck", hang=True, spawn_child=True),
}


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[EventStore]:
    async with await EventStore.open(tmp_path / "k.db") as opened:
        yield opened


def orchestrator(store: EventStore, **settings: Any) -> Orchestrator:
    config = Settings(turn_timeout_s=30, kill_grace_s=1, **settings)
    return Orchestrator(config, store, EventBus(), AGENTS)


async def all_events(store: EventStore) -> list[AgentEvent]:
    return [e async for e in store.iter_events()]


def max_concurrency(events: list[AgentEvent]) -> int:
    """Highest number of turns between their `starting` and final status, in journal order."""
    running = peak = 0
    for event in events:
        if event.type is not EventType.STATUS:
            continue
        if event.data["state"] == "starting":
            running += 1
            peak = max(peak, running)
        elif event.data["state"] in {"done", "failed", "cancelled", "limited"}:
            running -= 1
    return peak


def is_running(event: object) -> bool:
    return isinstance(event, AgentEvent) and event.data.get("state") == "running"


async def wait_running(orch: Orchestrator, session_id: str) -> None:
    # Subscribe before looking at the journal so the event cannot slip between the two.
    with orch.bus.subscribe(session_id) as live:
        if any([is_running(e) async for e in orch.store.iter_events(session_id=session_id)]):
            return
        async for event in live:
            if is_running(event):
                return


async def test_new_session(
    store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "work").mkdir()
    orch = orchestrator(store)

    session = await orch.new_session("quick", Path("work"))

    assert session.workdir == (tmp_path / "work").resolve()
    assert session.workdir.is_absolute()
    assert (session.agent, session.provider, session.status) == ("quick", "fake", State.IDLE)
    assert await store.get_session(session.id) is not None


async def test_new_session_validation(store: EventStore, tmp_path: Path) -> None:
    orch = orchestrator(store)

    with pytest.raises(OrchestratorError, match="unknown agent 'nobody'"):
        await orch.new_session("nobody", tmp_path)
    with pytest.raises(OrchestratorError, match="does not exist"):
        await orch.new_session("quick", tmp_path / "missing")
    with pytest.raises(OrchestratorError, match="unknown session"):
        await orch.send("missing", "hi")


async def test_second_turn_resumes_provider_session(store: EventStore, tmp_path: Path) -> None:
    orch = orchestrator(store)
    session = await orch.new_session("quick", tmp_path)

    first = await orch.send(session.id, "remember 42")
    second = await orch.send(session.id, "what number?")

    assert first.status is State.DONE and second.status is State.DONE
    assert first.provider_session_id == "8cf3e9e9-b897-49ad-b998-cf945d86b5ea"
    # The fake replays the resume recording only when it receives a resume id.
    assert second.result_text == "42"
    assert [t.idx for t in await store.list_turns(session.id)] == [0, 1]


async def test_two_sessions_run_in_parallel(store: EventStore, tmp_path: Path) -> None:
    orch = orchestrator(store, max_parallel_turns=3)
    first = await orch.new_session("slow", tmp_path)
    second = await orch.new_session("slow", tmp_path)

    outcomes = await asyncio.gather(orch.send(first.id, "a"), orch.send(second.id, "b"))

    assert [o.status for o in outcomes] == [State.DONE, State.DONE]
    assert max_concurrency(await all_events(store)) == 2


async def test_semaphore_limits_parallel_turns(store: EventStore, tmp_path: Path) -> None:
    orch = orchestrator(store, max_parallel_turns=2)
    sessions = [await orch.new_session("slow", tmp_path) for _ in range(3)]

    outcomes = await asyncio.gather(*(orch.send(s.id, "go") for s in sessions))

    assert all(o.status is State.DONE for o in outcomes)
    assert max_concurrency(await all_events(store)) == 2


async def test_turns_of_one_session_run_one_at_a_time(store: EventStore, tmp_path: Path) -> None:
    orch = orchestrator(store, max_parallel_turns=3)
    session = await orch.new_session("slow", tmp_path)

    first, second = await asyncio.gather(orch.send(session.id, "1"), orch.send(session.id, "2"))

    assert max_concurrency(await all_events(store)) == 1
    assert first.result_text and first.result_text.startswith("SQLite")
    assert second.result_text == "42"  # it waited and resumed the first turn's session


async def test_cancel_running_turn(store: EventStore, tmp_path: Path) -> None:
    orch = orchestrator(store)
    session = await orch.new_session("stuck", tmp_path)
    assert orch.cancel(session.id) is False

    send = asyncio.create_task(orch.send(session.id, "hang"))
    await asyncio.wait_for(wait_running(orch, session.id), timeout=10)
    assert orch.active_sessions == [session.id]
    assert orch.cancel(session.id) is True
    outcome = await asyncio.wait_for(send, timeout=10)

    assert outcome.status is State.CANCELLED
    assert orch.active_sessions == []
    assert (await store.get_session(session.id)).status is State.IDLE  # type: ignore[union-attr]


async def test_shutdown_leaves_no_processes(store: EventStore, tmp_path: Path) -> None:
    orch = orchestrator(store)
    sessions = [await orch.new_session("stuck", tmp_path) for _ in range(2)]
    sends = [asyncio.create_task(orch.send(s.id, "hang")) for s in sessions]
    for session in sessions:
        await asyncio.wait_for(wait_running(orch, session.id), timeout=10)

    await asyncio.wait_for(orch.shutdown(), timeout=10)

    outcomes = await asyncio.gather(*sends)
    assert [o.status for o in outcomes] == [State.CANCELLED, State.CANCELLED]
    for pid in spawned_pids(await all_events(store)):
        assert await wait_dead(pid)
    with pytest.raises(OrchestratorError, match="shut down"):
        await orch.send(sessions[0].id, "again")


async def test_cancelling_the_caller_kills_the_turn(store: EventStore, tmp_path: Path) -> None:
    orch = orchestrator(store)
    session = await orch.new_session("stuck", tmp_path)

    send = asyncio.create_task(orch.send(session.id, "hang"))
    await asyncio.wait_for(wait_running(orch, session.id), timeout=10)
    send.cancel()
    with pytest.raises(asyncio.CancelledError):
        await send

    [turn] = await store.list_turns(session.id)
    assert turn.status is State.CANCELLED
    for pid in spawned_pids(await all_events(store)):
        assert await wait_dead(pid)


async def test_changed_agent_provider_is_rejected(store: EventStore, tmp_path: Path) -> None:
    orch = orchestrator(store)
    session = await orch.new_session("quick", tmp_path)
    orch.agents["quick"] = orch.agents["quick"].model_copy(update={"provider": "claude"})

    with pytest.raises(OrchestratorError, match="start a new session"):
        await orch.send(session.id, "hi")


def spawned_pids(events: list[AgentEvent]) -> list[int]:
    pids = []
    for event in events:
        if event.type is EventType.RAW and event.raw and "fake_child" in event.raw:
            info = json.loads(event.raw)
            pids += [info["parent_pid"], info["pid"]]
    assert pids
    return pids


async def wait_dead(pid: int) -> bool:
    try:
        async with asyncio.timeout(5):
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
                await asyncio.sleep(0.05)
    except TimeoutError:
        return False
