"""Real Claude Code turns through the whole pipeline.

Consumes subscription limits, so it is skipped by default; run on purpose with
`uv run pytest -m live`.
"""

import shutil
from pathlib import Path

import pytest

from konklawe.agents import AgentSpec
from konklawe.bus import EventBus
from konklawe.config import Settings
from konklawe.events import EventType, State
from konklawe.orchestrator import Orchestrator
from konklawe.store import EventStore

pytestmark = pytest.mark.live


async def test_real_turn_and_resume(tmp_path: Path) -> None:
    if shutil.which("claude") is None:
        pytest.skip("claude CLI is not installed")
    agent = AgentSpec(
        name="live",
        description="Live test agent",
        provider="claude",
        model="haiku",
        disallowed_tools=["Edit", "Write", "Bash", "NotebookEdit"],
        role_prompt="Odpowiadasz bardzo zwięźle.",
        source_path=Path("agents/live.md"),
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    settings = Settings(data_dir=tmp_path / "data", turn_timeout_s=180)

    async with await EventStore.open(settings.db_path) as store:
        orch = Orchestrator(settings, store, EventBus(), {agent.name: agent})
        session = await orch.new_session(agent.name, workdir)
        try:
            # Arithmetic rather than "remember X": auto-memory must not be able to answer turn 2.
            first = await orch.send(session.id, "Ile to 17 + 25? Odpowiedz samą liczbą.")
            second = await orch.send(
                session.id, "Pomnóż swoją poprzednią odpowiedź przez 2. Odpowiedz samą liczbą."
            )
        finally:
            await orch.shutdown()
        events = [e async for e in store.iter_events(session_id=session.id)]

    assert first.status is State.DONE, first
    assert "42" in (first.result_text or "")
    assert second.status is State.DONE, second
    assert "84" in (second.result_text or "")
    assert first.provider_session_id and first.provider_session_id == second.provider_session_id

    kinds = {e.type for e in events}
    assert {EventType.SESSION_STARTED, EventType.TEXT_DELTA, EventType.MESSAGE} <= kinds
    assert EventType.TURN_COMPLETED in kinds
    assert EventType.RATE_LIMITED not in kinds
    assert not [e for e in events if e.type is EventType.ERROR and e.data.get("fatal")]
    # Unknown stream lines are allowed (formats change), but worth seeing when they appear.
    for event in events:
        if event.type is EventType.RAW:
            print("raw line:", (event.raw or "")[:200])
