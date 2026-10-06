import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from konklawe.adapters import get_adapter
from konklawe.adapters.base import TurnRequest
from konklawe.adapters.claude import (
    ClaudeAdapter,
    ClaudeTurnParser,
    detect_rate_limit,
    summarize_tool_input,
    summarize_tool_output,
)
from konklawe.agents import AgentSpec
from konklawe.events import EventType, ParsedEvent

FIXTURES = Path(__file__).parent / "fixtures" / "claude"


def spec(**fields: Any) -> AgentSpec:
    base: dict[str, Any] = {
        "name": "a",
        "description": "d",
        "provider": "claude",
        "role_prompt": "You review code.",
        "source_path": Path("agents/a.md"),
    }
    return AgentSpec(**{**base, **fields})


def replay(name: str) -> list[ParsedEvent]:
    parser = ClaudeTurnParser()
    events: list[ParsedEvent] = []
    for line in (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
        events += parser.feed(line)
    meta = json.loads((FIXTURES / f"{name}.meta.json").read_text(encoding="utf-8"))
    return events + parser.finish(meta["exit_code"], meta["stderr"].splitlines())


def summarize(events: list[ParsedEvent]) -> list[tuple[Any, ...]]:
    """Compact view: runs of text deltas joined, key fields of everything else."""
    out: list[tuple[Any, ...]] = []
    for e in events:
        d = e.data
        match e.type:
            case EventType.TEXT_DELTA if out and out[-1][0] == "text_delta":
                out[-1] = ("text_delta", out[-1][1] + d["text"], out[-1][2] + 1)
            case EventType.TEXT_DELTA:
                out.append(("text_delta", d["text"], 1))
            case EventType.SESSION_STARTED:
                out.append(("session_started", d["provider_session_id"], d["model"]))
            case EventType.MESSAGE:
                out.append(("message", d["text"], d["from_deltas"]))
            case EventType.TOOL_CALL:
                out.append(("tool_call", d["name"], Path(d["input_summary"]).name))
            case EventType.TOOL_RESULT:
                out.append(("tool_result", d["is_error"]))
            case EventType.TURN_COMPLETED:
                out.append(("turn_completed", d["is_error"], d["num_turns"], d["result_text"]))
            case EventType.ERROR:
                out.append(("error", d["fatal"], d.get("code")))
            case _:
                out.append((e.type.value,))
    return out


SQLITE = (
    "SQLite to lekka, osadzana baza danych SQL, która przechowuje dane w pojedynczym pliku bez "
    "potrzeby osobnego serwera. Jest idealna do aplikacji desktopowych, mobilnych i małych "
    "projektów ze względu na prostotę i niezawodność."
)
README_SUMMARY = (
    "Projekt to aplikacja konsolowa w Pythonie do zarządzania listą zakupów z persystencją w "
    "formacie JSON i funkcjami dodawania, usuwania oraz zaznaczania produktów."
)
DENIED_REPLY = (
    'Czekam na Twoją zgodę na zapisanie pliku. Kliknij "Allow" lub "Approve", aby pozwolić na '
    "utworzenie pliku `notatka.txt`."
)
BAD_MODEL = (
    "There's an issue with the selected model (nie-ma-takiego-modelu). It may not exist or you "
    "may not have access to it. Run --model to pick a different model."
)
HAIKU = "claude-haiku-4-5-20251001"

EXPECTED = {
    "simple_text": [
        ("session_started", "8cf3e9e9-b897-49ad-b998-cf945d86b5ea", HAIKU),
        ("text_delta", "Zapamiętuję liczbę 42.", 5),
        ("message", "Zapamiętuję liczbę 42.", True),
        ("tool_call", "Write", "number_42.md"),
        ("tool_result", False),
        ("text_delta", SQLITE, 36),
        ("message", SQLITE, True),
        ("turn_completed", False, 2, SQLITE),
    ],
    "resume_turn2": [
        ("session_started", "8cf3e9e9-b897-49ad-b998-cf945d86b5ea", HAIKU),
        ("text_delta", "42", 1),
        ("message", "42", True),
        ("turn_completed", False, 1, "42"),
    ],
    "tool_use": [
        ("session_started", "afe84b4d-bea5-43a1-82b6-a21efe110ec1", HAIKU),
        ("tool_call", "Read", "README.md"),
        ("tool_result", False),
        ("text_delta", README_SUMMARY, 23),
        ("message", README_SUMMARY, True),
        ("turn_completed", False, 2, README_SUMMARY),
    ],
    "tool_denied": [
        ("session_started", "76261741-f5c3-440e-8bf0-2bae9f7f32af", HAIKU),
        ("tool_call", "Write", "notatka.txt"),
        ("tool_result", True),
        ("text_delta", DENIED_REPLY, 22),
        ("message", DENIED_REPLY, True),
        ("turn_completed", False, 2, DENIED_REPLY),
    ],
    "error": [
        ("session_started", "07ca4d2e-96cd-4b7c-9e86-1d2aed81e3f9", "nie-ma-takiego-modelu"),
        ("error", False, "model_not_found"),
        ("turn_completed", True, 1, BAD_MODEL),
    ],
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_parser_on_recorded_fixture(name: str) -> None:
    events = replay(name)

    assert summarize(events) == EXPECTED[name]
    assert not [e for e in events if e.type is EventType.RAW]
    assert all(e.raw and json.loads(e.raw) for e in events if e.type is not EventType.ERROR)


def test_turn_completed_details() -> None:
    completed = replay("simple_text")[-1]

    assert completed.data["provider_session_id"] == "8cf3e9e9-b897-49ad-b998-cf945d86b5ea"
    assert completed.data["cost_estimate_usd"] == pytest.approx(0.0233651)
    assert completed.data["duration_ms"] == 6402
    assert completed.data["usage"]["output_tokens"] == 454


def test_tool_details() -> None:
    events = replay("tool_denied")
    call = next(e for e in events if e.type is EventType.TOOL_CALL)
    result = next(e for e in events if e.type is EventType.TOOL_RESULT)

    assert call.data["tool_id"] == result.data["tool_id"] == "toolu_01XyHKvNKpNB7RqiN2s9tGag"
    assert "haven't granted it yet" in result.data["output_summary"]


def test_session_started_lists_tools() -> None:
    started = replay("tool_denied")[0]

    assert "Read" in started.data["tools"]
    assert "Bash" not in started.data["tools"]  # --disallowedTools removes it from the list


# Delta / full message deduplication


def stream(*objs: dict[str, Any]) -> list[ParsedEvent]:
    parser = ClaudeTurnParser()
    return [e for obj in objs for e in parser.feed(json.dumps(obj))]


def message_start(message_id: str) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {"type": "message_start", "message": {"id": message_id}},
    }


def text_delta(index: int, text: str) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    }


