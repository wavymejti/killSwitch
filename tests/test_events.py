from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from konklawe.events import (
    INPUT_SUMMARY_LIMIT,
    OUTPUT_SUMMARY_LIMIT,
    AgentEvent,
    EventType,
    ParsedEvent,
    State,
    truncate,
)


def test_truncate() -> None:
    assert truncate("abc", 3) == "abc"
    assert truncate("abcdef", 4) == "abc…"
    assert len(truncate("x" * 1000, 300)) == 300


def test_optional_fields_are_omitted() -> None:
    event = ParsedEvent.status(State.RUNNING)

    assert event.type is EventType.STATUS
    assert event.data == {"state": "running"}
    assert ParsedEvent.session_started("abc").data == {"provider_session_id": "abc"}


def test_summaries_are_truncated() -> None:
    call = ParsedEvent.tool_call("t1", "Read", "x" * 1000)
    result = ParsedEvent.tool_result("t1", False, "y" * 5000)

    assert len(call.data["input_summary"]) == INPUT_SUMMARY_LIMIT
    assert len(result.data["output_summary"]) == OUTPUT_SUMMARY_LIMIT
    assert result.data["is_error"] is False


def test_rate_limited_reset_is_utc_iso_text() -> None:
    reset = datetime(2026, 10, 6, 14, 0, tzinfo=timezone(timedelta(hours=2)))

    event = ParsedEvent.rate_limited("limit reached", reset)

    assert event.data == {"message": "limit reached", "resets_at": "2026-10-06T12:00:00+00:00"}


def test_turn_completed_keeps_false_values() -> None:
    event = ParsedEvent.turn_completed(is_error=False, num_turns=0, cost_estimate_usd=0.0)

    assert event.data == {"is_error": False, "num_turns": 0, "cost_estimate_usd": 0.0}


def test_unrecognized_keeps_line_in_raw() -> None:
    event = ParsedEvent.unrecognized("not json", note="invalid JSON")

    assert event.type is EventType.RAW
    assert event.raw == "not json"
    assert event.data == {"note": "invalid JSON"}


def test_agent_event_from_parsed() -> None:
    parsed = ParsedEvent.text_delta("Hi", message_id="m1", raw='{"x":1}')

    event = AgentEvent.from_parsed(
        parsed, session_id="s1", turn_id="t1", agent="echo", provider="fake"
    )

    assert event.seq is None
    assert event.ts.tzinfo is UTC
    assert (event.type, event.data, event.raw) == (EventType.TEXT_DELTA, parsed.data, '{"x":1}')


def test_agent_event_converts_to_utc_and_rejects_naive_time() -> None:
    local = datetime(2026, 10, 6, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    common = dict(session_id="s", turn_id=None, agent="a", provider="p", type="stderr", data={})

    assert AgentEvent(ts=local, **common).ts == datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    with pytest.raises(ValidationError):
        AgentEvent(ts=datetime(2026, 10, 6, 12, 0), **common)


def test_json_round_trip() -> None:
    event = AgentEvent.from_parsed(
        ParsedEvent.error("boom", fatal=True, code="model_not_found"),
        session_id="s",
        turn_id=None,
        agent="a",
        provider="p",
    )

    assert AgentEvent.model_validate_json(event.model_dump_json()) == event
    assert event.data == {"message": "boom", "fatal": True, "code": "model_not_found"}
