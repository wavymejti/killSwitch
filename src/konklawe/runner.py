"""Runs one turn: a provider CLI process whose stream becomes journaled events."""

import asyncio
import os
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from konklawe.adapters.base import ProviderAdapter, TurnParser, TurnRequest
from konklawe.agents import AgentSpec
from konklawe.bus import EventBus, emit
from konklawe.config import Settings
from konklawe.events import AgentEvent, EventType, ParsedEvent, State
from konklawe.store import EventStore, Session, Turn

STDERR_TAIL_LINES = 50
# After the process exits, how long its pipes may stay open (held by stray descendants).
PIPE_DRAIN_TIMEOUT_S = 5.0
IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class TurnOutcome:
    turn_id: str
    status: State
    result_text: str | None
    is_error: bool
    provider_session_id: str | None
    duration_ms: int


async def run_turn(
    session: Session,
    agent: AgentSpec,
    prompt: str,
    *,
    adapter: ProviderAdapter,
    store: EventStore,
    bus: EventBus,
    settings: Settings,
    cancel: asyncio.Event | None = None,
) -> TurnOutcome:
    """Run one turn of `agent` in `session` and journal everything it produces.

    Setting `cancel` stops the turn and returns a `cancelled` outcome. Cancelling the
    task running this coroutine cleans up the same way and then re-raises.
    """
    turn = await store.create_turn(session.id, adapter.name, prompt)
    run = _TurnRun(session, agent, turn, adapter, store, bus, settings)
    return await run.execute(prompt, cancel)


