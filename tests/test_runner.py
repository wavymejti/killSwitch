import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from konklawe.adapters import get_adapter
from konklawe.agents import AgentSpec
from konklawe.bus import EventBus
from konklawe.config import Settings
from konklawe.events import AgentEvent, EventType, State
from konklawe.runner import TurnOutcome, run_turn
from konklawe.store import EventStore, Session

FIXTURES = Path(__file__).parent / "fixtures" / "claude"
SIMPLE_TEXT = FIXTURES / "simple_text.jsonl"
INIT_LINE = SIMPLE_TEXT.read_text(encoding="utf-8").splitlines()[0]
RESULT_LINE = SIMPLE_TEXT.read_text(encoding="utf-8").splitlines()[-1]


@dataclass
class Env:
    store: EventStore
    bus: EventBus
    settings: Settings
    workdir: Path


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    async with await EventStore.open(tmp_path / "k.db") as store:
        settings = Settings(data_dir=tmp_path, turn_timeout_s=20, kill_grace_s=1)
        yield Env(store, EventBus(), settings, tmp_path)


def fake_agent(**options: Any) -> AgentSpec:
    return AgentSpec(
        name="echo",
        description="d",
        provider="fake",
        role_prompt="unused",
        source_path=Path("agents/echo.md"),
        provider_options={"fixture": str(SIMPLE_TEXT), "delay_ms": 0, **options},
    )


def fixture_file(tmp_path: Path, *lines: str) -> str:
    path = tmp_path / f"fixture-{len(list(tmp_path.glob('fixture-*')))}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


async def new_session(env: Env, agent: AgentSpec) -> Session:
    return await env.store.create_session(agent.name, agent.provider, env.workdir)


async def run(
    env: Env,
    agent: AgentSpec,
    *,
    prompt: str = "hi",
    settings: Settings | None = None,
    session: Session | None = None,
    cancel: asyncio.Event | None = None,
) -> tuple[TurnOutcome, list[AgentEvent]]:
    session = session or await new_session(env, agent)
    outcome = await run_turn(
        session,
        agent,
        prompt,
        adapter=get_adapter(agent.provider),
        store=env.store,
        bus=env.bus,
        settings=settings or env.settings,
        cancel=cancel,
    )
    return outcome, [e async for e in env.store.iter_events(session_id=session.id)]


def of_type(events: list[AgentEvent], kind: EventType) -> list[AgentEvent]:
    return [e for e in events if e.type is kind]


def states(events: list[AgentEvent]) -> list[str]:
    return [e.data["state"] for e in of_type(events, EventType.STATUS)]


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


def spawned_pids(events: list[AgentEvent]) -> tuple[int, int]:
    [line] = [e.raw for e in of_type(events, EventType.RAW) if e.raw and "fake_child" in e.raw]
    info = json.loads(line)
    return info["parent_pid"], info["pid"]


async def wait_for_state(bus_events: Any, state: str) -> None:
    async for event in bus_events:
        if isinstance(event, AgentEvent) and event.data.get("state") == state:
            return


async def test_successful_turn(env: Env) -> None:
    outcome, events = await run(env, fake_agent())

    assert outcome.status is State.DONE
    assert outcome.is_error is False
    assert outcome.provider_session_id == "8cf3e9e9-b897-49ad-b998-cf945d86b5ea"
    assert outcome.result_text and outcome.result_text.startswith("SQLite to lekka")
    assert states(events) == ["starting", "running", "done"]
    kinds = [e.type for e in events]
    assert kinds[:3] == [EventType.STATUS, EventType.STATUS, EventType.SESSION_STARTED]
    assert kinds[-3:] == [EventType.TURN_COMPLETED, EventType.PROCESS_EXITED, EventType.STATUS]
    assert of_type(events, EventType.PROCESS_EXITED)[0].data["exit_code"] == 0
    assert not of_type(events, EventType.ERROR)
    assert not of_type(events, EventType.STDERR)


async def test_events_are_journaled_in_order_with_identity(env: Env) -> None:
    outcome, events = await run(env, fake_agent())

    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    assert {(e.turn_id, e.agent, e.provider) for e in events} == {(outcome.turn_id, "echo", "fake")}


