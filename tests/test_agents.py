from pathlib import Path

import pytest

from konklawe.adapters.base import ProviderAdapter
from konklawe.agents import (
    AgentLoadError,
    AgentSpec,
    load_agent,
    load_agents,
    scan_agents,
    split_tool_list,
)


class StubAdapter(ProviderAdapter):
    name = "stub"

    def validate_agent(self, spec: AgentSpec) -> list[str]:
        return ["option 'bad' is not supported"] if "bad" in spec.provider_options else []


ADAPTERS = {"stub": StubAdapter()}

VALID = """\
---
name: recenzent
description: Szuka błędów i ryzyk
provider: stub
model: sonnet
tools: Read, Grep, Glob
disallowed_tools: [Edit, Write, Bash]
permission_mode: default
provider_options: {}
---
Jesteś recenzentem.

Szukasz problemów.
"""


def write_agent(directory: Path, name: str, text: str) -> Path:
    path = directory / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def agent_text(name: str = "a", provider: str = "stub", **fields: str) -> str:
    extra = "".join(f"{key}: {value}\n" for key, value in fields.items())
    return f"---\nname: {name}\ndescription: test\nprovider: {provider}\n{extra}---\nRole.\n"


def errors_of(path: Path) -> list[str]:
    with pytest.raises(AgentLoadError) as info:
        load_agent(path, ADAPTERS)
    assert all(p == path for p, _ in info.value.errors)
    return [message for _, message in info.value.errors]


def test_valid_file(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "recenzent", VALID)

    spec = load_agent(path, ADAPTERS)

    assert spec.name == "recenzent"
    assert spec.description == "Szuka błędów i ryzyk"
    assert spec.provider == "stub"
    assert spec.model == "sonnet"
    assert spec.tools == ["Read", "Grep", "Glob"]
    assert spec.disallowed_tools == ["Edit", "Write", "Bash"]
    assert spec.permission_mode == "default"
    assert spec.provider_options == {}
    assert spec.role_prompt == "Jesteś recenzentem.\n\nSzukasz problemów."
    assert spec.source_path == path


def test_optional_fields_default(tmp_path: Path) -> None:
    spec = load_agent(write_agent(tmp_path, "a", agent_text(provider_options="")), ADAPTERS)

    assert spec.model is None
    assert spec.tools == []
    assert spec.disallowed_tools == []
    assert spec.permission_mode is None
    assert spec.provider_options == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Read, Grep,Glob", ["Read", "Grep", "Glob"]),
        ("[Read, Grep]", ["Read", "Grep"]),
        ('"Bash(git diff:*, git log:*), Read"', ["Bash(git diff:*, git log:*)", "Read"]),
        ('["Bash(git log:*)", " Edit "]', ["Bash(git log:*)", "Edit"]),
    ],
)
def test_tools_as_string_or_list(tmp_path: Path, value: str, expected: list[str]) -> None:
    spec = load_agent(write_agent(tmp_path, "a", agent_text(tools=value)), ADAPTERS)

    assert spec.tools == expected


def test_split_tool_list_ignores_empty_items() -> None:
    assert split_tool_list(" Read,, Grep ,") == ["Read", "Grep"]


def test_missing_header(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "a", "name: a\n")

    assert errors_of(path) == ["file must start with a '---' line (YAML header)"]


def test_unclosed_header(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "a", "---\nname: a\n")

    assert errors_of(path) == ["YAML header is not closed with a '---' line"]


def test_header_must_be_mapping(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "a", "---\n- a\n- b\n---\nRole.\n")

    assert errors_of(path) == ["YAML header must be a mapping of fields"]


def test_invalid_yaml(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "a", "---\nname: [a\n---\nRole.\n")

    [message] = errors_of(path)
    assert message.startswith("invalid YAML header:")


def test_unknown_field(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "a", agent_text(colour="red"))

    assert errors_of(path) == ["unknown field 'colour'"]


def test_loader_fields_cannot_be_set_in_header(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "a", agent_text(role_prompt="x"))

    assert errors_of(path) == ["unknown field 'role_prompt'"]


def test_missing_required_fields(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "a", "---\nname: a\n---\nRole.\n")

    assert errors_of(path) == [
        "missing required field 'description'",
        "missing required field 'provider'",
    ]


def test_empty_role_prompt(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "a", "---\nname: a\ndescription: d\nprovider: stub\n---\n  \n")

    assert errors_of(path) == ["role prompt (text below the header) must not be empty"]


def test_name_must_match_file_name(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "architekt", agent_text(name="recenzent"))

    assert errors_of(path) == ["name 'recenzent' does not match file name 'architekt'"]


def test_name_must_be_slug(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "Zly_Agent", agent_text(name="Zly_Agent"))

    [message] = errors_of(path)
    assert message.startswith("invalid field 'name'")


def test_unknown_provider(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "a", agent_text(provider="nope"))

    assert errors_of(path) == ["unknown provider 'nope' (available: stub)"]


def test_adapter_validation_errors_are_included(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "a", agent_text(provider_options="{bad: 1}"))

    assert errors_of(path) == ["option 'bad' is not supported"]


def test_name_and_adapter_errors_reported_together(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "b", agent_text(name="a", provider_options="{bad: 1}"))

    assert errors_of(path) == [
        "name 'a' does not match file name 'b'",
        "option 'bad' is not supported",
    ]


def test_load_agents(tmp_path: Path) -> None:
    write_agent(tmp_path, "recenzent", VALID)
    write_agent(tmp_path, "a", agent_text())
    (tmp_path / "notes.txt").write_text("not an agent", encoding="utf-8")

    agents = load_agents(tmp_path, ADAPTERS)

    assert sorted(agents) == ["a", "recenzent"]


def test_errors_from_all_files_reported_at_once(tmp_path: Path) -> None:
    write_agent(tmp_path, "good", agent_text(name="good"))
    bad_header = write_agent(tmp_path, "no-header", "just text\n")
    bad_provider = write_agent(tmp_path, "nope", agent_text(name="nope", provider="nope"))
    bad_field = write_agent(tmp_path, "typo", agent_text(name="typo", modle="x"))

    with pytest.raises(AgentLoadError) as info:
        load_agents(tmp_path, ADAPTERS)

    assert {path for path, _ in info.value.errors} == {bad_header, bad_provider, bad_field}
    assert str(bad_header) in str(info.value)

    scan = scan_agents(tmp_path, ADAPTERS)
    assert list(scan.agents) == ["good"]
    assert len(scan.errors) == 3


def test_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(AgentLoadError, match="agents directory does not exist"):
        load_agents(tmp_path / "missing", ADAPTERS)
