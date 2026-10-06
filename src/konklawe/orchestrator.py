"""Sessions, turn scheduling and clean shutdown."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from konklawe.adapters import adapters as registered_adapters
from konklawe.adapters.base import ProviderAdapter
from konklawe.agents import AgentSpec
from konklawe.bus import EventBus
from konklawe.config import Settings
from konklawe.events import State
from konklawe.runner import TurnOutcome, run_turn
from konklawe.store import EventStore, Session


class OrchestratorError(Exception):
    pass


@dataclass
class _ActiveTurn:
    task: asyncio.Task[TurnOutcome]
    cancel: asyncio.Event


class Orchestrator:
    """Runs turns: one at a time per session, at most `max_parallel_turns` overall."""

    def __init__(
        self,
        settings: Settings,
        store: EventStore,
        bus: EventBus,
        agents: Mapping[str, AgentSpec],
        adapters: Mapping[str, ProviderAdapter] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.bus = bus
        self.agents = dict(agents)
        self._adapters = dict(adapters) if adapters is not None else registered_adapters()
        self._semaphore = asyncio.Semaphore(settings.max_parallel_turns)
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._active: dict[str, _ActiveTurn] = {}
        self._closed = False

    async def new_session(self, agent_name: str, workdir: Path) -> Session:
        agent = self._agent(agent_name)
        path = await asyncio.to_thread(_existing_dir, workdir)
        # The session records the provider as its current executor (switchable in stage 5).
        return await self.store.create_session(agent.name, agent.provider, path, State.IDLE)

    async def get_session(self, session_id: str) -> Session:
        session = await self.store.get_session(session_id)
        if session is None:
            raise OrchestratorError(f"unknown session '{session_id}'")
        return session

    async def send(self, session_id: str, prompt: str) -> TurnOutcome:
        """Run one turn in the session, resuming the provider session of the previous turn.

        Waits while the session is busy or `max_parallel_turns` turns are running.
        Cancelling the caller cancels the turn (and kills its processes).
        """
        await self.get_session(session_id)
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock, self._semaphore:
            # Re-read after waiting: the previous turn may have set provider_session_id.
            session = await self.get_session(session_id)
            agent = self._agent(session.agent)
            adapter = self._adapter_for(session, agent)
            if self._closed:
                raise OrchestratorError("orchestrator is shut down")
            cancel = asyncio.Event()
            task = asyncio.create_task(
                run_turn(
                    session,
                    agent,
                    prompt,
                    adapter=adapter,
                    store=self.store,
                    bus=self.bus,
                    settings=self.settings,
                    cancel=cancel,
                ),
                name=f"turn:{session_id}",
            )
            self._active[session_id] = _ActiveTurn(task, cancel)
            try:
                return await task
            finally:
                self._active.pop(session_id, None)

    def cancel(self, session_id: str) -> bool:
        """Stop the session's running turn. Returns False when nothing was running."""
        active = self._active.get(session_id)
        if active is None:
            return False
        active.cancel.set()
        return True

    @property
    def active_sessions(self) -> list[str]:
        return list(self._active)

    async def shutdown(self) -> None:
        """Refuse new turns, cancel running ones and wait until their processes are gone."""
        self._closed = True
        active = list(self._active.values())
        for turn in active:
            turn.cancel.set()
        await asyncio.gather(*(turn.task for turn in active), return_exceptions=True)

    def _agent(self, name: str) -> AgentSpec:
        try:
            return self.agents[name]
        except KeyError:
            known = ", ".join(sorted(self.agents)) or "none"
            raise OrchestratorError(f"unknown agent '{name}' (known: {known})") from None

    def _adapter_for(self, session: Session, agent: AgentSpec) -> ProviderAdapter:
        if agent.provider != session.provider:
            raise OrchestratorError(
                f"agent '{agent.name}' now uses provider '{agent.provider}', but session "
                f"{session.id} runs on '{session.provider}'; start a new session"
            )
        try:
            return self._adapters[session.provider]
        except KeyError:
            raise OrchestratorError(f"unknown provider '{session.provider}'") from None


def _existing_dir(workdir: Path) -> Path:
    path = workdir.expanduser().resolve()
    if not path.is_dir():
        raise OrchestratorError(f"working directory does not exist: {path}")
    return path
