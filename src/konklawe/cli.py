"""Command-line interface (Typer)."""

import asyncio
import json
import os
import shlex
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from konklawe import __version__
from konklawe.adapters import get_adapter
from konklawe.adapters.base import TurnRequest
from konklawe.agents import AgentSpec, scan_agents
from konklawe.bus import EventBus, EventsDropped, Subscription
from konklawe.config import ConfigError, Settings, load_settings
from konklawe.events import AgentEvent, State
from konklawe.orchestrator import Orchestrator, OrchestratorError
from konklawe.render import Renderer
from konklawe.runner import (
    TurnOutcome,
    child_env,
    process_group_kwargs,
    terminate_tree,
)
from konklawe.store import EventStore, Session

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130
FOLLOW_POLL_S = 0.3

app = typer.Typer(
    help="Local multi-agent environment: AI coding CLIs working together, observed live.",
    no_args_is_help=True,
)
agents_app = typer.Typer(help="Inspect agent definitions.", no_args_is_help=True)
sessions_app = typer.Typer(help="Inspect sessions.", no_args_is_help=True)
app.add_typer(agents_app, name="agents")
app.add_typer(sessions_app, name="sessions")

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

WorkdirOption = Annotated[
    Path | None,
    typer.Option("--workdir", "-w", help="Working directory (default: current directory)."),
]
VerboseOption = Annotated[
    bool, typer.Option("--verbose", "-v", help="Also show tool output, stderr and statuses.")
]
SessionOption = Annotated[
    str | None, typer.Option("--session", "-s", help="Continue this session instead of a new one.")
]


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"konklawe {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_print_version, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Local multi-agent environment: AI coding CLIs working together, observed live."""


# Agents


@agents_app.command("list")
def agents_list() -> None:
    """List agents; invalid agent files are reported with their path."""
    settings = _settings()
    scan = scan_agents(settings.agents_dir)
    table = Table("Name", "Provider", "Model", "Description")
    for spec in scan.agents.values():
        table.add_row(spec.name, spec.provider, spec.model or "-", spec.description)
    console.print(table)
    _print_agent_errors(scan.errors)
    if scan.errors:
        raise typer.Exit(EXIT_FAILED)


@agents_app.command("show")
def agents_show(name: str, workdir: WorkdirOption = None, prompt: str = "<prompt>") -> None:
    """Show an agent definition and the command a turn would run (dry run)."""
    settings = _settings()
    spec = _load_agent(settings, name)
    table = Table(show_header=False, box=None)
    for field in ("name", "description", "provider", "model", "permission_mode", "source_path"):
        table.add_row(Text(field, style="bold"), Text(str(getattr(spec, field))))
    table.add_row(Text("tools", style="bold"), Text(", ".join(spec.tools) or "-"))
    table.add_row(
        Text("disallowed_tools", style="bold"), Text(", ".join(spec.disallowed_tools) or "-")
    )
    table.add_row(Text("provider_options", style="bold"), Text(json.dumps(spec.provider_options)))
    console.print(table)
    console.print(Text("\nRole prompt:", style="bold"))
    console.print(Text(spec.role_prompt))

    request = TurnRequest(spec, prompt, workdir or Path.cwd(), resume_id=None)
    command = get_adapter(spec.provider).build_command(request)
    console.print(Text("\nCommand (dry run):", style="bold"))
    console.print(Text(shlex.join(command.argv)), soft_wrap=True)
    if command.stdin_text is not None:
        console.print(Text(f"stdin: {command.stdin_text}"))
    for key, value in command.env_overrides.items():
        console.print(Text(f"env: {key}={value}"))
    removed = ", ".join(settings.strip_env)
    console.print(Text(f"env removed if set: {removed}", style="dim"), soft_wrap=True)


# Turns


