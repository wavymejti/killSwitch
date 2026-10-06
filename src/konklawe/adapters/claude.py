"""Adapter for Claude Code (`claude -p --output-format stream-json`).

Stream format notes: docs/notes/claude-stream-format.md (recorded with Claude Code 2.1.289).
"""

import json
import re
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from konklawe.adapters.base import CommandSpec, ProviderAdapter, TurnParser, TurnRequest
from konklawe.agents import AgentSpec
from konklawe.events import ParsedEvent

BASE_ARGS = [
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
    "--include-partial-messages",
]

PERMISSION_MODES = frozenset({"acceptEdits", "auto", "default", "manual", "dontAsk", "plan"})
FORBIDDEN_ARG_FRAGMENTS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "bypassPermissions",
)
OPTION_KEYS = frozenset({"executable", "extra_args"})

# Lines that carry nothing the common format needs (see DECISIONS.md).
SKIPPED_SYSTEM_SUBTYPES = frozenset({"status", "thinking_tokens", "permission_denied"})
SKIPPED_STREAM_EVENTS = frozenset(
    {"message_start", "content_block_start", "content_block_stop", "message_delta", "message_stop"}
)
SKIPPED_DELTAS = frozenset({"thinking_delta", "signature_delta", "input_json_delta"})
SKIPPED_BLOCKS = frozenset({"thinking", "redacted_thinking"})

# Input keys that best describe a tool call, in order of preference.
SUMMARY_KEYS = (
    "command",
    "file_path",
    "notebook_path",
    "pattern",
    "url",
    "query",
    "description",
    "path",
    "prompt",
)


class ClaudeAdapter(ProviderAdapter):
    name = "claude"

    def validate_agent(self, spec: AgentSpec) -> list[str]:
        problems = []
        options = spec.provider_options
        for key in sorted(set(options) - OPTION_KEYS):
            problems.append(f"unknown provider option '{key}' for claude")
        executable = options.get("executable", "claude")
        if not isinstance(executable, str) or not executable.strip():
            problems.append("provider option 'executable' must be a non-empty string")
        extra_args = options.get("extra_args", [])
        if not isinstance(extra_args, list) or not all(isinstance(a, str) for a in extra_args):
            problems.append("provider option 'extra_args' must be a list of strings")
        else:
            for arg in extra_args:
                if any(fragment in arg for fragment in FORBIDDEN_ARG_FRAGMENTS):
                    problems.append(f"extra_args must not bypass permissions: '{arg}'")
        if spec.permission_mode == "bypassPermissions":
            problems.append("permission_mode 'bypassPermissions' is not allowed")
        elif spec.permission_mode is not None and spec.permission_mode not in PERMISSION_MODES:
            allowed = ", ".join(sorted(PERMISSION_MODES))
            problems.append(
                f"unknown permission_mode '{spec.permission_mode}' (allowed: {allowed})"
            )
        return problems

    def build_command(self, req: TurnRequest) -> CommandSpec:
        spec = req.agent
        argv = [spec.provider_options.get("executable", "claude"), *BASE_ARGS]
        if req.resume_id:
            argv += ["--resume", req.resume_id]
        if spec.model:
            argv += ["--model", spec.model]
        # Variadic flags: each tool is its own argument; the next flag ends the list.
        if spec.tools:
            argv += ["--allowedTools", *spec.tools]
        if spec.disallowed_tools:
            argv += ["--disallowedTools", *spec.disallowed_tools]
        if spec.permission_mode:
            argv += ["--permission-mode", spec.permission_mode]
        argv += ["--append-system-prompt", spec.role_prompt]
        argv += spec.provider_options.get("extra_args", [])
        # The prompt always goes through stdin: a positional argument after a variadic
        # flag would be swallowed as a tool name, and Windows caps command-line length.
        return CommandSpec(argv=argv, stdin_text=req.prompt)

    def new_parser(self, agent: AgentSpec) -> "ClaudeTurnParser":
        return ClaudeTurnParser()


@dataclass(frozen=True)
class RateLimitSignal:
    message: str
    resets_at: datetime | None = None


RATE_LIMIT_ERROR_CODES = frozenset({"rate_limit"})
# Fallback for error texts only, never for ordinary model output.
# TODO: confirm against real output once a subscription limit is actually hit.
RATE_LIMIT_TEXT_PATTERNS = (
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"limit (?:reached|exceeded)", re.IGNORECASE),
    re.compile(r"limit will reset", re.IGNORECASE),
)


