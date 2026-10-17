"""Project-independent runtime version output for humans and CLI consumers."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import re
import sys


_SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


class RuntimeVersionAction(argparse.Action):
    """Keep --version's eager exit, but resolve metadata only when requested."""

    def __init__(self, option_strings, dest, *, version_getter: Callable[[], str], **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)
        self.version_getter = version_getter

    def __call__(self, parser, namespace, values, option_string=None):
        formatter = parser._get_formatter()
        formatter.add_text(f"%(prog)s {self.version_getter()}")
        parser._print_message(formatter.format_help(), sys.stdout)
        parser.exit()


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("version", help="Report the invoked Sikula runtime version without project setup")
    parser.add_argument("--json", action="store_true", help="Emit the sikula.cli.v1 version object")


def cmd_version(args: argparse.Namespace, *, version_getter: Callable[[], str]) -> None:
    try:
        version = version_getter()
        if not isinstance(version, str) or not 5 <= len(version) <= 64 or _SEMVER.fullmatch(version) is None:
            raise ValueError("Invalid runtime version")
    except Exception:
        # Metadata errors may contain installation paths or other private details.
        print("Cannot report Sikula version: runtime version is unavailable or invalid.", file=sys.stderr)
        raise SystemExit(1) from None

    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": "sikula.cli.v1",
                    "sikula_version": version,
                    "command": "sikula version",
                    "status": "done",
                    "next_action": "none",
                }
            )
        )
    else:
        print(f"sikula {version}")
