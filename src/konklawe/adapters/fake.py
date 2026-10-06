"""Fake adapter: runs `konklawe.fake_cli` to replay a recorded stream through a real parser."""

import sys
from pathlib import Path

from konklawe.adapters.base import CommandSpec, ProviderAdapter, TurnParser, TurnRequest
from konklawe.agents import AgentSpec

# Repository root (src/konklawe/adapters/fake.py -> repo); relative fixture paths start here.
KONKLAWE_ROOT = Path(__file__).resolve().parents[3]

OPTION_TYPES: dict[str, type] = {
    "fixture": str,
    "fixture_resume": str,
    "replay_of": str,
    "delay_ms": int,
    "exit_code": int,
    "hang": bool,
    "stderr": str,
    "report_env": list,
    "spawn_child": bool,
    "ignore_sigterm": bool,
}
DEFAULT_REPLAY_OF = "claude"
DEFAULT_DELAY_MS = 20


def resolve_fixture(path: str) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else KONKLAWE_ROOT / candidate


class FakeAdapter(ProviderAdapter):
    name = "fake"

    def validate_agent(self, spec: AgentSpec) -> list[str]:
        from konklawe.adapters import available_providers

        options = spec.provider_options
        problems = [
            f"unknown provider option '{key}' for fake"
            for key in sorted(set(options) - set(OPTION_TYPES))
        ]
        for key, expected in OPTION_TYPES.items():
            value = options.get(key)
            # bool is a subclass of int, so `delay_ms: true` must be rejected explicitly.
            if value is not None and (
                not isinstance(value, expected) or (expected is int and isinstance(value, bool))
            ):
                problems.append(f"provider option '{key}' must be of type {expected.__name__}")
        if "fixture" not in options:
            problems.append("provider option 'fixture' is required")
        for key in ("fixture", "fixture_resume"):
            value = options.get(key)
            if isinstance(value, str) and not resolve_fixture(value).is_file():
                problems.append(f"{key} not found: {resolve_fixture(value)}")
        delay = options.get("delay_ms")
        if isinstance(delay, int) and delay < 0:
            problems.append("provider option 'delay_ms' must not be negative")
        replay_of = options.get("replay_of", DEFAULT_REPLAY_OF)
        if replay_of == self.name or replay_of not in available_providers():
            problems.append(f"replay_of must name another provider, got '{replay_of}'")
        return problems

    def build_command(self, req: TurnRequest) -> CommandSpec:
        options = req.agent.provider_options
        fixture = options["fixture"]
        if req.resume_id and options.get("fixture_resume"):
            fixture = options["fixture_resume"]
        argv = [
            sys.executable,
            "-m",
            "konklawe.fake_cli",
            "--fixture",
            str(resolve_fixture(fixture)),
            "--delay-ms",
            str(options.get("delay_ms", DEFAULT_DELAY_MS)),
        ]
        if options.get("exit_code") is not None:
            argv += ["--exit-code", str(options["exit_code"])]
        if options.get("stderr"):
            argv += ["--stderr", options["stderr"]]
        if options.get("hang"):
            argv.append("--hang")
        if options.get("report_env"):
            argv += ["--report-env", *options["report_env"]]
        if options.get("spawn_child"):
            argv.append("--spawn-child")
        if options.get("ignore_sigterm"):
            argv.append("--ignore-sigterm")
        return CommandSpec(argv=argv, stdin_text=req.prompt)

    def new_parser(self, agent: AgentSpec) -> TurnParser:
        """The parser of the provider whose recording is replayed (`replay_of`)."""
        from konklawe.adapters import get_adapter

        replay_of = agent.provider_options.get("replay_of", DEFAULT_REPLAY_OF)
        return get_adapter(replay_of).new_parser(agent)