async def test_session_and_turn_are_updated(env: Env) -> None:
    agent = fake_agent()
    session = await new_session(env, agent)

    outcome, _ = await run(env, agent, session=session, prompt="first prompt")

    stored = await env.store.get_session(session.id)
    [turn] = await env.store.list_turns(session.id)
    assert stored is not None
    assert stored.status is State.IDLE
    assert stored.provider_session_id == outcome.provider_session_id
    assert (turn.id, turn.status, turn.prompt, turn.is_error) == (
        outcome.turn_id,
        State.DONE,
        "first prompt",
        False,
    )
    assert turn.usage and turn.usage["output_tokens"] == 454


async def test_resume_passes_provider_session(env: Env) -> None:
    agent = fake_agent(fixture_resume=str(FIXTURES / "resume_turn2.jsonl"))
    session = await new_session(env, agent)
    session = await env.store.update_session(session.id, provider_session_id="8cf3e9e9")

    outcome, _ = await run(env, agent, session=session)

    assert outcome.result_text == "42"


async def test_exit_code_and_stderr_without_result(env: Env) -> None:
    fixture = fixture_file(env.workdir, INIT_LINE)
    agent = fake_agent(fixture=fixture, exit_code=3, stderr="fatal: boom")

    outcome, events = await run(env, agent)

    assert outcome.status is State.FAILED
    assert [e.data["line"] for e in of_type(events, EventType.STDERR)] == ["fatal: boom"]
    [error] = of_type(events, EventType.ERROR)
    assert error.data["fatal"] is True
    assert "code 3" in error.data["message"] and "fatal: boom" in error.data["message"]
    assert of_type(events, EventType.PROCESS_EXITED)[0].data["exit_code"] == 3
    assert states(events)[-1] == "failed"
    assert (await env.store.list_sessions())[0].status is State.FAILED


async def test_recorded_api_error(env: Env) -> None:
    meta = json.loads((FIXTURES / "error.meta.json").read_text(encoding="utf-8"))
    agent = fake_agent(
        fixture=str(FIXTURES / "error.jsonl"), exit_code=1, stderr=meta["stderr"].strip()
    )

    outcome, events = await run(env, agent)

    assert outcome.status is State.FAILED
    assert outcome.is_error is True
    [error] = of_type(events, EventType.ERROR)
    assert error.data == {
        "message": outcome.result_text,
        "fatal": False,
        "code": "model_not_found",
    }


async def test_timeout_kills_the_whole_tree(env: Env) -> None:
    settings = env.settings.model_copy(update={"turn_timeout_s": 1.0, "kill_grace_s": 1.0})
    agent = fake_agent(hang=True, spawn_child=True)

    started = time.monotonic()
    outcome, events = await run(env, agent, settings=settings)

    assert time.monotonic() - started < 10
    assert outcome.status is State.FAILED
    assert of_type(events, EventType.STATUS)[-1].data == {
        "state": "failed",
        "reason": "timeout after 1s",
    }
    assert of_type(events, EventType.PROCESS_EXITED)[0].data["signal"] == "SIGTERM"
    parent, child = spawned_pids(events)
    assert await wait_dead(parent)
    assert await wait_dead(child)


async def test_sigterm_ignored_escalates_to_sigkill(env: Env) -> None:
    settings = env.settings.model_copy(update={"turn_timeout_s": 0.5, "kill_grace_s": 0.3})
    agent = fake_agent(hang=True, ignore_sigterm=True, spawn_child=True)

    outcome, events = await run(env, agent, settings=settings)

    assert outcome.status is State.FAILED
    assert of_type(events, EventType.PROCESS_EXITED)[0].data["signal"] == "SIGKILL"
    parent, child = spawned_pids(events)
    assert await wait_dead(parent)
    assert await wait_dead(child)


