"""The carto-ctl command registry (spec Section 20): frozen entries, unique paths, groups with
help, and a parser builder that rejects what it cannot represent."""

from __future__ import annotations

import dataclasses
import re

import pytest

from carto_ctl.cli import build_parser
from carto_ctl.registry import COMMANDS, GROUPS, Command, stub

MILESTONE = re.compile(r"^M[0-6]$")
SECTION = re.compile(r"^\d{1,2}(\.\d{1,2})?$")
WORD = re.compile(r"^[a-z][a-z-]*[a-z]$")


def test_commands_are_frozen_dataclasses() -> None:
    for command in COMMANDS:
        assert dataclasses.is_dataclass(command)
        for field in dataclasses.fields(command):
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(command, field.name, None)


def test_registry_containers_are_read_only() -> None:
    assert isinstance(COMMANDS, tuple)
    with pytest.raises(TypeError):
        GROUPS["new"] = "help"  # type: ignore[index]


def test_paths_are_unique_and_well_formed() -> None:
    paths = [command.path for command in COMMANDS]
    assert len(paths) == len(set(paths))
    for path in paths:
        assert 1 <= len(path) <= 2
        for word in path:
            assert WORD.match(word), word


def test_no_command_is_a_prefix_of_another() -> None:
    paths = {command.path for command in COMMANDS}
    for path in paths:
        for depth in range(1, len(path)):
            assert path[:depth] not in paths, path


def test_every_group_has_help_and_every_help_has_a_group() -> None:
    groups = {command.path[0] for command in COMMANDS if len(command.path) > 1}
    assert groups == set(GROUPS)
    for group, group_help in GROUPS.items():
        assert "spec " in group_help, group
        assert group_help.isascii()


def test_milestones_sections_and_help_are_well_formed() -> None:
    for command in COMMANDS:
        assert MILESTONE.match(command.milestone), command.name
        assert SECTION.match(command.section), command.name
        assert command.help.isascii()
        assert command.help[0].isupper()
        assert not command.help.endswith(".")
        assert command.summary == f"{command.help} (spec {command.section}, {command.milestone})"
        assert command.name == " ".join(command.path)
        assert command.configure is None


def test_implemented_means_not_the_stub() -> None:
    command = Command(("x",), "X", "M0", "20", stub)
    assert not command.implemented
    assert dataclasses.replace(command, handler=lambda _invocation: 0).implemented


def test_build_parser_rejects_duplicate_commands() -> None:
    command = Command(("version",), "Twice", "M0", "20", stub)
    with pytest.raises(ValueError, match="duplicate command: 'version'"):
        build_parser((command, command))


def test_build_parser_rejects_a_command_that_is_also_a_group() -> None:
    commands = (
        Command(("pki",), "Group as leaf", "M1", "14.4", stub),
        Command(("pki", "init"), "Leaf", "M1", "14.4", stub),
    )
    with pytest.raises(ValueError, match="'pki' is both a command and a group"):
        build_parser(commands)


def test_build_parser_rejects_a_group_without_help() -> None:
    with pytest.raises(ValueError, match="command group 'mystery' has no help text"):
        build_parser((Command(("mystery", "thing"), "Leaf", "M6", "16", stub),))


@pytest.mark.parametrize("path", [(), ("",), ("pki", "")])
def test_build_parser_rejects_empty_paths(path: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="non-empty words"):
        build_parser((Command(path, "Leaf", "M6", "16", stub),))


def test_build_parser_names_the_first_unknown_group_in_registry_order() -> None:
    commands = tuple(
        Command((group, "thing"), "Leaf", "M6", "16", stub) for group in ("aaa", "bbb", "ccc")
    )
    with pytest.raises(ValueError, match="command group 'aaa' has no help text"):
        build_parser(commands)
    with pytest.raises(ValueError, match="command group 'ccc' has no help text"):
        build_parser(commands[::-1])


def test_build_parser_names_the_first_group_clash_in_registry_order() -> None:
    commands = (
        Command(("pki",), "Group as leaf", "M1", "14.4", stub),
        Command(("key",), "Group as leaf", "M1", "8.4", stub),
        Command(("pki", "init"), "Leaf", "M1", "14.4", stub),
        Command(("key", "init"), "Leaf", "M1", "8.4", stub),
    )
    with pytest.raises(ValueError, match="'pki' is both a command and a group"):
        build_parser(commands)
    with pytest.raises(ValueError, match="'key' is both a command and a group"):
        build_parser(commands[::-1])


def test_build_parser_accepts_the_shipped_registry() -> None:
    parser = build_parser()
    for command in COMMANDS:
        args = parser.parse_args(list(command.path))
        assert args.command is command
