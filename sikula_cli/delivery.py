"""Delivery-plan CLI commands."""

from __future__ import annotations

import argparse
import json
import sys


def register_parser(subparsers) -> argparse.ArgumentParser:
    delivery_p = subparsers.add_parser("delivery", help="Inspect and run delivery plans")
    delivery_sub = delivery_p.add_subparsers(dest="delivery_command")

    delivery_check_p = delivery_sub.add_parser("check", help="Check a delivery plan file")
    delivery_check_p.add_argument("plan_file", metavar="PLAN_FILE", help="Path to .sikula/delivery/*/plan.yaml")
    delivery_check_p.add_argument("--json", action="store_true", default=False, help="Print structured JSON output")

    delivery_status_p = delivery_sub.add_parser("status", help="Show delivery plan progress")
    delivery_status_p.add_argument("plan_file", metavar="PLAN_FILE", help="Path to .sikula/delivery/*/plan.yaml")
    delivery_status_p.add_argument("--json", action="store_true", default=False, help="Print structured JSON output")

    return delivery_p


def cmd_delivery_check(args: argparse.Namespace, cfg: dict) -> None:
    from core.delivery_plan import check_delivery_plan_file, render_delivery_plan_check

    result = check_delivery_plan_file(args.plan_file)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(render_delivery_plan_check(result), end="")
    if not result.valid:
        sys.exit(1)


def cmd_delivery_status(args: argparse.Namespace, cfg: dict) -> None:
    from core.delivery_progress import get_delivery_status, render_delivery_status

    result = get_delivery_status(args.plan_file)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(render_delivery_status(result), end="")
    if not result.valid:
        sys.exit(1)