class _TurnRun:
    def __init__(
        self,
        session: Session,
        agent: AgentSpec,
        turn: Turn,
        adapter: ProviderAdapter,
        store: EventStore,
        bus: EventBus,
        settings: Settings,
    ) -> None:
        self.session = session
        self.agent = agent
        self.turn = turn
        self.adapter = adapter
        self.store = store
        self.bus = bus
        self.settings = settings
        self.started = time.monotonic()
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self.provider_session_id = session.provider_session_id
        self.completed = False
        self.result_text: str | None = None
        self.result_is_error = False
        self.usage: dict[str, Any] | None = None
        self.rate_limited = False
        self.running = False
        self.exit_reported = False

    async def execute(self, prompt: str, cancel: asyncio.Event | None) -> TurnOutcome:
        await self.store.update_session(self.session.id, status=State.RUNNING)
        await self.emit(ParsedEvent.status(State.STARTING))
        status = State.FAILED
        reason: str | None = None
        try:
            status, reason = await self._run_process(prompt, cancel)
        except asyncio.CancelledError:
            await self._cleanup_after_cancel()
            raise
        except Exception as exc:
            await self.emit(ParsedEvent.error(f"turn failed: {exc!r}", fatal=True))
            status, reason = State.FAILED, "internal error"
        finally:
            if self.proc is not None and self.proc.returncode is None:
                # Only reachable on an unexpected path; never leave the tree behind.
                await asyncio.shield(terminate_tree(self.proc, self.settings.kill_grace_s))
        return await self._finish(status, reason)

    async def _run_process(
        self, prompt: str, cancel: asyncio.Event | None
    ) -> tuple[State, str | None]:
        request = TurnRequest(
            agent=self.agent,
            prompt=prompt,
            workdir=self.session.workdir,
            resume_id=self.session.provider_session_id,
        )
        command = self.adapter.build_command(request)
        parser = self.adapter.new_parser(self.agent)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *command.argv,
                cwd=self.session.workdir,
                env=child_env(self.settings.strip_env, command.env_overrides),
                stdin=subprocess.PIPE if command.stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                limit=self.settings.stdout_line_limit,
                **process_group_kwargs(),
            )
        except OSError as exc:
            await self.emit(
                ParsedEvent.error(f"cannot start {command.argv[0]!r}: {exc}", fatal=True)
            )
            return State.FAILED, "process did not start"

        pump = asyncio.create_task(self._pump(parser, command.stdin_text))
        exited = asyncio.create_task(self.proc.wait())
        try:
            stop_reason = await self._wait_for_end(exited, pump, cancel)
            if stop_reason is not None:
                await terminate_tree(self.proc, self.settings.kill_grace_s)
            exit_code = await exited
            # Descendants still in the group are orphans now; their open pipes would also
            # keep the readers waiting forever.
            kill_group(self.proc)
            await self._drain(pump)
        finally:
            for task in (pump, exited):
                if not task.done():
                    task.cancel()

        if stop_reason is None:
            for parsed in self._finish_parser(parser, exit_code):
                await self.handle(parsed)
        await self._report_exit()
        if stop_reason == "cancelled":
            return State.CANCELLED, "cancelled"
        if stop_reason == "timeout":
            return State.FAILED, f"timeout after {self.settings.turn_timeout_s:g}s"
        if self.rate_limited:
            return State.LIMITED, "rate limited"
        if self.completed and not self.result_is_error:
            return State.DONE, None
        return State.FAILED, None

    async def _wait_for_end(
        self, exited: asyncio.Task[int], pump: asyncio.Task[None], cancel: asyncio.Event | None
    ) -> str | None:
        """Wait until the process exits (None), or return "cancelled" / "timeout"."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.turn_timeout_s
        cancel_wait = asyncio.create_task(cancel.wait()) if cancel else None
        try:
            while True:
                watched: set[asyncio.Future[Any]] = {exited}
                if not pump.done():
                    watched.add(pump)
                if cancel_wait:
                    watched.add(cancel_wait)
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return "timeout"
                done, _ = await asyncio.wait(
                    watched, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    return "timeout"
                if exited in done:
                    return None
                if cancel_wait in done:
                    return "cancelled"
                if pump in done and pump.exception() is not None:
                    raise pump.exception()  # type: ignore[misc]
                # Output ended before the process: keep waiting for its exit.
        finally:
            if cancel_wait and not cancel_wait.done():
                cancel_wait.cancel()

    async def _drain(self, pump: asyncio.Task[None]) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(pump), PIPE_DRAIN_TIMEOUT_S)
        except TimeoutError:
            pump.cancel()
            message = "stopped reading output: pipes stayed open after the process exited"
            await self.emit(ParsedEvent.error(message, fatal=False))

    async def _pump(self, parser: TurnParser, stdin_text: str | None) -> None:
        assert self.proc is not None and self.proc.stdout and self.proc.stderr
        async with asyncio.TaskGroup() as group:
            if stdin_text is not None and self.proc.stdin is not None:
                group.create_task(write_stdin(self.proc.stdin, stdin_text))
            group.create_task(self._pump_stdout(self.proc.stdout, parser))
            group.create_task(self._pump_stderr(self.proc.stderr))

    async def _pump_stdout(self, stream: asyncio.StreamReader, parser: TurnParser) -> None:
        async for line in read_lines(stream):
            if line is None:
                limit = self.settings.stdout_line_limit
                message = f"dropped a stdout line longer than {limit} bytes"
                await self.emit(ParsedEvent.error(message, fatal=False))
                continue
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if not text.strip():
                continue
            try:
                parsed_events = parser.feed(text)
            except Exception as exc:  # the parser contract says it never raises; enforce it
                parsed_events = [ParsedEvent.unrecognized(text, note=f"parser error: {exc!r}")]
            for parsed in parsed_events:
                await self.handle(parsed)

    async def _pump_stderr(self, stream: asyncio.StreamReader) -> None:
        async for line in read_lines(stream):
            if line is None:
                continue
            text = line.decode("utf-8", errors="replace").rstrip("\r")
            if not text.strip():
                continue
            self.stderr_tail.append(text)
            await self.emit(ParsedEvent.stderr(text))

    def _finish_parser(self, parser: TurnParser, exit_code: int | None) -> list[ParsedEvent]:
        try:
            return parser.finish(exit_code, list(self.stderr_tail))
        except Exception as exc:
            return [ParsedEvent.error(f"parser failed to finish: {exc!r}", fatal=False)]

    async def handle(self, parsed: ParsedEvent) -> None:
        """Emit a parsed event and track what the turn outcome needs."""
        if not self.running:
            self.running = True
            await self.emit(ParsedEvent.status(State.RUNNING))
        await self.emit(parsed)
        data = parsed.data
        match parsed.type:
            case EventType.SESSION_STARTED:
                self.provider_session_id = data.get("provider_session_id") or (
                    self.provider_session_id
                )
            case EventType.TURN_COMPLETED:
                self.completed = True
                self.result_text = data.get("result_text")
                self.result_is_error = bool(data.get("is_error"))
                self.usage = data.get("usage")
                self.provider_session_id = data.get("provider_session_id") or (
                    self.provider_session_id
                )
            case EventType.RATE_LIMITED:
                self.rate_limited = True

    async def emit(self, parsed: ParsedEvent) -> AgentEvent:
        event = AgentEvent.from_parsed(
            parsed,
            session_id=self.session.id,
            turn_id=self.turn.id,
            agent=self.agent.name,
            provider=self.adapter.name,
        )
        return await emit(self.store, self.bus, event)

    async def _cleanup_after_cancel(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            await terminate_tree(self.proc, self.settings.kill_grace_s)
        if self.proc is not None:
            kill_group(self.proc)
            await self._report_exit()
        await self._finish(State.CANCELLED, "cancelled")

    async def _report_exit(self) -> None:
        if self.proc is None or self.exit_reported:
            return
        self.exit_reported = True
        code = self.proc.returncode
        await self.emit(ParsedEvent.process_exited(code, self.elapsed_ms(), signal_name(code)))

    async def _finish(self, status: State, reason: str | None) -> TurnOutcome:
        is_error = status is not State.DONE
        await self.store.finish_turn(
            self.turn.id,
            status=status,
            result_text=self.result_text,
            is_error=is_error,
            usage=self.usage,
        )
        session_status = {
            State.DONE: State.IDLE,
            State.CANCELLED: State.IDLE,
            State.LIMITED: State.LIMITED,
        }.get(status, State.FAILED)
        await self.store.update_session(
            self.session.id, status=session_status, provider_session_id=self.provider_session_id
        )
        await self.emit(ParsedEvent.status(status, reason))
        return TurnOutcome(
            turn_id=self.turn.id,
            status=status,
            result_text=self.result_text,
            is_error=is_error,
            provider_session_id=self.provider_session_id,
            duration_ms=self.elapsed_ms(),
        )

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


def child_env(strip: list[str], overrides: dict[str, str]) -> dict[str, str]:
    removed = set(strip)
    env = {key: value for key, value in os.environ.items() if key not in removed}
    env.update(overrides)
    return env


def process_group_kwargs() -> dict[str, Any]:
    """Start the child in its own process group so the whole tree can be killed."""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


async def write_stdin(stdin: asyncio.StreamWriter, text: str) -> None:
    try:
        stdin.write(text.encode("utf-8"))
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # the process exited without reading its input; its exit code tells the story
    finally:
        stdin.close()
        try:
            await stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def read_lines(stream: asyncio.StreamReader) -> AsyncIterator[bytes | None]:
    """Yield lines without the trailing newline; `None` stands for a line over the limit.

    The remainder of an over-long line is discarded rather than yielded as a new line.
    """
    while True:
        try:
            line = await stream.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                yield exc.partial
            return
        except asyncio.LimitOverrunError as exc:
            yield None
            if not await _discard_rest_of_line(stream, exc.consumed):
                return
            continue
        yield line[:-1]


async def _discard_rest_of_line(stream: asyncio.StreamReader, consumed: int) -> bool:
    """Skip to the next newline. Returns False when the stream ended first."""
    try:
        await stream.readexactly(consumed)
        while True:
            try:
                await stream.readuntil(b"\n")
                return True
            except asyncio.LimitOverrunError as exc:
                await stream.readexactly(exc.consumed)
    except asyncio.IncompleteReadError:
        return False


async def terminate_tree(proc: asyncio.subprocess.Process, grace_s: float) -> None:
    """Stop the process and its descendants: gently first, by force after `grace_s`."""
    if IS_WINDOWS:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/T",
            "/F",
            "/PID",
            str(proc.pid),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await killer.wait()
        await proc.wait()
        return
    _signal_group(proc, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), grace_s)
    except TimeoutError:
        _signal_group(proc, signal.SIGKILL)
        await proc.wait()
    kill_group(proc)


def kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL whatever is left in the process group (no-op on Windows and when empty)."""
    if not IS_WINDOWS:
        _signal_group(proc, signal.SIGKILL)


def _signal_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    try:
        os.killpg(proc.pid, sig)  # the child leads its own group: pgid == pid
    except (ProcessLookupError, PermissionError):
        pass


def signal_name(exit_code: int | None) -> str | None:
    if exit_code is None or exit_code >= 0:
        return None
    try:
        return signal.Signals(-exit_code).name
    except ValueError:
        return None