def assistant(message_id: str, *blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"id": message_id, "content": list(blocks)}}


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def messages(events: list[ParsedEvent]) -> list[tuple[str, bool]]:
    return [(e.data["text"], e.data["from_deltas"]) for e in events if e.type is EventType.MESSAGE]


def test_message_after_deltas_is_marked() -> None:
    events = stream(
        message_start("m1"),
        text_delta(0, "Hel"),
        text_delta(0, "lo"),
        assistant("m1", text("Hello")),
    )

    assert [e.data["text"] for e in events if e.type is EventType.TEXT_DELTA] == ["Hel", "lo"]
    assert messages(events) == [("Hello", True)]


def test_message_without_deltas_is_not_marked() -> None:
    assert messages(stream(assistant("m1", text("Hello")))) == [("Hello", False)]


def test_deltas_of_other_message_do_not_count() -> None:
    events = stream(
        message_start("m1"),
        text_delta(0, "first"),
        assistant("m1", text("first")),
        assistant("m2", text("second")),
    )

    assert messages(events) == [("first", True), ("second", False)]


def test_several_text_blocks_in_one_message() -> None:
    events = stream(
        message_start("m1"),
        text_delta(0, "one"),
        text_delta(2, "two"),
        assistant(
            "m1",
            text("one"),
            {"type": "tool_use", "id": "t", "name": "Read", "input": {}},
            text("two"),
        ),
        assistant("m1", text("three")),
    )

    assert messages(events) == [("one", True), ("two", True), ("three", False)]


