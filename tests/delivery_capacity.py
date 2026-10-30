"""Opt-in deterministic delivery experiment; real CLI/Git, fake providers only.

Run with ``python -m tests.delivery_capacity --depths 1 2 3``. Nothing is
created in the operator checkout; each case uses a fresh temporary repository.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile
import time
import tracemalloc
from unittest.mock import patch

import yaml

from core.delivery_evidence_resolution import EvidenceResolver
from core.llm_client import LLMReadOnlyViolation
from core.version import sikula_version
from sikula import main as sikula_main
from tests.e2e.conftest import FakeLLMClient
from tests.e2e.test_delivery_plan import _git_commit_all, _write_delivery_stop_config


PLAN_ID = "capacity"
BUILD = "python3 -m compileall -q src/"
TEST = "python3 -m unittest discover -s tests_proj -q"
SOURCE = """# Hierarchical totals

Build a tree of independent Python modules. Every node contributes one and its
total includes the totals of its immediate children. Deliver child modules before
their consumers. The final root total must equal the number of delivered nodes.
Preserve already delivered contributions when integrating other groups. Keep all
data local; do not log private inputs or introduce network access.
"""


def children(node: str, depth: int, fanout: int) -> list[str]:
    level = 0 if node == "root" else len(node.split("."))
    if level >= depth:
        return []
    return [("" if node == "root" else node + ".") + f"g{i}" for i in range(fanout)]


def nodes(depth: int, fanout: int, node: str = "root") -> list[str]:
    return [item for child in children(node, depth, fanout) for item in nodes(depth, fanout, child)] + [node]


def module(node: str) -> str:
    return "node_" + node.replace(".", "_")


def contract(node: str, *, repair: bool = False) -> str:
    return f"""# {"Restore" if repair else "Implement"} capacity node {node}

## Goal
Deliver this node's contribution and preserve the tree's aggregate behavior.

## Current behavior
The node's contribution is {"incorrect" if repair else "not implemented"}.

## Desired behavior
Expose VALUE = 1 and total(), which adds VALUE to immediate child totals.
Use existing Python modules in src/; preserve all other delivered contributions.

## Acceptance criteria
- The local contribution equals one.
- Consumers can call total() through the existing child module interfaces.
- Aggregation includes each assigned contribution exactly once.

## Out of scope
Do not introduce dependencies, networking or unrelated features.

## Security and privacy
Keep inputs local and do not log private data or credentials.

## Reviewer focus
Check child interface use, exact aggregation and preservation of prior work.

## Verification
- `{BUILD}`
- `{TEST}`
"""


def implementation(node: str, depth: int, fanout: int, *, value: int = 1) -> str:
    imports = [f"import {module(child)}" for child in children(node, depth, fanout)]
    terms = ["VALUE", *(f"{module(child)}.total()" for child in children(node, depth, fanout))]
    return "\n".join([*imports, f"VALUE = {value}", "", "def total():", "    return " + " + ".join(terms), ""])


class CapacityInterruption(KeyboardInterrupt):
    """Distinguish the injected interruption from an operator's Ctrl-C."""