async def test_cancel_event_stops_the_turn(env: Env) -> None:
    agent = fake_agent(hang=True, spawn_child=True)
    session = await new_session(env, agent)
    cancel = asyncio.Event()

    with env.bus.subscribe(session.id) as live:
        task = asyncio.create_task(run(env, agent, session=session, cancel=cancel))
        await asyncio.wait_for(wait_for_state(live, "running"), timeout=10)
        cancel.set()
        outcome, events = await asyncio.wait_for(task, timeout=10)

    assert outcome.status is State.CANCELLED
    assert states(events) == ["starting", "running", "cancelled"]
    assert not of_type(events, EventType.ERROR)
    assert (await env.store.get_session(session.id)).status is State.IDLE  # type: ignore[union-attr]
    parent, child = spawned_pids(events)
    assert await wait_dead(parent)
    assert await wait_dead(child)


async def test_task_cancellation_cleans_up_and_propagates(env: Env) -> None:
    agent = fake_agent(hang=True, spawn_child=True)
    session = await new_session(env, agent)

    with env.bus.subscribe(session.id) as live:
        task = asyncio.create_task(run(env, agent, session=session))
        await asyncio.wait_for(wait_for_state(live, "running"), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    events = [e async for e in env.store.iter_events(session_id=session.id)]
    [turn] = await env.store.list_turns(session.id)
    assert turn.status is State.CANCELLED
    assert states(events)[-1] == "cancelled"
    assert len(of_type(events, EventType.PROCESS_EXITED)) == 1
    parent, child = spawned_pids(events)
    assert await wait_dead(parent)
    assert await wait_dead(child)


async def test_line_longer_than_64_kib(env: Env) -> None:
    huge_result = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 200_000}],
            },
        }
    )
    agent = fake_agent(fixture=fixture_file(env.workdir, INIT_LINE, huge_result, RESULT_LINE))

    outcome, events = await run(env, agent)

    assert outcome.status is State.DONE
    [result] = of_type(events, EventType.TOOL_RESULT)
    assert len(result.data["output_summary"]) == 2000
    assert result.raw is not None and len(result.raw) > 200_000


async def test_line_over_the_limit_is_dropped_and_reading_continues(env: Env) -> None:
    settings = env.settings.model_copy(update={"stdout_line_limit": 64 * 1024})
    agent = fake_agent(fixture=fixture_file(env.workdir, INIT_LINE, "y" * 200_000, RESULT_LINE))

    outcome, events = await run(env, agent, settings=settings)

    assert outcome.status is State.DONE
    [error] = of_type(events, EventType.ERROR)
    assert error.data["fatal"] is False
    assert "longer than 65536 bytes" in error.data["message"]
    assert not of_type(events, EventType.RAW)  # the tail of the long line is not a new line


async def test_unrecognized_output_does_not_break_the_turn(env: Env) -> None:
    agent = fake_agent(fixture=fixture_file(env.workdir, INIT_LINE, "hello, not json", RESULT_LINE))

    outcome, events = await run(env, agent)

    assert outcome.status is State.DONE
    [raw] = of_type(events, EventType.RAW)
    assert raw.raw == "hello, not json"


async def test_strip_env_is_applied(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("KONKLAWE_TEST_KEEP", "1")
    agent = fake_agent(report_env=["ANTHROPIC_API_KEY", "CLAUDECODE", "KONKLAWE_TEST_KEEP"])

    _, events = await run(env, agent)

    [report] = [json.loads(e.raw) for e in of_type(events, EventType.RAW) if e.raw]
    assert report["set"] == {
        "ANTHROPIC_API_KEY": False,
        "CLAUDECODE": False,
        "KONKLAWE_TEST_KEEP": True,
    }


async def test_large_prompt_goes_through_stdin(env: Env) -> None:
    outcome, _ = await run(env, fake_agent(), prompt="x" * 2_000_000)

    assert outcome.status is State.DONE


async def test_missing_executable(env: Env) -> None:
    agent = AgentSpec(
        name="ghost",
        description="d",
        provider="claude",
        role_prompt="r",
        source_path=Path("agents/ghost.md"),
        provider_options={"executable": str(env.workdir / "no-such-claude")},
    )

    outcome, events = await run(env, agent)

    assert outcome.status is State.FAILED
    [error] = of_type(events, EventType.ERROR)
    assert error.data["fatal"] is True
    assert error.data["message"].startswith("cannot start")
    assert not of_type(events, EventType.PROCESS_EXITED)
    assert states(events) == ["starting", "failed"]
