"""Common event format shared by every part of konklawe outside the adapters.

The `data` contract for each event type lives in the `ParsedEvent` constructors below;
adapters and the runner build events only through them.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator

INPUT_SUMMARY_LIMIT = 300
OUTPUT_SUMMARY_LIMIT = 2000
ELLIPSIS = "…"


class EventType(StrEnum):
    STATUS = "status"
    SESSION_STARTED = "session_started"
    TEXT_DELTA = "text_delta"
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TURN_COMPLETED = "turn_completed"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"
    STDERR = "stderr"
    PROCESS_EXITED = "process_exited"
    RAW = "raw"


class State(StrEnum):
    """Session and turn states carried by `status` events."""

    STARTING = "starting"
    RUNNING = "running"
    IDLE = "idle"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LIMITED = "limited"


def utc_now() -> datetime:
    return datetime.now(UTC)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - len(ELLIPSIS)] + ELLIPSIS


def _compact(**fields: Any) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


class ParsedEvent(BaseModel):
    """Event produced by a provider parser (or the runner), before it gets an identity."""

    model_config = ConfigDict(frozen=True)

    type: EventType
    data: dict[str, Any]
    raw: str | None = None

    @classmethod
    def status(cls, state: State, reason: str | None = None) -> Self:
        return cls(type=EventType.STATUS, data=_compact(state=state.value, reason=reason))

    @classmethod
    def session_started(
        cls,
        provider_session_id: str,
        model: str | None = None,
        tools: list[str] | None = None,
        raw: str | None = None,
    ) -> Self:
        data = _compact(provider_session_id=provider_session_id, model=model, tools=tools)
        return cls(type=EventType.SESSION_STARTED, data=data, raw=raw)

    @classmethod
    def text_delta(cls, text: str, message_id: str | None = None, raw: str | None = None) -> Self:
        return cls(
            type=EventType.TEXT_DELTA, data=_compact(text=text, message_id=message_id), raw=raw
        )

    @classmethod
    def message(
        cls,
        text: str,
        message_id: str | None = None,
        from_deltas: bool = False,
        raw: str | None = None,
    ) -> Self:
        data = _compact(text=text, message_id=message_id, from_deltas=from_deltas)
        return cls(type=EventType.MESSAGE, data=data, raw=raw)

    @classmethod
    def tool_call(cls, tool_id: str, name: str, input_summary: str, raw: str | None = None) -> Self:
        data = {
            "tool_id": tool_id,
            "name": name,
            "input_summary": truncate(input_summary, INPUT_SUMMARY_LIMIT),
        }
        return cls(type=EventType.TOOL_CALL, data=data, raw=raw)

    @classmethod
    def tool_result(
        cls, tool_id: str, is_error: bool, output_summary: str, raw: str | None = None
    ) -> Self:
        data = {
            "tool_id": tool_id,
            "is_error": is_error,
            "output_summary": truncate(output_summary, OUTPUT_SUMMARY_LIMIT),
        }
        return cls(type=EventType.TOOL_RESULT, data=data, raw=raw)

    @classmethod
    def turn_completed(
        cls,
        *,
        is_error: bool,
        result_text: str | None = None,
        duration_ms: int | None = None,
        num_turns: int | None = None,
        usage: dict[str, Any] | None = None,
        cost_estimate_usd: float | None = None,
        provider_session_id: str | None = None,
        raw: str | None = None,
    ) -> Self:
        data = _compact(
            result_text=result_text,
            is_error=is_error,
            duration_ms=duration_ms,
            num_turns=num_turns,
            usage=usage,
            cost_estimate_usd=cost_estimate_usd,
            provider_session_id=provider_session_id,
        )
        return cls(type=EventType.TURN_COMPLETED, data=data, raw=raw)

    @classmethod
    def rate_limited(
        cls, message: str, resets_at: datetime | None = None, raw: str | None = None
    ) -> Self:
        # Stored as ISO 8601 text so `data` stays plain JSON.
        reset = resets_at.astimezone(UTC).isoformat() if resets_at else None
        return cls(
            type=EventType.RATE_LIMITED, data=_compact(message=message, resets_at=reset), raw=raw
        )

    @classmethod
    def error(cls, message: str, fatal: bool, raw: str | None = None, **extra: Any) -> Self:
        return cls(
            type=EventType.ERROR, data={"message": message, "fatal": fatal, **extra}, raw=raw
        )

    @classmethod
    def stderr(cls, line: str) -> Self:
        return cls(type=EventType.STDERR, data={"line": line})

    @classmethod
    def process_exited(
        cls, exit_code: int | None, duration_ms: int, signal: str | None = None
    ) -> Self:
        data = {"exit_code": exit_code, "duration_ms": duration_ms, **_compact(signal=signal)}
        return cls(type=EventType.PROCESS_EXITED, data=data)

    @classmethod
    def unrecognized(cls, line: str, note: str | None = None) -> Self:
        """A `raw` event: a stream line the parser did not recognise."""
        return cls(type=EventType.RAW, data=_compact(note=note), raw=line)


class AgentEvent(BaseModel):
    """Event stored in the journal and published on the bus."""

    model_config = ConfigDict(frozen=True)

    seq: int | None = None  # assigned by the journal
    ts: datetime
    session_id: str
    turn_id: str | None
    agent: str
    provider: str
    type: EventType
    data: dict[str, Any]
    raw: str | None = None

    @field_validator("ts")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @classmethod
    def from_parsed(
        cls,
        parsed: ParsedEvent,
        *,
        session_id: str,
        turn_id: str | None,
        agent: str,
        provider: str,
        ts: datetime | None = None,
    ) -> Self:
        return cls(
            ts=ts or utc_now(),
            session_id=session_id,
            turn_id=turn_id,
            agent=agent,
            provider=provider,
            type=parsed.type,
            data=parsed.data,
            raw=parsed.raw,
        )