class CapacityLLM(FakeLLMClient):
    """Deterministic fault oracle, not a claim about LLM semantic quality."""

    def __init__(self, root: Path, depth: int, fanout: int, scenario: str, interrupt: bool):
        super().__init__()
        self.root = root.resolve()
        self.depth, self.fanout, self.scenario = depth, fanout, scenario
        self.order = nodes(depth, fanout)
        self.calls: list[dict] = []
        self.implemented: list[str] = []
        self.interrupt_pending = interrupt
        self.interrupted = False
        self.gaps = 0
        self.renewals = 0

    def record(self, stage: str, node: str, prompt: str, output: str = "") -> str:
        self.calls.append(
            {"stage": stage, "node": node, "prompt_bytes": len(prompt.encode()), "response_bytes": len(output.encode())}
        )
        return output

    def generate(self, system: str, prompt: str) -> str:
        if "INPUT JSON:\n" not in prompt:
            if "delivery-plan authoring assistant" not in prompt:
                return self.record("planning", "unit", prompt, "SINGLE_PASS")
            return self.record(
                "route", "root", prompt, json.dumps({"plan_id": PLAN_ID, "preparation_mode": "hierarchical"})
            )
        packet = json.loads(prompt.split("INPUT JSON:\n", 1)[1])
        node = packet["node_id"]
        if "proposal" in packet:
            stage = "parent_preparation_review" if "accepted_children" in packet else "local_preparation_review"
            return self.record(
                stage,
                node,
                prompt,
                json.dumps({"kind": "approved", "input_fingerprint": packet["input_fingerprint"], "feedback": ""}),
            )
        if self.scenario == "prerequisite":
            return self.record("author", node, prompt, '{"kind":"stop_and_follow_up"}')
        refs = [fragment["id"] for fragment in packet["source_fragments"]]
        local_children = [child.rsplit(".", 1)[-1] for child in children(node, self.depth, self.fanout)]
        proposal = {
            "title": "Hierarchical totals",
            "children": [
                {
                    "id": child,
                    "source_fragment_ids": refs,
                    "depends_on": [],
                    "integration_context": "Provide a Python module exposing VALUE=1 and total() for this subtree.",
                }
                for child in local_children
            ],
            "units": [
                {
                    "id": "work",
                    "title": f"Implement node {node}",
                    "depends_on": local_children,
                    "scope_paths": ["src/"],
                    "risk_tags": ["privacy"],
                    "asset_paths": [],
                    "task_markdown": contract(node),
                    "budget": {"max_planner_steps": 1},
                }
            ],
            "constraints": [],
            "obligations": [
                {
                    "id": "total",
                    "summary": "The local contribution integrates with child totals.",
                    "source_fragment_ids": refs,
                    "unit_ids": ["work", *local_children],
                    "disposition": "preserved",
                }
            ],
            "source_accounting": [
                {
                    "source_fragment_id": ref,
                    "disposition": "mapped",
                    "obligation_ids": ["total"],
                    "constraint_ids": [],
                    "child_ids": local_children,
                    "rationale": "This rule governs every local contribution.",
                }
                for ref in refs
            ],
        }
        return self.record("author", node, prompt, json.dumps(proposal))

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        if not cwd.resolve().is_relative_to(self.root):
            raise ValueError("Synthetic implementation must stay inside its fixture repository")
        if len(self.implemented) < len(self.order):
            node = self.order[len(self.implemented)]
            changes = {f"src/{module(node)}.py": implementation(node, self.depth, self.fanout)}
            if node == "root" and self.scenario == "repair":
                leaf = self.order[0]
                changes[f"src/{module(leaf)}.py"] = implementation(leaf, self.depth, self.fanout, value=0)
        else:
            assert self.scenario == "repair" and len(self.implemented) == len(self.order), (
                "Unexpected implementation call"
            )
            node = self.order[0]
            changes = {f"src/{module(node)}.py": implementation(node, self.depth, self.fanout)}
        for name, content in changes.items():
            (cwd / name).parent.mkdir(parents=True, exist_ok=True)
            (cwd / name).write_bytes(content.encode())
        self.implemented.append(node)
        self.record("implementation", node, prompt)
        return sorted(changes), "Implemented the assigned contribution."

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        if "Independently assess the proposed repair authority" in prompt:
            self.renewals += 1
            fingerprint = re.search(r'"packet_fingerprint": "(sha256:[a-f0-9]+)"', prompt).group(1)
            return self.record(
                "authority_renewal",
                "repair",
                prompt,
                json.dumps(
                    {
                        "schema_version": 1,
                        "packet_fingerprint": fingerprint,
                        "disposition": "approved",
                        "summary": "Authority preserved.",
                    }
                ),
            )
        if "Author one small, self-contained delivery repair contract" in prompt:
            return self.record(
                "repair_author",
                self.order[0],
                prompt,
                json.dumps(
                    {
                        "schema_version": 1,
                        "disposition": "repair",
                        "task_markdown": contract(self.order[0], repair=True),
                    }
                ),
            )
        if "Delivery plan context:\n" not in prompt:
            if "Produce the implementation prompt." in prompt:
                return self.record(
                    "analysis",
                    "unit",
                    prompt,
                    "Implement the assigned capacity module using its child interfaces; preserve other modules and run the declared validation.",
                )
            if "DISPOSITION CONTRACT" in prompt:
                return self.record(
                    "unit_review",
                    "unit",
                    prompt,
                    json.dumps(
                        {
                            "sikula_disposition_schema_version": 1,
                            "disposition": "approved",
                            "summary": "Fixture unit accepted.",
                        }
                    ),
                )
            return self.record("unit_review_or_analysis", "unit", prompt, super().run_readonly_agent(prompt, cwd))
        context = json.loads(
            prompt.split("Delivery plan context:\n", 1)[1].split("\n\nDeterministic validation summary:", 1)[0]
        )
        node = context.get("verification_node", {}).get("id", "root")
        if "checkpoint_composition" in context:
            assert len(context["units"]) <= 2, "Parent prompt expanded descendant contracts"
            assert len(context["checkpoint_composition"]["children"]) <= self.fanout
        security = "Review cross-unit security and privacy behavior" in prompt
        stage = "security" if security else "semantic"
        if not security and node != "root" and self.interrupt_pending:
            self.interrupt_pending = False
            self.interrupted = True
            self.record(stage + "_interrupted", node, prompt)
            raise CapacityInterruption()
        if security and node == "root" and self.scenario == "readonly":
            self.record(stage, node, prompt)
            raise LLMReadOnlyViolation("Synthetic read-only boundary")
        template = next(line for line in reversed(prompt.splitlines()) if line.startswith('{"schema_version":2'))
        control = json.loads(template)
        if security and node == "root" and self.scenario == "security":
            control.update(disposition="human_review_required", summary="Synthetic security boundary.")
            control["findings"] = [
                {
                    "code": "security.synthetic",
                    "summary": "Synthetic security boundary.",
                    "unit_ids": [],
                    "obligation_ids": [],
                }
            ]
        leaf = self.order[0]
        broken = (cwd / f"src/{module(leaf)}.py").exists() and "VALUE = 0" in (
            cwd / f"src/{module(leaf)}.py"
        ).read_text()
        if not security and broken:
            uncertain = [
                item
                for item in control.get("checkpoint_results", [])
                if leaf == item["id"] or leaf.startswith(item["id"] + ".")
            ]
            if uncertain:
                for item in uncertain:
                    item["outcome"] = "verification_required"
            elif any(
                item["id"] == (leaf + ".total" if leaf != "root" else "total") for item in control["obligation_results"]
            ):
                self.gaps += 1
                control.update(disposition="repair_required", summary="A delivered contribution was lost.")
                ids = [leaf + ".total" if leaf != "root" else "total"]
                for item in control["obligation_results"]:
                    item["outcome"] = "missing" if item["id"] in ids else "satisfied"
                control["findings"] = [
                    {
                        "code": "total.missing",
                        "summary": "Restore the missing contribution.",
                        "unit_ids": [leaf + ".work" if leaf != "root" else "work"],
                        "obligation_ids": ids,
                    }
                ]
        output = self.record(stage, node, prompt, json.dumps(control))
        self.calls[-1]["composed"] = "checkpoint_composition" in context
        self.calls[-1]["direct_units"] = len(context["units"])
        return output


