import json
import re
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner, Result

from konklawe import __version__, cli
from konklawe.cli import app

FIXTURES = Path(__file__).parent / "fixtures" / "claude"

runner = CliRunner()


def fake_agent_file(name: str, **options: object) -> str:
    opts = {
        "fixture": str(FIXTURES / "simple_text.jsonl"),
        "fixture_resume": str(FIXTURES / "resume_turn2.jsonl"),
        "delay_ms": 0,
        **options,
    }
    lines = "".join(f"  {key}: {json.dumps(value)}\n" for key, value in opts.items())
    return (
        f"---\nname: {name}\ndescription: Test agent {name}\nprovider: fake\n"
        f"provider_options:\n{lines}---\nRole of {name}.\n"
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A konklawe directory with fake agents; the test runs from inside it."""
    (tmp_path / "konklawe.toml").write_text('agents_dir = "agents"\ndata_dir = "data"\n')
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "echo.md").write_text(fake_agent_file("echo"))
    (agents / "other.md").write_text(
        fake_agent_file("other", fixture=str(FIXTURES / "tool_use.jsonl"))
    )
    monkeypatch.chdir(tmp_path)
    plain = Console(color_system=None, highlight=False, width=300)
    monkeypatch.setattr(cli, "console", plain)
    monkeypatch.setattr(cli, "err_console", Console(stderr=True, color_system=None, width=300))
    return tmp_path


def invoke(*args: str, input: str | None = None) -> Result:
    return runner.invoke(app, list(args), input=input)


def session_id(result: Result) -> str:
    match = re.search(r"session: ([0-9a-f]{12})", result.output)
    assert match, result.output
    return match.group(1)


def test_help() -> None:
    result = invoke("--help")

    assert result.exit_code == 0
    assert "Usage" in result.output


def test_version() -> None:
    result = invoke("--version")

    assert result.exit_code == 0
    assert __version__ in result.output


def test_agents_list(project: Path) -> None:
    result = invoke("agents", "list")

    assert result.exit_code == 0, result.output
    assert "echo" in result.output and "Test agent other" in result.output


def test_agents_list_reports_broken_file(project: Path) -> None:
    (project / "agents" / "broken.md").write_text("no header here\n")

    result = invoke("agents", "list")

    assert result.exit_code == 1
    assert "echo" in result.output
    assert "agents/broken.md: file must start with a '---' line" in result.output


def test_agents_show_prints_dry_run_command(project: Path) -> None:
    result = invoke("agents", "show", "echo")

    assert result.exit_code == 0, result.output
    assert "Role of echo." in result.output
    assert "-m konklawe.fake_cli --fixture" in result.output
    assert "stdin: <prompt>" in result.output


def test_run_and_continue_session(project: Path) -> None:
    first = invoke("run", "echo", "test")

    assert first.exit_code == 0, first.output
    assert f"workdir: {project.resolve()}" in first.output
    assert "[echo] SQLite to lekka" in first.output
    assert "(API estimate, not a charge)" in first.output

    second = invoke("run", "echo", "what number?", "--session", session_id(first))

    assert second.exit_code == 0, second.output
    assert "[echo] 42" in second.output
    assert session_id(second) == session_id(first)


def test_run_rejects_session_of_another_agent(project: Path) -> None:
    first = invoke("run", "echo", "test")

    result = invoke("run", "other", "x", "--session", session_id(first))

    assert result.exit_code == 2
    assert "belongs to agent 'echo'" in result.output


def test_run_unknown_agent(project: Path) -> None:
    result = invoke("run", "nobody", "hi")

    assert result.exit_code == 2
    assert "unknown agent 'nobody' (known: echo, other)" in result.output


def test_run_with_broken_agent_file_shows_its_errors(project: Path) -> None:
    (project / "agents" / "bad.md").write_text("---\nname: bad\n---\nRole.\n")

    result = invoke("run", "bad", "hi")

    assert result.exit_code == 2
    assert "agents/bad.md: missing required field 'description'" in result.output


def test_failed_turn_sets_exit_code(project: Path) -> None:
    init_only = project / "init.jsonl"
    init_only.write_text((FIXTURES / "simple_text.jsonl").read_text().splitlines()[0] + "\n")
    (project / "agents" / "crash.md").write_text(
        fake_agent_file("crash", fixture=str(init_only), exit_code=3, stderr="boom")
    )

    result = invoke("run", "crash", "hi")

    assert result.exit_code == 1
    assert "✗ error: claude exited with code 3 without a result" in result.output
    assert "■ failed" in result.output


def test_run_many(project: Path) -> None:
    result = invoke("run-many", "--task", "echo=a", "--task", "other=b")

    assert result.exit_code == 0, result.output
    assert "[echo] SQLite to lekka" in result.output
    assert "[other] ▸ Read" in result.output
    assert len(re.findall(r"session: [0-9a-f]{12}", result.output)) == 2


def test_run_many_labels_repeated_agents(project: Path) -> None:
    result = invoke("run-many", "-t", "echo=a", "-t", "echo=b")

    assert result.exit_code == 0, result.output
    assert len(set(re.findall(r"\[echo:([0-9a-f]{4})\]", result.output))) == 2


def test_run_many_rejects_malformed_task(project: Path) -> None:
    result = invoke("run-many", "--task", "echo")

    assert result.exit_code == 2
    assert "--task expects AGENT=PROMPT" in result.output


def test_sessions_list_and_log(project: Path) -> None:
    live = invoke("run", "echo", "test")
    sid = session_id(live)

    listing = invoke("sessions", "list")
    replay = invoke("log", sid)

    assert listing.exit_code == 0
    assert sid in listing.output and "idle" in listing.output
    assert replay.exit_code == 0, replay.output
    rendered = [line for line in live.output.splitlines() if line.startswith("[echo]")]
    assert [line for line in replay.output.splitlines() if line.startswith("[echo]")] == rendered


def test_log_unknown_session(project: Path) -> None:
    result = invoke("log", "000000000000")

    assert result.exit_code == 2
    assert "unknown session" in result.output


def test_chat_keeps_the_session(project: Path) -> None:
    result = invoke("chat", "echo", input="first\n\nsecond\n/exit\n")

    assert result.exit_code == 0, result.output
    assert "[echo] SQLite to lekka" in result.output
    assert "[echo] 42" in result.output


def test_chat_ends_on_eof(project: Path) -> None:
    result = invoke("chat", "echo", input="")

    assert result.exit_code == 0, result.output
    assert "session:" in result.output


def test_record(project: Path) -> None:
    out = project / "rec" / "turn.jsonl"

    result = invoke("record", "echo", "hello", "--out", str(out))

    assert result.exit_code == 0, result.output
    assert out.read_text() == (FIXTURES / "simple_text.jsonl").read_text()
    meta = json.loads((project / "rec" / "turn.jsonl.meta.json").read_text())
    assert meta["exit_code"] == 0
    assert meta["stdin"] == "hello"
    assert "konklawe.fake_cli" in meta["command"]
    assert meta["cwd"] == str(project.resolve())


def test_config_error_is_reported(project: Path) -> None:
    (project / "konklawe.toml").write_text("max_paralel_turns = 2\n")

    result = invoke("agents", "list")

    assert result.exit_code == 2
    assert "unknown key 'max_paralel_turns'" in result.output