def detect_rate_limit(obj: dict[str, Any]) -> RateLimitSignal | None:
    """Recognise an exhausted subscription limit in one decoded stream object.

    Structured signals first: `rate_limit_event` with status `rejected`, an API error
    message with code `rate_limit`. Text patterns apply only to error results.
    """
    kind = obj.get("type")
    if kind == "rate_limit_event":
        info = obj.get("rate_limit_info") or {}
        # `overageStatus: rejected` also appears on normal turns; only `status` counts.
        if info.get("status") == "rejected":
            window = info.get("rateLimitType")
            message = f"subscription limit reached ({window})" if window else "limit reached"
            return RateLimitSignal(message, _epoch_to_datetime(info.get("resetsAt")))
        return None
    if kind == "assistant" and obj.get("error") in RATE_LIMIT_ERROR_CODES:
        return RateLimitSignal(_assistant_text(obj) or "rate limit reached")
    if kind == "result" and obj.get("is_error"):
        text = str(obj.get("result") or "")
        if obj.get("api_error_status") == 429 or matches_rate_limit_text(text):
            return RateLimitSignal(text or "rate limit reached")
    return None


def matches_rate_limit_text(text: str) -> bool:
    return any(pattern.search(text) for pattern in RATE_LIMIT_TEXT_PATTERNS)