# Robustness: unknown input becomes `raw`, never an exception


@pytest.mark.parametrize(
    "line",
    [
        "not json at all",
        "[1, 2, 3]",
        '{"type": "brand_new_event"}',
        '{"no_type": true}',
        '{"type": "system", "subtype": "compact_boundary"}',
        '{"type": "stream_event", "event": {"type": "brand_new"}}',
        '{"type": "stream_event", "event": {"type": "content_block_delta", "delta": {}}}',
        '{"type": "assistant", "message": "oops"}',
        '{"type": "system", "subtype": "init"}',
    ],
)
def test_unknown_input_becomes_raw(line: str) -> None:
    [event] = ClaudeTurnParser().feed(line)

    assert event.type is EventType.RAW
    assert event.raw == line


def test_unknown_content_block_is_kept_as_raw_next_to_known_ones() -> None:
    events = stream(assistant("m1", text("hi"), {"type": "hologram"}))

    assert [e.type for e in events] == [EventType.MESSAGE, EventType.RAW]


def test_blank_lines_and_skipped_lines_produce_nothing() -> None:
    parser = ClaudeTurnParser()

    assert parser.feed("   ") == []
    assert parser.feed('{"type": "system", "subtype": "status", "status": "requesting"}') == []
    assert parser.feed(json.dumps(assistant("m1", {"type": "thinking", "thinking": ""}))) == []


# finish()


def test_finish_reports_missing_result_with_stderr() -> None:
    [error] = ClaudeTurnParser().finish(2, ["first", "Error: boom"])

    assert error.type is EventType.ERROR
    assert error.data["fatal"] is True
    assert "code 2" in error.data["message"] and "Error: boom" in error.data["message"]


def test_finish_reports_clean_exit_without_result() -> None:
    [error] = ClaudeTurnParser().finish(0, [])

    assert error.data == {"message": "stream ended without a result", "fatal": True}


def test_finish_after_result_adds_nothing() -> None:
    parser = ClaudeTurnParser()
    parser.feed('{"type": "result", "is_error": true, "result": "x", "session_id": "s"}')

    assert parser.finish(1, ["[claude-code:unrecognized_model] {}"]) == []


# Rate limits


REJECTED = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "rejected",
        "resetsAt": 1791241200,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
    },
}
ALLOWED = {
    "type": "rate_limit_event",
    "rate_limit_info": {"status": "allowed", "resetsAt": 1791241200, "overageStatus": "rejected"},
}
API_RATE_LIMIT = {
    "type": "assistant",
    "message": {"id": "x", "content": [text("You've hit your usage limit.")]},
    "error": "rate_limit",
    "is_api_error_message": True,
}


@pytest.mark.parametrize(
    ("obj", "limited"),
    [
        (REJECTED, True),
        (ALLOWED, False),
        ({**REJECTED, "rate_limit_info": {"status": "allowed_warning"}}, False),
        (API_RATE_LIMIT, True),
        ({**API_RATE_LIMIT, "error": "overloaded"}, False),
        (
            {"type": "result", "is_error": True, "api_error_status": 429, "result": "Slow down"},
            True,
        ),
        ({"type": "result", "is_error": True, "result": "Claude usage limit reached"}, True),
        ({"type": "result", "is_error": False, "result": "Explaining rate limit headers"}, False),
        (assistant("m", text("Your usage limit resets daily")), False),
    ],
)
def test_detect_rate_limit(obj: dict[str, Any], limited: bool) -> None:
    assert (detect_rate_limit(obj) is not None) is limited


def test_rejected_event_carries_reset_time() -> None:
    signal = detect_rate_limit(REJECTED)

    assert signal is not None
    assert signal.resets_at == datetime.fromtimestamp(1791241200, UTC)
    assert "five_hour" in signal.message


def test_rate_limited_emitted_once_per_turn() -> None:
    events = stream(
        ALLOWED,
        REJECTED,
        API_RATE_LIMIT,
        {
            "type": "result",
            "is_error": True,
            "api_error_status": 429,
            "result": "limit",
            "session_id": "s",
        },
    )

    limited = [e for e in events if e.type is EventType.RATE_LIMITED]
    assert len(limited) == 1
    assert limited[0].data["resets_at"] == "2026-10-05T23:00:00+00:00"
    assert events[-1].type is EventType.TURN_COMPLETED