@app.command()
def run(
    agent: str,
    prompt: str,
    workdir: WorkdirOption = None,
    session: SessionOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Run one turn of AGENT. Without --session a new session is created."""
    settings = _settings()
    spec = _load_agent(settings, agent)
    target = (workdir or Path.cwd()).resolve()

    async def main() -> TurnOutcome:
        async with await EventStore.open(
            settings.db_path, store_raw=settings.store_raw_lines
        ) as store:
            bus = EventBus()
            orch = Orchestrator(settings, store, bus, {spec.name: spec})
            current = await _open_session(orch, spec, session, target, workdir is not None)
            console.print(Text(f"workdir: {current.workdir}", style="dim"))
            renderer = Renderer(console, verbose=verbose)
            try:
                return await _live(orch, bus, renderer, orch.send(current.id, prompt))
            finally:
                await orch.shutdown()
                console.print(f"session: {current.id}")

    outcome = _run_async(main)
    if outcome.status is not State.DONE:
        raise typer.Exit(EXIT_FAILED)


@app.command()
def chat(
    agent: str,
    workdir: WorkdirOption = None,
    session: SessionOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Talk to AGENT: every line is a new turn in the same session (/exit or Ctrl+D ends)."""
    settings = _settings()
    spec = _load_agent(settings, agent)
    target = (workdir or Path.cwd()).resolve()
    renderer = Renderer(console, verbose=verbose)
    with asyncio.Runner() as runner:
        store = runner.run(EventStore.open(settings.db_path, store_raw=settings.store_raw_lines))
        bus = EventBus()
        orch = Orchestrator(settings, store, bus, {spec.name: spec})
        try:
            current = runner.run(_open_session(orch, spec, session, target, workdir is not None))
            console.print(Text(f"workdir: {current.workdir}", style="dim"))
            console.print(Text(f"session: {current.id} · /exit or Ctrl+D to leave", style="dim"))
            while True:
                try:
                    line = console.input("> ").strip()
                except (EOFError, KeyboardInterrupt):
                    console.print()
                    break
                if not line:
                    continue
                if line in {"/exit", "/quit"}:
                    break
                try:
                    runner.run(_live(orch, bus, renderer, orch.send(current.id, line)))
                except KeyboardInterrupt:
                    console.print(Text("turn cancelled", style="yellow"))
            console.print(f"session: {current.id}")
        finally:
            runner.run(orch.shutdown())
            runner.run(store.close())


@app.command("run-many")
def run_many(
    task: Annotated[
        list[str],
        typer.Option("--task", "-t", help="AGENT=PROMPT; repeat for each agent to run."),
    ],
    workdir: WorkdirOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Run several agents in parallel, each in its own session."""
    settings = _settings()
    tasks = [_parse_task(item) for item in task]
    specs = {name: _load_agent(settings, name) for name in dict.fromkeys(n for n, _ in tasks)}
    target = (workdir or Path.cwd()).resolve()

    async def main() -> list[tuple[Session, TurnOutcome]]:
        async with await EventStore.open(
            settings.db_path, store_raw=settings.store_raw_lines
        ) as store:
            bus = EventBus()
            orch = Orchestrator(settings, store, bus, specs)
            sessions = [await _open_session(orch, specs[n], None, target, False) for n, _ in tasks]
            console.print(Text(f"workdir: {sessions[0].workdir}", style="dim"))
            repeated = {n for n, _ in tasks if sum(1 for m, _ in tasks if m == n) > 1}
            labels = {s.id: f"{s.agent}:{s.id[:4]}" for s in sessions if s.agent in repeated}
            renderer = Renderer(console, verbose=verbose, stream=False, labels=labels)
            sends = [
                orch.send(s.id, prompt) for s, (_, prompt) in zip(sessions, tasks, strict=True)
            ]
            try:
                outcomes = await _live(orch, bus, renderer, asyncio.gather(*sends))
            finally:
                await orch.shutdown()
                for current in sessions:
                    console.print(f"session: {current.id} ({current.agent})")
            return list(zip(sessions, outcomes, strict=True))

    results = _run_async(main)
    table = Table("Agent", "Session", "Status", "Time")
    for current, outcome in results:
        style = "green" if outcome.status is State.DONE else "red"
        table.add_row(
            current.agent,
            current.id,
            Text(outcome.status.value, style=style),
            f"{outcome.duration_ms / 1000:.1f}s",
        )
    console.print(table)
    if any(outcome.status is not State.DONE for _, outcome in results):
        raise typer.Exit(EXIT_FAILED)


# Sessions and replay


@sessions_app.command("list")
def sessions_list() -> None:
    """List sessions, most recently active first."""
    settings = _settings()

    async def main() -> list[Session]:
        async with await EventStore.open(settings.db_path) as store:
            return await store.list_sessions()

    table = Table("ID", "Agent", "Provider", "Status", "Turns", "Last activity (UTC)")
    for current in _run_async(main):
        table.add_row(
            current.id,
            current.agent,
            current.provider,
            current.status.value,
            str(current.turn_count),
            _utc(current.updated_at),
        )
    console.print(table)


@app.command()
def log(
    session_id: str,
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Keep printing new events (Ctrl+C stops).")
    ] = False,
    verbose: VerboseOption = False,
) -> None:
    """Replay a session from the journal with the same renderer as the live view."""
    settings = _settings()
    renderer = Renderer(console, verbose=verbose)

    async def main() -> None:
        async with await EventStore.open(settings.db_path) as store:
            current = await store.get_session(session_id)
            if current is None:
                _fail(f"unknown session '{session_id}'")
            console.print(
                Text(f"session: {current.id} · {current.agent} · {current.provider}", style="dim")
            )
            console.print(Text(f"workdir: {current.workdir}", style="dim"))
            after = 0
            try:
                while True:
                    async for event in store.iter_events(session_id=session_id, after_seq=after):
                        renderer.render(event)
                        after = event.seq or after
                    if not follow:
                        break
                    await asyncio.sleep(FOLLOW_POLL_S)
            finally:
                renderer.close()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if not follow:
            raise typer.Exit(EXIT_INTERRUPTED) from None


@app.command()
def record(
    agent: str,
    prompt: str,
    out: Annotated[Path, typer.Option("--out", "-o", help="File for the raw stdout stream.")],
    workdir: WorkdirOption = None,
    resume: Annotated[
        str | None, typer.Option("--resume", help="Provider session id to resume.")
    ] = None,
) -> None:
    """Record the raw stdout of one turn as a fixture (runs the real CLI of the agent).

    stderr, exit code and the exact command go to OUT.meta.json.
    """
    settings = _settings()
    spec = _load_agent(settings, agent)
    cwd = (workdir or Path.cwd()).resolve()
    command = get_adapter(spec.provider).build_command(TurnRequest(spec, prompt, cwd, resume))
    removed = sorted(name for name in settings.strip_env if name in os.environ)
    console.print(Text(f"workdir: {cwd}", style="dim"))

    async def main() -> tuple[bytes, bytes, int | None, int]:
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=cwd,
            env=child_env(settings.strip_env, command.env_overrides),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_group_kwargs(),
        )
        stdin = (command.stdin_text or "").encode("utf-8")
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin), settings.turn_timeout_s
            )
        except TimeoutError:
            await terminate_tree(proc, settings.kill_grace_s)
            _fail(f"no exit after {settings.turn_timeout_s:g}s; nothing was written")
        except BaseException:
            await asyncio.shield(terminate_tree(proc, settings.kill_grace_s))
            raise
        return stdout, stderr, proc.returncode, int((time.monotonic() - started) * 1000)

    recorded_at = datetime.now(UTC).isoformat()
    stdout, stderr, exit_code, duration_ms = _run_async(main)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(stdout)
    meta = {
        "command": command.argv,
        "stdin": command.stdin_text,
        "cwd": str(cwd),
        "exit_code": exit_code,
        "stderr": stderr.decode("utf-8", errors="replace"),
        "duration_ms": duration_ms,
        "recorded_at": recorded_at,
        "env_removed": removed,
    }
    meta_path = out.with_name(out.name + ".meta.json")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = len(stdout.splitlines())
    console.print(f"recorded {lines} lines to {out} (exit code {exit_code}); metadata: {meta_path}")


# Helpers


def _settings() -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        _fail(str(exc))


def _load_agent(settings: Settings, name: str) -> AgentSpec:
    scan = scan_agents(settings.agents_dir)
    if name in scan.agents:
        return scan.agents[name]
    own_errors = [(path, msg) for path, msg in scan.errors if path.stem == name]
    if own_errors:
        _print_agent_errors(own_errors)
        raise typer.Exit(EXIT_USAGE)
    known = ", ".join(sorted(scan.agents)) or "none"
    _fail(f"unknown agent '{name}' (known: {known})")


def _print_agent_errors(errors: list[tuple[Path, str]]) -> None:
    for path, message in errors:
        err_console.print(Text(f"✗ {_display_path(path)}: {message}", style="red"))


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _parse_task(item: str) -> tuple[str, str]:
    name, sep, prompt = item.partition("=")
    if not sep or not name.strip() or not prompt.strip():
        _fail(f"--task expects AGENT=PROMPT, got '{item}'")
    return name.strip(), prompt


async def _open_session(
    orch: Orchestrator,
    spec: AgentSpec,
    session_id: str | None,
    workdir: Path,
    workdir_given: bool,
) -> Session:
    """A new session in `workdir`, or the existing `session_id` (which must match)."""
    try:
        if session_id is None:
            return await orch.new_session(spec.name, workdir)
        current = await orch.get_session(session_id)
        if current.agent != spec.name:
            raise OrchestratorError(f"session {session_id} belongs to agent '{current.agent}'")
        if workdir_given and workdir != current.workdir:
            raise OrchestratorError(f"session {session_id} works in {current.workdir}")
        return current
    except OrchestratorError as exc:
        _fail(str(exc))


async def _live[T](orch: Orchestrator, bus: EventBus, renderer: Renderer, work: Awaitable[T]) -> T:
    """Await `work` while rendering every bus event; always leaves no turn running."""
    with bus.subscribe() as subscription:
        consumer = asyncio.create_task(_consume(subscription, renderer))
        try:
            return await work
        finally:
            await orch.cancel_all()
            subscription.close()
            await consumer
            renderer.close()


async def _consume(subscription: Subscription, renderer: Renderer) -> None:
    item: AgentEvent | EventsDropped
    async for item in subscription:
        renderer.render(item)


def _run_async[T](main: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run `main`; Ctrl+C cancels it (turns clean up their processes) and exits with 130."""
    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        err_console.print(Text("interrupted", style="yellow"))
        raise typer.Exit(EXIT_INTERRUPTED) from None


def _fail(message: str) -> NoReturn:
    err_console.print(Text(f"✗ {message}", style="red"))
    raise typer.Exit(EXIT_USAGE)


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