def create_fixture(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=False)
    (root / "src").mkdir()
    (root / "tests_proj").mkdir()
    (root / "tests_proj/test_interfaces.py").write_text(
        "import importlib, pathlib, sys, unittest\n"
        "sys.path.insert(0, str(pathlib.Path('src').resolve()))\n"
        "class Interfaces(unittest.TestCase):\n"
        "    def test_local_interfaces(self):\n"
        "        for path in pathlib.Path('src').glob('node_*.py'):\n"
        "            self.assertIsInstance(importlib.import_module(path.stem).total(), int)\n",
        encoding="utf-8",
    )
    _write_delivery_stop_config(root)
    config_path = root / ".sikula/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["build"].update(compile_command=BUILD, test_command=TEST)
    # TestWriter is outside this capacity experiment; validation and both review roles run.
    config["run_test_writing"] = False
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with (root / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write(".capacity/\n__pycache__/\n")
    task = root / ".sikula/tasks/capacity.md"
    task.parent.mkdir(parents=True)
    task.write_bytes(SOURCE.encode())
    (root / ".capacity").mkdir()
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Capacity fixture"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "capacity@example.invalid"], cwd=root, check=True, capture_output=True
    )
    _git_commit_all(root, "Initialize synthetic capacity fixture")
    return task