def test_api_rate_limit_uses_reset_from_earlier_event() -> None:
    [event] = stream(ALLOWED, API_RATE_LIMIT)

    assert event.type is EventType.RATE_LIMITED
    assert event.data["message"] == "You've hit your usage limit."
    assert event.data["resets_at"] == "2026-10-05T23:00:00+00:00"


def test_rate_limit_text_in_stderr_without_result() -> None:
    events = ClaudeTurnParser().finish(1, ["Error: usage limit reached"])

    assert [e.type for e in events] == [EventType.RATE_LIMITED, EventType.ERROR]


# Summaries


def test_tool_input_summary_prefers_descriptive_keys() -> None:
    assert summarize_tool_input({"command": "ls -la", "description": "list"}) == "ls -la"
    assert summarize_tool_input({"file_path": "/a/b.py", "limit": 10}) == "/a/b.py"
    assert summarize_tool_input({"x": 1}) == '{"x": 1}'
    assert summarize_tool_input(None) == ""


def test_tool_output_summary_handles_block_lists() -> None:
    content = [text("line 1"), {"type": "image", "source": {}}, text("line 2")]

    assert summarize_tool_output(content) == "line 1\n[image]\nline 2"
    assert summarize_tool_output("plain") == "plain"


# Command building and validation


def request(agent: AgentSpec, resume_id: str | None = None) -> TurnRequest:
    return TurnRequest(
        agent=agent, prompt="Describe this repo", workdir=Path("/w"), resume_id=resume_id
    )


def test_minimal_command() -> None:
    command = ClaudeAdapter().build_command(request(spec()))

    assert command.argv == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--append-system-prompt",
        "You review code.",
    ]
    assert command.stdin_text == "Describe this repo"
    assert command.env_overrides == {}


def test_full_command() -> None:
    agent = spec(
        model="sonnet",
        tools=["Read", "Bash(git log:*)"],
        disallowed_tools=["Edit", "Write"],
        permission_mode="dontAsk",
        provider_options={"executable": "/opt/claude", "extra_args": ["--effort", "low"]},
    )

    argv = ClaudeAdapter().build_command(request(agent, resume_id="abc-123")).argv

    assert argv == [
        "/opt/claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--resume",
        "abc-123",
        "--model",
        "sonnet",
        "--allowedTools",
        "Read",
        "Bash(git log:*)",
        "--disallowedTools",
        "Edit",
        "Write",
        "--permission-mode",
        "dontAsk",
        "--append-system-prompt",
        "You review code.",
        "--effort",
        "low",
    ]
    assert "Describe this repo" not in argv


def test_valid_agent() -> None:
    agent = spec(permission_mode="default", provider_options={"extra_args": ["--effort", "low"]})

    assert ClaudeAdapter().validate_agent(agent) == []


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"provider_options": {"timeout": 5}}, "unknown provider option 'timeout'"),
        ({"provider_options": {"executable": ""}}, "'executable' must be a non-empty string"),
        ({"provider_options": {"extra_args": "--x"}}, "'extra_args' must be a list of strings"),
        (
            {"provider_options": {"extra_args": ["--dangerously-skip-permissions"]}},
            "must not bypass permissions",
        ),
        (
            {"provider_options": {"extra_args": ["--allow-dangerously-skip-permissions"]}},
            "must not bypass permissions",
        ),
        (
            {"provider_options": {"extra_args": ["--permission-mode=bypassPermissions"]}},
            "must not bypass permissions",
        ),
        ({"permission_mode": "bypassPermissions"}, "'bypassPermissions' is not allowed"),
        ({"permission_mode": "yolo"}, "unknown permission_mode 'yolo'"),
    ],
)
def test_invalid_agent(fields: dict[str, Any], problem: str) -> None:
    problems = ClaudeAdapter().validate_agent(spec(**fields))

    assert len(problems) == 1
    assert problem in problems[0]


def test_registry() -> None:
    assert isinstance(get_adapter("claude"), ClaudeAdapter)
    assert isinstance(get_adapter("claude").new_parser(spec()), ClaudeTurnParser)
