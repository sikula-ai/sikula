"""Opt-in real-provider preparation smoke; never collected or run by CI."""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import yaml

REPOSITORY = Path(__file__).resolve().parents[1]
PLAN_ID = "inventory-orders"
PLAN_DIR = Path(".sikula/delivery") / PLAN_ID
CONTROL = Path(".sikula/state/delivery-preparation") / PLAN_ID / "control.json"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def create_fixture(directory: Path | None, provider: str, model: str, timeout: int) -> Path:
    if directory is None:
        root = Path(tempfile.mkdtemp(prefix="sikula-preparation-smoke-")).resolve()
    else:
        root = directory.resolve()
        root.mkdir(parents=True, exist_ok=False)
    for name in ("warehouse", "tests_proj", ".sikula/tasks", ".smoke"):
        (root / name).mkdir(parents=True)
    files = {
        "warehouse/__init__.py": '"""In-memory inventory and order library."""\n',
        "tests_proj/test_baseline.py": "import unittest\nimport warehouse\n\n"
        "class Baseline(unittest.TestCase):\n"
        "    def test_package_imports(self):\n"
        "        self.assertIsNotNone(warehouse)\n",
        "README.md": "# Inventory and orders\n\nPython standard library only. "
        "Production code belongs in warehouse/ and unittest tests in tests_proj/.\n"
        "Run `python -m unittest discover -s tests_proj -v` from the repository root.\n",
        ".gitignore": "__pycache__/\n*.pyc\n.sikula/state/\n.sikula/worktrees/\n.sikula/contract-reports/\n.smoke/\n",
        f".sikula/tasks/{PLAN_ID}.md": (REPOSITORY / "tests/fixtures/hierarchical-preparation-smoke/task.md").read_text(
            encoding="utf-8"
        ),
    }
    for name, contents in files.items():
        (root / name).write_bytes(contents.encode("utf-8"))
    cfg = {
        "project": {"name": PLAN_ID, "root_path": str(root), "build_tool": "python", "language": "Python"},
        "llm": {"provider": provider, "model": model, "agent_timeout": timeout},
        "sandbox": {
            "allowed_read_paths": ["."],
            "allowed_write_paths": ["warehouse/"],
            "allowed_test_write_paths": ["tests_proj/"],
        },
        "run_build": True,
        "run_tests": True,
        "run_review": True,
        "run_security_review": True,
        "run_test_writing": True,
        "run_checks": False,
        "build": {
            "compile_command": "python -m compileall -q warehouse/",
            "test_command": "python -m unittest discover -s tests_proj -v",
            "timeout": 60,
        },
        "guidelines": {"context_files": ["README.md"]},
    }
    (root / ".sikula/config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    write_json(root / ".smoke/fixture.json", {"fixture": "hierarchical-preparation", "version": 1})
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    return root


def check_fixture(root: Path) -> None:
    if json.loads((root / ".smoke/fixture.json").read_text(encoding="utf-8")) != {
        "fixture": "hierarchical-preparation",
        "version": 1,
    }:
        raise ValueError("Not a smoke fixture")


def control_calls(root: Path) -> list[dict]:
    path = root / CONTROL
    return json.loads(path.read_text(encoding="utf-8"))["calls"] if path.exists() else []


def artifact_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root / PLAN_DIR).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / PLAN_DIR).rglob("*"))
        if path.is_file()
    }