class ClaudeTurnParser(TurnParser):
    def __init__(self) -> None:
        self._message_id: str | None = None
        # Per message: indexes of text blocks that streamed deltas, not yet matched to the
        # full `assistant` block (which arrives right before the block's `content_block_stop`).
        self._streamed: dict[str, deque[int]] = {}
        self._result_seen = False
        self._rate_limited = False
        self._last_resets_at: datetime | None = None

    def feed(self, line: str) -> list[ParsedEvent]:
        line = line.strip()
        if not line:
            return []
        try:
            obj = json.loads(line)
        except ValueError:
            return [ParsedEvent.unrecognized(line, note="not JSON")]
        if not isinstance(obj, dict):
            return [ParsedEvent.unrecognized(line, note="not a JSON object")]
        try:
            return self._dispatch(obj, line)
        except Exception as exc:  # a parser bug must not end the turn
            return [ParsedEvent.unrecognized(line, note=f"parser error: {exc!r}")]

    def finish(self, exit_code: int | None, stderr_tail: list[str]) -> list[ParsedEvent]:
        events: list[ParsedEvent] = []
        if not self._rate_limited and any(matches_rate_limit_text(s) for s in stderr_tail):
            events.extend(self._rate_limited_event(RateLimitSignal("\n".join(stderr_tail))))
        if not self._result_seen:
            reason = (
                "stream ended without a result"
                if exit_code == 0
                else f"claude exited with code {exit_code} without a result"
            )
            details = "\n".join(stderr_tail[-10:])
            message = f"{reason}\n{details}" if details else reason
            events.append(ParsedEvent.error(message, fatal=True))
        return events

    def _dispatch(self, obj: dict[str, Any], line: str) -> list[ParsedEvent]:
        match obj.get("type"):
            case "system":
                return self._system(obj, line)
            case "stream_event":
                return self._stream_event(obj, line)
            case "assistant":
                return self._assistant(obj, line)
            case "user":
                return self._user(obj, line)
            case "rate_limit_event":
                return self._rate_limit_event(obj, line)
            case "result":
                return self._result(obj, line)
            case _:
                return [ParsedEvent.unrecognized(line)]

    def _system(self, obj: dict[str, Any], line: str) -> list[ParsedEvent]:
        subtype = obj.get("subtype")
        if subtype == "init":
            tools = obj.get("tools")
            return [
                ParsedEvent.session_started(
                    str(obj["session_id"]),
                    model=obj.get("model"),
                    tools=[str(t) for t in tools] if isinstance(tools, list) else None,
                    raw=line,
                )
            ]
        if subtype in SKIPPED_SYSTEM_SUBTYPES:
            return []
        return [ParsedEvent.unrecognized(line)]

    def _stream_event(self, obj: dict[str, Any], line: str) -> list[ParsedEvent]:
        event = obj.get("event") or {}
        kind = event.get("type")
        if kind == "message_start":
            self._message_id = (event.get("message") or {}).get("id")
            return []
        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                self._note_streamed(event.get("index"))
                return [ParsedEvent.text_delta(delta.get("text", ""), self._message_id, raw=line)]
            if delta_type in SKIPPED_DELTAS:
                return []
            return [ParsedEvent.unrecognized(line)]
        if kind in SKIPPED_STREAM_EVENTS:
            return []
        return [ParsedEvent.unrecognized(line)]

    def _note_streamed(self, index: Any) -> None:
        if self._message_id is None or not isinstance(index, int):
            return
        pending = self._streamed.setdefault(self._message_id, deque())
        if index not in pending:
            pending.append(index)

    def _assistant(self, obj: dict[str, Any], line: str) -> list[ParsedEvent]:
        message = obj.get("message") or {}
        message_id = message.get("id")
        if obj.get("error") or obj.get("is_api_error_message"):
            signal = detect_rate_limit(obj)
            if signal:
                return self._rate_limited_event(signal, line)
            text = _assistant_text(obj) or "API error"
            return [ParsedEvent.error(text, fatal=False, raw=line, code=obj.get("error"))]

        events: list[ParsedEvent] = []
        unknown = False
        for block in _blocks(message.get("content")):
            kind = block.get("type")
            if kind == "text":
                pending = self._streamed.get(message_id)
                from_deltas = bool(pending)
                if pending:
                    pending.popleft()
                events.append(
                    ParsedEvent.message(block.get("text", ""), message_id, from_deltas, raw=line)
                )
            elif kind == "tool_use":
                events.append(
                    ParsedEvent.tool_call(
                        str(block.get("id")),
                        str(block.get("name")),
                        summarize_tool_input(block.get("input")),
                        raw=line,
                    )
                )
            elif kind not in SKIPPED_BLOCKS:
                unknown = True
        if unknown:
            events.append(ParsedEvent.unrecognized(line, note="unknown assistant content block"))
        return events

    def _user(self, obj: dict[str, Any], line: str) -> list[ParsedEvent]:
        events = []
        for block in _blocks((obj.get("message") or {}).get("content")):
            if block.get("type") == "tool_result":
                events.append(
                    ParsedEvent.tool_result(
                        str(block.get("tool_use_id")),
                        bool(block.get("is_error", False)),
                        summarize_tool_output(block.get("content")),
                        raw=line,
                    )
                )
        return events

    def _rate_limit_event(self, obj: dict[str, Any], line: str) -> list[ParsedEvent]:
        resets_at = _epoch_to_datetime((obj.get("rate_limit_info") or {}).get("resetsAt"))
        if resets_at:
            self._last_resets_at = resets_at
        signal = detect_rate_limit(obj)
        return self._rate_limited_event(signal, line) if signal else []

    def _result(self, obj: dict[str, Any], line: str) -> list[ParsedEvent]:
        self._result_seen = True
        events: list[ParsedEvent] = []
        signal = detect_rate_limit(obj)
        if signal:
            events.extend(self._rate_limited_event(signal))
        usage = obj.get("usage")
        cost = obj.get("total_cost_usd")
        events.append(
            ParsedEvent.turn_completed(
                is_error=bool(obj.get("is_error", False)),
                result_text=obj.get("result"),
                duration_ms=obj.get("duration_ms"),
                num_turns=obj.get("num_turns"),
                usage=usage if isinstance(usage, dict) else None,
                cost_estimate_usd=float(cost) if isinstance(cost, int | float) else None,
                provider_session_id=obj.get("session_id"),
                raw=line,
            )
        )
        return events

    def _rate_limited_event(
        self, signal: RateLimitSignal, line: str | None = None
    ) -> list[ParsedEvent]:
        if self._rate_limited:
            return []
        self._rate_limited = True
        resets_at = signal.resets_at or self._last_resets_at
        return [ParsedEvent.rate_limited(signal.message, resets_at, raw=line)]


def summarize_tool_input(tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        for key in SUMMARY_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                return value
        return json.dumps(tool_input, ensure_ascii=False)
    return "" if tool_input is None else str(tool_input)


def summarize_tool_output(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, dict):
                parts.append(f"[{block.get('type', 'content')}]")
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return "" if content is None else json.dumps(content, ensure_ascii=False)


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


def _assistant_text(obj: dict[str, Any]) -> str:
    content = (obj.get("message") or {}).get("content")
    return "\n".join(
        str(b.get("text", "")) for b in _blocks(content) if b.get("type") == "text"
    ).strip()


def _epoch_to_datetime(value: Any) -> datetime | None:
    if isinstance(value, int | float) and value > 0:
        return datetime.fromtimestamp(value, UTC)
    return None
