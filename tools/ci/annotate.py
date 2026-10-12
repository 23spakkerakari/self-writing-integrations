"""Turn CI failures into GitHub workflow annotations, which anyone can read through the
check-runs API and the pull request view without downloading the job log.

    python3 tools/ci/annotate.py junit pytest-report.xml       # one error per failed test
    python3 tools/ci/annotate.py tail step.log --title "pip-audit" [--lines 60]
    python3 tools/ci/annotate.py gitleaks gitleaks.json        # rule, file, line; never the secret

Messages are escaped as workflow commands require (``%``, CR, LF) and cut at 4,000 characters.
The data CI produces is synthetic (simulator output, test fixtures) and the product's own logs
are redacted, so the tail of a step's output is safe to show. Standard library only, except
the ``junit`` mode, which parses with ``defusedxml`` and so runs under ``uv run`` in the jobs
that have the workspace installed. Always exits 0 so it never hides the step's own failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_MESSAGE = 4000
MAX_ANNOTATIONS = 40


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _property(text: str) -> str:
    return _escape(text).replace(":", "%3A").replace(",", "%2C")


def _emit(message: str, *, title: str, file: str | None = None, line: int | None = None) -> None:
    where = ""
    if file:
        where = f"file={_property(file)},"
        if line:
            where += f"line={line},"
    body = message[-MAX_MESSAGE:]
    print(f"::error {where}title={_property(title)}::{_escape(body)}")


def junit(path: Path) -> None:
    if not path.is_file():
        _emit(f"{path} was not written (collection error?)", title="pytest")
        return
    from defusedxml.ElementTree import parse  # noqa: PLC0415 - only the junit mode needs it

    root = parse(path).getroot()
    emitted = 0
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is None:
                continue
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            text = (node.get("message") or "") + "\n" + (node.text or "")
            file = case.get("file")
            line = case.get("line")
            _emit(
                text.strip(),
                title=f"{kind}: {name}"[:200],
                file=file,
                line=int(line) + 1 if line and line.isdigit() else None,
            )
            emitted += 1
            if emitted >= MAX_ANNOTATIONS:
                return


def tail(path: Path, title: str, lines: int) -> None:
    if not path.is_file():
        _emit(f"{path} was not written", title=title)
        return
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    _emit("\n".join(text[-lines:]), title=title)


def gitleaks(path: Path) -> None:
    if not path.is_file():
        _emit(f"{path} was not written", title="gitleaks")
        return
    findings = json.loads(path.read_text(encoding="utf-8") or "[]")
    for finding in findings[:MAX_ANNOTATIONS]:
        _emit(
            f"rule {finding.get('RuleID')} in commit {str(finding.get('Commit', ''))[:12]}",
            title="gitleaks finding (value redacted)",
            file=finding.get("File"),
            line=finding.get("StartLine"),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("junit")
    one.add_argument("path", type=Path)
    two = commands.add_parser("tail")
    two.add_argument("path", type=Path)
    two.add_argument("--title", required=True)
    two.add_argument("--lines", type=int, default=60)
    three = commands.add_parser("gitleaks")
    three.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "junit":
            junit(args.path)
        elif args.command == "tail":
            tail(args.path, args.title, args.lines)
        else:
            gitleaks(args.path)
    except Exception as exc:  # never hide the real failure behind ours
        print(f"annotate: {type(exc).__name__}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