@contextmanager
def working_directory(root: Path):
    previous = Path.cwd()
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(previous)


def run_case(
    root: Path,
    *,
    depth: int,
    fanout: int = 2,
    scenario: str = "repair",
    prepare_only: bool = False,
    trace_memory: bool = False,
) -> dict:
    task = create_fixture(root)
    fake = CapacityLLM(root, depth, fanout, scenario, interrupt=not prepare_only)
    operations = []
    evidence = {"operations": 0, "reads": 0, "bytes_read": 0, "materialized_bytes": 0, "visits": 0, "expanded_items": 0}
    report = {
        "schema_version": 1,
        "depth": depth,
        "fanout": fanout,
        "scenario": scenario,
        "requested_units": len(fake.order),
        "prepare_only": prepare_only,
        "python": platform.python_version(),
        "platform": platform.system(),
        "sikula_version": sikula_version(),
        "python_memory_tracing": trace_memory,
        "completed": False,
        "checks_passed": False,
        "operations": operations,
        "calls": fake.calls,
        "evidence_totals": evidence,
        "source_bytes": len(SOURCE.encode()),
    }
    original_exit = EvidenceResolver.__exit__

    def measure_exit(resolver, *args):
        try:
            return original_exit(resolver, *args)
        finally:
            evidence["operations"] += 1
            for field in evidence.keys() - {"operations"}:
                evidence[field] += getattr(resolver, field)

    def invoke(label: str, *args: str) -> dict:
        before, start = len(fake.calls), time.perf_counter()
        output, errors = io.StringIO(), io.StringIO()
        code, interrupted = 0, False
        with (
            redirect_stdout(output),
            redirect_stderr(errors),
            patch("sys.argv", ["sikula", "delivery", *args, "--json"]),
        ):
            try:
                sikula_main()
            except SystemExit as exc:
                code = exc.code
            except CapacityInterruption:
                interrupted = True
        (root / f".capacity/{label}.stdout").write_text(output.getvalue(), encoding="utf-8")
        (root / f".capacity/{label}.stderr").write_text(errors.getvalue(), encoding="utf-8")
        payload = json.loads(output.getvalue()) if output.getvalue().strip() else {}
        operations.append(
            {
                "phase": label,
                "seconds": round(time.perf_counter() - start, 3),
                "exit_code": code,
                "interrupted": interrupted,
                "calls": len(fake.calls) - before,
                "stop_code": payload.get("stop_code"),
                "error_codes": [item["code"] for item in payload.get("errors", [])],
            }
        )
        return payload

    started = time.perf_counter()
    if trace_memory:
        tracemalloc.start()
    try:
        with (
            working_directory(root),
            patch("core.llm_client.create_llm_client", return_value=fake),
            patch.object(EvidenceResolver, "__exit__", measure_exit),
        ):
            prepared = invoke("prepare", "prepare", str(task))
            if not prepared.get("prepared") or not prepared.get("ready"):
                report["stop_phase"] = "prepare"
                if scenario == "prerequisite":
                    assert "delivery_prepare.prerequisite_stop" in operations[-1]["error_codes"]
                    before = len(fake.calls)
                    repeated = invoke("terminal_replay", "prepare", str(task))
                    assert not repeated.get("prepared") and len(fake.calls) == before
                    report["expected_terminal_stop"] = True
                    report["checks_passed"] = True
                return report
            path = root / ".sikula/delivery/capacity/plan.yaml"
            report["prepared"] = True
            report["plan_bytes"] = path.stat().st_size
            plan = yaml.safe_load(path.read_text())
            report["units"] = len(plan["units"])
            report["checkpoints"] = len(plan.get("checkpoints", []))
            artifacts = {p: sha256(p.read_bytes()).hexdigest() for p in path.parent.rglob("*") if p.is_file()}
            replay = invoke("prepare_replay", "prepare", str(task), "--force")
            assert replay.get("prepared") and replay.get("ready") and operations[-1]["calls"] == 0
            assert all(sha256(p.read_bytes()).hexdigest() == fingerprint for p, fingerprint in artifacts.items())
            if prepare_only:
                report["checks_passed"] = True
                return report
            _git_commit_all(root, "Publish checked synthetic plan")
            index = (root / ".git/index").read_bytes()
            result = invoke("run", "run", str(path), "--max-units", "1")
            if operations[-1]["interrupted"] or result.get("succeeded"):
                result = invoke("resume", "run", str(path), "--max-units", str(len(fake.order) + 2))
            report["completed"] = bool(result.get("finalized"))
            report["stop_phase"] = None if report["completed"] else operations[-1]["phase"]
            if report["completed"]:
                repeated = invoke("final_replay", "run", str(path))
                assert repeated.get("finalized") and repeated["units_attempted"] == 0 and operations[-1]["calls"] == 0
                candidate = result["final_commit"]
                # Read the final Git tree, not the untouched operator checkout.
                for node in fake.order:
                    content = subprocess.run(
                        ["git", "show", f"{candidate}:src/{module(node)}.py"],
                        cwd=root,
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout
                    assert content == implementation(node, depth, fanout)
                assert len(fake.implemented) == len(fake.order) + (scenario == "repair")
                if scenario == "repair":
                    assert fake.gaps
            elif scenario in {"security", "readonly"}:
                expected = "delivery_verification." + (
                    "human_review_required" if scenario == "security" else "readonly_mutation"
                )
                assert result.get("stop_code") == expected, result.get("stop_code")
                before = len(fake.calls)
                repeated = invoke("terminal_replay", "run", str(path))
                assert not repeated.get("finalized") and len(fake.calls) == before
                report["expected_terminal_stop"] = True
            assert (root / ".git/index").read_bytes() == index
            report["checks_passed"] = report["completed"] or report.get("expected_terminal_stop", False)
            return report
    finally:
        report.update(
            seconds=round(time.perf_counter() - started, 3),
            implementations=len(fake.implemented),
            semantic_gaps=fake.gaps,
            authority_renewals=fake.renewals,
            interrupted=fake.interrupted,
            max_prompt_bytes=max((call["prompt_bytes"] for call in fake.calls), default=0),
            artifact_bytes=sum(
                p.stat().st_size for p in (root / ".sikula").rglob("*") if p.is_file() and "worktrees" not in p.parts
            ),
        )
        if trace_memory:
            report["python_peak_bytes"] = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
        report["composed_reviews"] = sum(call.get("composed", False) for call in fake.calls)
        (root / ".capacity/report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depths", nargs="+", type=int, default=[1, 2])
    parser.add_argument("--fanout", type=int, default=2)
    parser.add_argument(
        "--scenario", choices=["happy", "repair", "security", "readonly", "prerequisite"], default="repair"
    )
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--trace-memory",
        action="store_true",
        help="Measure Python allocations; adds runtime overhead, excludes subprocess memory",
    )
    args = parser.parse_args()
    if not all(0 <= depth <= 5 for depth in args.depths) or not 1 <= args.fanout <= 8:
        parser.error("Use depths 0..5 and fanout 1..8")
    root = Path(tempfile.mkdtemp(prefix="sikula-capacity-")).resolve()
    print(f"Private experiment artifacts: {root}", flush=True)
    reports = []
    for index, depth in enumerate(args.depths):
        report = run_case(
            root / f"case-{index}",
            depth=depth,
            fanout=args.fanout,
            scenario=args.scenario,
            prepare_only=args.prepare_only,
            trace_memory=args.trace_memory,
        )
        reports.append(report)
        print(
            json.dumps(
                {
                    key: report.get(key)
                    for key in (
                        "depth",
                        "requested_units",
                        "completed",
                        "prepared",
                        "stop_phase",
                        "seconds",
                        "plan_bytes",
                        "max_prompt_bytes",
                    )
                }
            ),
            flush=True,
        )
    (root / "summary.json").write_text(json.dumps(reports, indent=2) + "\n", encoding="utf-8")
    return 0 if all(report["checks_passed"] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