def audit_metrics(root: Path) -> dict:
    rows = []
    for path in sorted((root / ".sikula/contract-reports").glob("*.auto-llm.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)["record"]
            if isinstance(record.get("prompt"), str):
                rows.append(
                    {
                        "phase": record.get("phase"),
                        "stage": record.get("stage", "route_or_flat"),
                        "prompt_bytes": len(record["prompt"].encode("utf-8")),
                        "response_bytes": len((record.get("raw_output") or "").encode("utf-8")),
                    }
                )
    return {
        "audited_exchanges": len(rows),
        "exchanges_by_stage": dict(Counter(row["stage"] for row in rows)),
        "max_prompt_bytes": max((row["prompt_bytes"] for row in rows), default=0),
        "total_prompt_bytes": sum(row["prompt_bytes"] for row in rows),
        "exchanges": rows,
    }


def invoke_prepare(root: Path, label: str) -> tuple[int, dict, float]:
    command = [
        sys.executable,
        str(REPOSITORY / "sikula.py"),
        "delivery",
        "prepare",
        f".sikula/tasks/{PLAN_ID}.md",
        "--json",
    ]
    if (root / PLAN_DIR / "plan.yaml").exists():
        command.append("--force")
    started = time.monotonic()
    # Keep raw CLI/provider diagnostics private; print only the summary below.
    with (
        (root / f".smoke/{label}.stdout.json").open("wb") as out,
        (root / f".smoke/{label}.stderr.log").open("wb") as err,
    ):
        process = subprocess.run(command, cwd=root, stdout=out, stderr=err, check=False)
    elapsed = round(time.monotonic() - started, 2)
    try:
        result = json.loads((root / f".smoke/{label}.stdout.json").read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        result = {}
    return process.returncode, result, elapsed


def run_smoke(root: Path) -> dict:
    check_fixture(root)
    report = {"passed": False, "implementation_executed": False}
    code, result, seconds = invoke_prepare(root, "prepare")
    report.update(prepare_exit_code=code, prepare_seconds=seconds, metrics=audit_metrics(root))
    if code or not result.get("prepared") or not result.get("ready"):
        report["failure"] = "preparation_not_ready"
        report["prepare_error_codes"] = [issue["code"] for issue in result.get("errors", [])]
        return report
    before = control_calls(root)
    plan = yaml.safe_load((root / PLAN_DIR / "plan.yaml").read_text(encoding="utf-8"))
    report.update(units=len(plan["units"]), checkpoints=len(plan.get("checkpoints", [])))
    stages = report["metrics"]["exchanges_by_stage"]
    if (
        not any(call["status"] == "accepted" for call in before)
        or not plan.get("checkpoints")
        or not all(stages.get(stage) for stage in ("author", "local_review", "integration_review"))
    ):
        report["failure"] = "hierarchical_preparation_not_demonstrated"
        return report
    artifacts = artifact_hashes(root)
    code, result, seconds = invoke_prepare(root, "replay")
    report.update(replay_exit_code=code, replay_seconds=seconds)
    report["reused_without_new_calls"] = (
        before == control_calls(root)
        and report["metrics"]["audited_exchanges"] == audit_metrics(root)["audited_exchanges"]
    )
    report["artifacts_unchanged"] = artifacts == artifact_hashes(root)
    report["passed"] = bool(
        not code
        and result.get("prepared")
        and result.get("ready")
        and report["reused_without_new_calls"]
        and report["artifacts_unchanged"]
    )
    if not report["passed"]:
        report["failure"] = "replay_did_not_reuse_exact_preparation"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create a fresh fixture; no provider calls")
    init.add_argument("--directory", type=Path)
    init.add_argument("--provider", required=True)
    init.add_argument("--model", required=True)
    init.add_argument("--timeout", type=int, default=600, help="Timeout per provider call, seconds")
    run = sub.add_parser("run", help="Use the configured real provider, then verify replay")
    run.add_argument("directory", type=Path)
    inspect = sub.add_parser("inspect", help="Read local audit sizes without provider calls")
    inspect.add_argument("directory", type=Path)
    args = parser.parse_args()
    # Fixture logs and preparation evidence stay private on POSIX hosts.
    os.umask(0o077)
    if args.command == "init":
        if args.timeout <= 0:
            parser.error("--timeout must be positive")
        print(create_fixture(args.directory, args.provider, args.model, args.timeout))
        return 0
    root = args.directory.resolve()
    check_fixture(root)
    if args.command == "inspect":
        print(json.dumps(audit_metrics(root), indent=2))
        return 0
    report = run_smoke(root)
    write_json(root / ".smoke/report.json", report)
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
