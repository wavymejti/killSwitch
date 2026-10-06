import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from konklawe.adapters import get_adapter
from konklawe.adapters.base import TurnRequest
from konklawe.adapters.claude import ClaudeTurnParser
from konklawe.adapters.fake import KONKLAWE_ROOT, FakeAdapter
from konklawe.agents import AgentSpec

FIXTURE = "tests/fixtures/claude/simple_text.jsonl"
RESUME_FIXTURE = "tests/fixtures/claude/resume_turn2.jsonl"


def spec(**options: Any) -> AgentSpec:
    return AgentSpec(
        name="echo",
        description="d",
        provider="fake",
        role_prompt="unused",
        source_path=Path("agents/echo.md"),
        provider_options={"fixture": FIXTURE, **options},
    )


def request(agent: AgentSpec, resume_id: str | None = None) -> TurnRequest:
    return TurnRequest(agent=agent, prompt="hi", workdir=Path("/w"), resume_id=resume_id)


def test_valid_agent() -> None:
    agent = spec(fixture_resume=RESUME_FIXTURE, delay_ms=0, exit_code=0, hang=False)

    assert FakeAdapter().validate_agent(agent) == []


@pytest.mark.parametrize(
    ("options", "problem"),
    [
        ({"fixture": "tests/fixtures/claude/missing.jsonl"}, "fixture not found"),
        ({"fixture_resume": "nope.jsonl"}, "fixture_resume not found"),
        ({"speed": 2}, "unknown provider option 'speed'"),
        ({"delay_ms": "fast"}, "'delay_ms' must be of type int"),
        ({"delay_ms": True}, "'delay_ms' must be of type int"),
        ({"delay_ms": -1}, "'delay_ms' must not be negative"),
        ({"replay_of": "fake"}, "replay_of must name another provider"),
        ({"replay_of": "gemini"}, "replay_of must name another provider"),
    ],
)
def test_invalid_agent(options: dict[str, Any], problem: str) -> None:
    problems = FakeAdapter().validate_agent(spec(**options))

    assert len(problems) == 1, problems
    assert problem in problems[0]


def test_fixture_is_required() -> None:
    agent = spec().model_copy(update={"provider_options": {}})

    assert FakeAdapter().validate_agent(agent) == ["provider option 'fixture' is required"]


def test_command_replays_fixture_through_fake_cli() -> None:
    command = FakeAdapter().build_command(request(spec(delay_ms=5, exit_code=3, hang=True)))

    assert command.argv == [
        sys.executable,
        "-m",
        "konklawe.fake_cli",
        "--fixture",
        str(KONKLAWE_ROOT / FIXTURE),
        "--delay-ms",
        "5",
        "--exit-code",
        "3",
        "--hang",
    ]
    assert command.stdin_text == "hi"


def test_resume_uses_resume_fixture() -> None:
    agent = spec(fixture_resume=RESUME_FIXTURE)

    first = FakeAdapter().build_command(request(agent)).argv
    resumed = FakeAdapter().build_command(request(agent, resume_id="abc")).argv
    without_resume_fixture = FakeAdapter().build_command(request(spec(), resume_id="abc")).argv

    assert str(KONKLAWE_ROOT / FIXTURE) in first
    assert str(KONKLAWE_ROOT / RESUME_FIXTURE) in resumed
    assert str(KONKLAWE_ROOT / FIXTURE) in without_resume_fixture


def test_parser_comes_from_replayed_provider() -> None:
    assert isinstance(get_adapter("fake").new_parser(spec()), ClaudeTurnParser)


def test_fake_cli_replays_lines_and_reports_env() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "konklawe.fake_cli",
            "--fixture",
            str(KONKLAWE_ROOT / RESUME_FIXTURE),
            "--report-env",
            "PATH",
            "KONKLAWE_SURELY_UNSET",
            "--stderr",
            "warning: test",
            "--exit-code",
            "4",
        ],
        input="prompt",
        capture_output=True,
        text=True,
        timeout=30,
    )

    lines = result.stdout.splitlines()
    assert json.loads(lines[0]) == {
        "type": "fake_env",
        "set": {"PATH": True, "KONKLAWE_SURELY_UNSET": False},
    }
    assert lines[1:] == (KONKLAWE_ROOT / RESUME_FIXTURE).read_text(encoding="utf-8").splitlines()
    assert result.stderr == "warning: test\n"
    assert result.returncode == 4
