"""Terminal rendering of events, shared by live view and journal replay."""

import zlib
from typing import Any

from rich.console import Console
from rich.text import Text

from konklawe.bus import EventsDropped
from konklawe.events import AgentEvent, EventType

AGENT_COLORS = (
    "cyan",
    "green",
    "magenta",
    "blue",
    "yellow",
    "bright_cyan",
    "bright_green",
    "bright_magenta",
    "bright_blue",
)
DIM = "dim"
SUMMARY_PREVIEW = 200
VISIBLE_STATES = {"failed": "bold red", "cancelled": "yellow", "limited": "bold magenta"}


def agent_color(agent: str) -> str:
    """Stable per-agent colour (the same live and on replay)."""
    return AGENT_COLORS[zlib.crc32(agent.encode("utf-8")) % len(AGENT_COLORS)]


class Renderer:
    """Prints events as readable lines prefixed with `[agent]`.

    `stream=True` writes text deltas as they arrive (one agent at a time). With
    `stream=False`, text is buffered per session and printed as whole lines, so agents
    working in parallel never interleave characters.
    """

    def __init__(
        self,
        console: Console,
        *,
        verbose: bool = False,
        stream: bool = True,
        labels: dict[str, str] | None = None,
    ) -> None:
        self.console = console
        self.verbose = verbose
        self.stream = stream
        self.labels = labels or {}  # session id -> prefix label (default: agent name)
        self._buffers: dict[str, str] = {}
        self._agents: dict[str, str] = {}
        self._open_line: str | None = None  # session whose streamed line is unfinished

    def render(self, item: AgentEvent | EventsDropped) -> None:
        if isinstance(item, EventsDropped):
            self._end_open_line()
            self.console.print(
                Text(f"! {item.count} events skipped by the live view (all are in the journal)"),
                style="yellow",
            )
            return
        event = item
        self._agents[event.session_id] = event.agent
        if event.type is EventType.TEXT_DELTA:
            self._text(event.session_id, event.data.get("text", ""))
            return
        if event.type is EventType.MESSAGE and event.data.get("from_deltas"):
            self._end_text(event.session_id)
            return
        self._end_text(event.session_id)
        self._end_open_line()
        for line in self._describe(event):
            self._print_line(event.session_id, line)

    def close(self) -> None:
        """Print any unfinished text."""
        for session_id in list(self._buffers):
            self._flush_buffer(session_id)
        self._end_open_line()

    # Text

    def _text(self, session_id: str, text: str) -> None:
        if not self.stream:
            self._buffers[session_id] = self._buffers.get(session_id, "") + text
            *complete, rest = self._buffers[session_id].split("\n")
            for line in complete:
                self._print_line(session_id, Text(line))
            self._buffers[session_id] = rest
            return
        for index, part in enumerate(text.split("\n")):
            if index > 0:
                self.console.print()
                self._open_line = None
            if not part:
                continue
            if self._open_line != session_id:
                self._end_open_line()
                self.console.print(self._prefix(session_id), end="", soft_wrap=True)
                self._open_line = session_id
            self.console.print(Text(part), end="", soft_wrap=True)

    def _end_text(self, session_id: str) -> None:
        if self.stream:
            if self._open_line == session_id:
                self._end_open_line()
        else:
            self._flush_buffer(session_id)

    def _flush_buffer(self, session_id: str) -> None:
        rest = self._buffers.pop(session_id, "")
        if rest:
            self._print_line(session_id, Text(rest))

    def _end_open_line(self) -> None:
        if self._open_line is not None:
            self.console.print()
            self._open_line = None

    def _print_line(self, session_id: str, line: Text) -> None:
        for part in line.split("\n") if "\n" in line.plain else [line]:
            self.console.print(self._prefix(session_id) + part, soft_wrap=True)

    def _prefix(self, session_id: str) -> Text:
        agent = self._agents.get(session_id, "?")
        label = self.labels.get(session_id, agent)
        return Text(f"[{label}] ", style=f"bold {agent_color(agent)}")

    # Other events

    def _describe(self, event: AgentEvent) -> list[Text]:
        data = event.data
        match event.type:
            case EventType.MESSAGE:
                return [Text(data.get("text", ""))]
            case EventType.TOOL_CALL:
                return [Text(f"▸ {data.get('name')} {data.get('input_summary', '')}", style=DIM)]
            case EventType.TOOL_RESULT:
                summary = _first_line(data.get("output_summary", ""))
                if data.get("is_error"):
                    return [Text(f"✗ {summary}", style="red")]
                return [Text(f"✓ {summary}", style=DIM)] if self.verbose else []
            case EventType.TURN_COMPLETED:
                return [Text(footer(data), style=DIM)]
            case EventType.RATE_LIMITED:
                reset = f" (resets {data['resets_at']})" if data.get("resets_at") else ""
                return [Text(f"⏸ rate limited: {data.get('message')}{reset}", style="bold magenta")]
            case EventType.ERROR:
                if data.get("fatal"):
                    return [Text(f"✗ error: {data.get('message')}", style="bold red")]
                return [Text(f"! {data.get('message')}", style="yellow")]
            case EventType.STATUS:
                return self._status(data)
            case EventType.SESSION_STARTED if self.verbose:
                model = f" · {data['model']}" if data.get("model") else ""
                return [Text(f"session {data.get('provider_session_id')}{model}", style=DIM)]
            case EventType.STDERR if self.verbose:
                return [Text(f"stderr: {data.get('line')}", style=DIM)]
            case EventType.PROCESS_EXITED if self.verbose:
                signal = f" ({data['signal']})" if data.get("signal") else ""
                text = f"process exited: code {data.get('exit_code')}{signal}"
                return [Text(f"{text} after {data.get('duration_ms')} ms", style=DIM)]
            case EventType.RAW if self.verbose:
                note = f" ({data['note']})" if data.get("note") else ""
                return [Text(f"raw{note}: {_first_line(event.raw or '')}", style=DIM)]
            case _:
                return []

    def _status(self, data: dict[str, Any]) -> list[Text]:
        state = data.get("state", "")
        reason = f": {data['reason']}" if data.get("reason") else ""
        if state in VISIBLE_STATES:
            return [Text(f"■ {state}{reason}", style=VISIBLE_STATES[state])]
        if self.verbose:
            return [Text(f"· {state}{reason}", style=DIM)]
        return []


def footer(data: dict[str, Any]) -> str:
    """One-line turn summary: time, steps, tokens and the API-equivalent cost estimate."""
    parts = []
    if data.get("duration_ms") is not None:
        parts.append(f"{data['duration_ms'] / 1000:.1f}s")
    if data.get("num_turns") is not None:
        steps = data["num_turns"]
        parts.append(f"{steps} step{'s' if steps != 1 else ''}")
    usage = data.get("usage")
    if isinstance(usage, dict):
        tokens_in = sum(v for k, v in usage.items() if k.endswith("input_tokens") and _num(v))
        tokens_out = sum(v for k, v in usage.items() if k.endswith("output_tokens") and _num(v))
        parts.append(f"{tokens_in:,} in / {tokens_out:,} out tokens")
    if data.get("cost_estimate_usd") is not None:
        parts.append(f"≈ ${data['cost_estimate_usd']:.4f} (API estimate, not a charge)")
    label = "turn failed" if data.get("is_error") else "done"
    return f"── {label}" + (f" · {' · '.join(parts)}" if parts else "")


def _num(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _first_line(text: str) -> str:
    line = text.strip().split("\n", 1)[0]
    return line if len(line) <= SUMMARY_PREVIEW else line[: SUMMARY_PREVIEW - 1] + "…"
