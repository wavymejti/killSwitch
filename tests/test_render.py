import io
import re
from pathlib import Path

from rich.console import Console

from konklawe.adapters import get_adapter
from konklawe.adapters.claude import ClaudeTurnParser
from konklawe.agents import AgentSpec
from konklawe.bus import EventBus, EventsDropped
from konklawe.config import Settings
from konklawe.events import AgentEvent, ParsedEvent, State
from konklawe.render import Renderer, agent_color, footer
from konklawe.runner import run_turn
from konklawe.store import EventStore

FIXTURES = Path(__file__).parent / "fixtures" / "claude"


def make_console() -> Console:
    return Console(file=io.StringIO(), width=300, color_system=None, highlight=False)


def output(console: Console) -> str:
    assert isinstance(console.file, io.StringIO)
    return console.file.getvalue()


def as_event(parsed: ParsedEvent, session: str = "s1", agent: str = "echo") -> AgentEvent:
    return AgentEvent.from_parsed(
        parsed, session_id=session, turn_id="t1", agent=agent, provider="fake"
    )


def fixture_events(name: str) -> list[AgentEvent]:
    parser = ClaudeTurnParser()
    lines = (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    return [as_event(p) for line in lines for p in parser.feed(line)]


def render(*events: AgentEvent | EventsDropped, verbose: bool = False, stream: bool = True) -> str:
    console = make_console()
    renderer = Renderer(console, verbose=verbose, stream=stream)
    for event in events:
        renderer.render(event)
    renderer.close()
    return output(console)


def test_streamed_text_is_printed_once() -> None:
    text = render(*fixture_events("simple_text"))

    assert text.count("SQLite to lekka") == 1
    assert "[echo] Zapamiętuję liczbę 42.\n" in text
    assert "[echo] SQLite to lekka, osadzana baza danych SQL" in text


def test_tool_call_and_footer() -> None:
    lines = render(*fixture_events("tool_use")).splitlines()

    assert lines[0].startswith("[echo] ▸ Read /") and lines[0].endswith("README.md")
    assert lines[-1].startswith("[echo] ── done · 7.5s · 2 steps · ")
    assert lines[-1].endswith("≈ $0.0237 (API estimate, not a charge)")


def test_successful_tool_results_only_when_verbose() -> None:
    events = fixture_events("tool_use")

    assert "✓" not in render(*events)
    verbose = render(*events, verbose=True)
    assert re.search(r"\[echo\] ✓ 1\s+# Lista zakupów", verbose)


def test_failed_tool_result_is_always_shown() -> None:
    assert "[echo] ✗ Claude requested permissions to write" in render(
        *fixture_events("tool_denied")
    )


def test_message_without_deltas_is_printed_with_prefix_per_line() -> None:
    text = render(as_event(ParsedEvent.message("first\nsecond", "m1", from_deltas=False)))

    assert text == "[echo] first\n[echo] second\n"


def test_stderr_and_process_details_only_when_verbose() -> None:
    events = [
        as_event(ParsedEvent.stderr("warning: x")),
        as_event(ParsedEvent.process_exited(0, 1500)),
        as_event(ParsedEvent.status(State.RUNNING)),
    ]

    assert render(*events) == ""
    verbose = render(*events, verbose=True)
    assert "stderr: warning: x" in verbose
    assert "process exited: code 0 after 1500 ms" in verbose
    assert "· running" in verbose


def test_problems_are_highlighted() -> None:
    text = render(
        as_event(ParsedEvent.rate_limited("limit reached")),
        as_event(ParsedEvent.error("boom", fatal=True)),
        as_event(ParsedEvent.error("minor", fatal=False)),
        as_event(ParsedEvent.status(State.FAILED, "timeout after 5s")),
        as_event(ParsedEvent.status(State.DONE)),
    )

    assert text.splitlines() == [
        "[echo] ⏸ rate limited: limit reached",
        "[echo] ✗ error: boom",
        "[echo] ! minor",
        "[echo] ■ failed: timeout after 5s",
    ]


def test_events_dropped_warning() -> None:
    assert "! 7 events skipped by the live view" in render(EventsDropped(7))


def test_buffered_mode_keeps_parallel_lines_whole() -> None:
    def delta(session: str, agent: str, text: str) -> AgentEvent:
        return as_event(ParsedEvent.text_delta(text), session=session, agent=agent)

    text = render(
        delta("s1", "architekt", "Hel"),
        delta("s2", "recenzent", "Wor"),
        delta("s1", "architekt", "lo\nsec"),
        delta("s2", "recenzent", "ld\n"),
        delta("s1", "architekt", "ond"),
        stream=False,
    )

    assert text.splitlines() == ["[architekt] Hello", "[recenzent] World", "[architekt] second"]


def test_streaming_mode_starts_a_new_line_when_the_session_changes() -> None:
    text = render(
        as_event(ParsedEvent.text_delta("abc"), session="s1", agent="a"),
        as_event(ParsedEvent.text_delta("xyz"), session="s2", agent="b"),
        as_event(ParsedEvent.text_delta("def\n\nnext"), session="s1", agent="a"),
    )

    assert text == "[a] abc\n[b] xyz\n[a] def\n\n[a] next\n"


def test_session_labels_override_agent_name() -> None:
    console = make_console()
    renderer = Renderer(console, labels={"s2": "echo:s2"})
    renderer.render(as_event(ParsedEvent.message("hi", from_deltas=False), session="s2"))

    assert output(console) == "[echo:s2] hi\n"


def test_footer_formats() -> None:
    data = {
        "is_error": False,
        "duration_ms": 6402,
        "num_turns": 1,
        "usage": {"input_tokens": 18, "cache_read_input_tokens": 32551, "output_tokens": 454},
        "cost_estimate_usd": 0.0233651,
    }

    assert footer(data) == (
        "── done · 6.4s · 1 step · 32,569 in / 454 out tokens · "
        "≈ $0.0234 (API estimate, not a charge)"
    )
    assert footer({"is_error": True}) == "── turn failed"


def test_agent_color_is_stable() -> None:
    assert agent_color("architekt") == agent_color("architekt")


async def test_live_view_and_replay_look_the_same(tmp_path: Path) -> None:
    agent = AgentSpec(
        name="echo",
        description="d",
        provider="fake",
        role_prompt="r",
        source_path=Path("agents/echo.md"),
        provider_options={"fixture": str(FIXTURES / "tool_denied.jsonl"), "delay_ms": 0},
    )
    async with await EventStore.open(tmp_path / "k.db") as store:
        bus = EventBus()
        session = await store.create_session("echo", "fake", tmp_path)
        live_console = make_console()
        live = Renderer(live_console, verbose=True)
        with bus.subscribe() as subscription:
            await run_turn(
                session,
                agent,
                "hi",
                adapter=get_adapter("fake"),
                store=store,
                bus=bus,
                settings=Settings(),
            )
            subscription.close()
            async for item in subscription:
                live.render(item)
        live.close()

        replay_console = make_console()
        replay = Renderer(replay_console, verbose=True)
        async for event in store.iter_events(session_id=session.id):
            replay.render(event)
        replay.close()

    assert output(live_console) == output(replay_console)
    assert "✗ Claude requested permissions" in output(replay_console)
