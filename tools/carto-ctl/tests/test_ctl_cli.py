"""carto-ctl command tree (spec Section 20): stubs refuse with exit 2 and name their milestone,
``version`` works, ``--help`` lists every command with its spec section, and argparse's
SystemExit never escapes ``main``."""

from __future__ import annotations

import argparse
import dataclasses
import io
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from carto_ctl import __version__
from carto_ctl.cli import build_parser, main, run
from carto_ctl.registry import (
    COMMANDS,
    EXIT_NOT_IMPLEMENTED,
    EXIT_OK,
    EXIT_USAGE,
    GROUPS,
    RESERVED_DESTS,
    Command,
    Invocation,
    stub,
)

STUBS = [command for command in COMMANDS if command.handler is stub]
GROUP_NAMES = sorted(GROUPS)


def _name(command: Command) -> str:
    return command.name


@pytest.fixture
def wide_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop argparse wrapping help lines so '(spec 14.4, M1)' stays on one line."""
    monkeypatch.setenv("COLUMNS", "200")


# --- the registry matches the task ------------------------------------------------------------


def test_registry_has_exactly_the_specified_commands() -> None:
    expected = {
        ("pki", "init"): ("M1", "14.4"),
        ("key", "init"): ("M1", "8.4"),
        ("audit", "verify"): ("M6", "14.9"),
        ("support-bundle",): ("M6", "16"),
        ("drill", "restore"): ("M6", "14.11"),
        ("verify-signatures",): ("M6", "14.8"),
        ("version",): ("M0", "20"),
    }
    assert {command.path: (command.milestone, command.section) for command in COMMANDS} == expected


def test_m1_implements_pki_init_key_init_and_version() -> None:
    implemented = [command.name for command in COMMANDS if command.implemented]
    assert implemented == ["pki init", "key init", "version"]
    assert len(STUBS) == len(COMMANDS) - 3


# --- stubs ------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", STUBS, ids=_name)
def test_stub_returns_2_and_names_milestone_and_section(
    command: Command, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(list(command.path)) == EXIT_NOT_IMPLEMENTED
    assert EXIT_NOT_IMPLEMENTED != EXIT_OK
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1, captured.out
    (line,) = lines
    assert line.startswith(f"carto-ctl {command.name}:")
    assert command.milestone in line
    assert f"spec {command.section}" in line
    assert captured.err == ""


def test_stub_writes_to_the_invocation_stream_only(capsys: pytest.CaptureFixture[str]) -> None:
    out, err = io.StringIO(), io.StringIO()
    command = next(command for command in COMMANDS if command.path == ("drill", "restore"))
    code = stub(Invocation(command, argparse.Namespace(), out, err))
    assert code == EXIT_NOT_IMPLEMENTED
    assert out.getvalue() == (
        "carto-ctl drill restore: not implemented in this build; arrives in M6 (spec 14.11).\n"
    )
    assert err.getvalue() == ""
    assert capsys.readouterr() == ("", "")


# --- version ----------------------------------------------------------------------------------


def test_version_command_prints_version_and_returns_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == EXIT_OK
    assert capsys.readouterr().out == "carto-ctl 0.1.0\n"


def test_version_flag_prints_version_and_returns_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == EXIT_OK
    assert capsys.readouterr().out.rstrip("\r\n") == "carto-ctl 0.1.0"


def test_package_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as handle:
        assert tomllib.load(handle)["project"]["version"] == __version__ == "0.1.0"


# --- help -------------------------------------------------------------------------------------


@pytest.mark.usefixtures("wide_terminal")
def test_root_help_lists_every_command_with_its_section(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--help"]) == EXIT_OK
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("usage: carto-ctl ")
    for command in COMMANDS:
        assert f"  {command.name} " in captured.out
        assert f"{command.help} (spec {command.section}, {command.milestone})" in captured.out
    for group, group_help in GROUPS.items():
        assert f"    {group} " in captured.out
        assert group_help in captured.out


@pytest.mark.usefixtures("wide_terminal")
def test_h_is_the_same_as_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-h"]) == EXIT_OK
    short = capsys.readouterr().out
    assert main(["--help"]) == EXIT_OK
    assert short == capsys.readouterr().out


@pytest.mark.usefixtures("wide_terminal")
@pytest.mark.parametrize("group", GROUP_NAMES)
def test_group_help_lists_its_subcommands_with_sections(
    group: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([group, "--help"]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith(f"usage: carto-ctl {group} ")
    members = [command for command in COMMANDS if command.path[0] == group]
    assert members, group
    for command in members:
        assert f"    {command.path[1]} " in out
        assert f"(spec {command.section}, {command.milestone})" in out


@pytest.mark.usefixtures("wide_terminal")
@pytest.mark.parametrize("command", COMMANDS, ids=_name)
def test_leaf_help_cites_section_and_milestone(
    command: Command, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*command.path, "--help"]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith(f"usage: carto-ctl {command.name} ")
    assert f"(spec {command.section})" in out
    if command.implemented:
        assert "Arrives in" not in out
    else:
        assert f"Arrives in {command.milestone}" in out
        assert f"exits with status {EXIT_NOT_IMPLEMENTED}" in out


# --- usage errors never raise and never return 0 ------------------------------------------------


def test_unknown_command_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["frobnicate"]) == EXIT_USAGE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid choice: 'frobnicate'" in captured.err


@pytest.mark.parametrize("group", GROUP_NAMES)
def test_unknown_subcommand_returns_2(group: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([group, "frobnicate"]) == EXIT_USAGE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid choice: 'frobnicate'" in captured.err


def test_unknown_option_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version", "--bogus"]) == EXIT_USAGE
    assert "unrecognized arguments: --bogus" in capsys.readouterr().err


ABBREVIATED = [
    ["--vers"],
    ["--he"],
    *([group, "--he"] for group in GROUP_NAMES),
    *([*command.path, "--he"] for command in COMMANDS),
]


@pytest.mark.parametrize("argv", ABBREVIATED, ids=" ".join)
def test_abbreviated_option_is_not_accepted_at_any_level(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == EXIT_USAGE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"unrecognized arguments: {argv[-1]}" in captured.err


def test_abbreviated_hook_option_is_not_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    def configure(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--since", default="the beginning")

    def verify(invocation: Invocation) -> int:
        invocation.out.write(f"since {invocation.args.since}\n")
        return EXIT_OK

    command = Command(("audit", "verify"), "Verify", "M6", "14.9", verify, configure)
    assert run(["audit", "verify", "--si", "2026-10-01"], (command,)) == EXIT_USAGE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unrecognized arguments: --si" in captured.err


@pytest.mark.usefixtures("wide_terminal")
def test_no_command_prints_root_help_and_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_USAGE
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("usage: carto-ctl ")
    for command in COMMANDS:
        assert f"  {command.name} " in captured.out


@pytest.mark.parametrize("group", GROUP_NAMES)
def test_group_without_subcommand_prints_group_help_and_returns_2(
    group: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([group]) == EXIT_USAGE
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith(f"usage: carto-ctl {group} ")
    assert "subcommands:" in captured.out


def test_main_reads_sys_argv_when_argv_is_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["carto-ctl", "version"])
    assert main() == EXIT_OK
    assert capsys.readouterr().out == "carto-ctl 0.1.0\n"
    monkeypatch.setattr(sys, "argv", ["carto-ctl", "audit", "verify"])
    assert main() == EXIT_NOT_IMPLEMENTED
    assert "M6" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv", [["--help"], ["--version"], [], ["nope"], ["pki"], ["pki", "--help"], ["-x"]]
)
def test_main_never_raises_system_exit(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    try:
        code = main(argv)
    except SystemExit as exc:
        pytest.fail(f"main({argv!r}) raised SystemExit({exc.code!r})")
    assert code in {EXIT_OK, EXIT_USAGE}
    capsys.readouterr()


# --- the process exit code is what scripts see --------------------------------------------------


def _run_module(*argv: str) -> subprocess.CompletedProcess[str]:
    # The interpreter is this test run's own; the arguments are literals from this file.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "carto_ctl", *argv],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_python_m_carto_ctl_version_exits_0() -> None:
    result = _run_module("version")
    assert result.returncode == EXIT_OK, result.stderr
    assert result.stdout.rstrip("\r\n") == "carto-ctl 0.1.0"


def test_python_m_carto_ctl_stub_exits_2() -> None:
    result = _run_module("support-bundle")
    assert result.returncode == EXIT_NOT_IMPLEMENTED, result.stderr
    assert "arrives in M6 (spec 16)" in result.stdout


# --- a real implementation replaces a stub without touching the parser --------------------------


def test_replacing_a_handler_changes_nothing_but_the_handler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []

    def verify(invocation: Invocation) -> int:
        seen.append(invocation.command.name)
        invocation.out.write("chain intact\n")
        return EXIT_OK

    commands = tuple(
        dataclasses.replace(command, handler=verify)
        if command.path == ("audit", "verify")
        else command
        for command in COMMANDS
    )
    assert run(["audit", "verify"], commands) == EXIT_OK
    assert seen == ["audit verify"]
    assert capsys.readouterr().out == "chain intact\n"
    # Every other entry still behaves as before.
    assert run(["drill", "restore"], commands) == EXIT_NOT_IMPLEMENTED
    assert "M6" in capsys.readouterr().out


def test_configure_hook_adds_options_for_the_handler(capsys: pytest.CaptureFixture[str]) -> None:
    def configure(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--since", required=True)

    def verify(invocation: Invocation) -> int:
        invocation.out.write(f"since {invocation.args.since}\n")
        return EXIT_OK

    command = Command(("audit", "verify"), "Verify", "M6", "14.9", verify, configure)
    assert run(["audit", "verify", "--since", "2026-10-01"], (command,)) == EXIT_OK
    assert capsys.readouterr().out == "since 2026-10-01\n"
    assert run(["audit", "verify"], (command,)) == EXIT_USAGE
    assert "--since" in capsys.readouterr().err


def test_reserved_dests_are_the_keys_the_parser_sets_on_every_command() -> None:
    assert sorted(RESERVED_DESTS) == ["command", "help_parser"]
    for command in COMMANDS:
        args = build_parser().parse_args(list(command.path))
        dests = set(vars(args))
        assert dests >= RESERVED_DESTS, command.name
        if command.configure is None:
            assert dests == RESERVED_DESTS, command.name


@pytest.mark.parametrize("dest", sorted(RESERVED_DESTS))
def test_configure_hook_may_not_add_an_option_with_a_reserved_dest(dest: str) -> None:
    def configure(parser: argparse.ArgumentParser) -> None:
        # An argument group is the least direct route; the check must see through it.
        parser.add_argument_group("extra").add_argument("--mine", dest=dest)

    command = Command(("audit", "verify"), "Verify", "M6", "14.9", stub, configure)
    with pytest.raises(ValueError, match=rf"'audit verify'.*reserved dest.*'{dest}'"):
        build_parser((command,))


def test_configure_hook_may_not_replace_a_reserved_default() -> None:
    def configure(parser: argparse.ArgumentParser) -> None:
        parser.set_defaults(command=None)

    command = Command(("audit", "verify"), "Verify", "M6", "14.9", stub, configure)
    with pytest.raises(ValueError, match=r"'audit verify'.*reserved default"):
        build_parser((command,))
