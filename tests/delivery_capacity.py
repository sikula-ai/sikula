"""Opt-in deterministic delivery experiment; real CLI/Git, fake providers only.

Run with ``python -m tests.delivery_capacity --depths 1 2 3``. Nothing is
created in the operator checkout; each case uses a fresh temporary repository.
Use ``--depths 2 --fanout 1 --scenario local-repair`` to check recursive leaf
repair and independent authority renewal at every affected level.
A smaller bounded full parent review is permitted only with
measured routing evidence. Add ``--require-compact-parents`` to require compact
parent reviews throughout, without changing production routing or limits.
Use ``--fanout 2`` for branching recovery, including preservation of unrelated
checkpoint admission/authority and fresh final applicability of both branches.
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

# Separate observable facets keep the small local-repair fixture representative
# of a detailed plan. They make composition useful without patching route sizing,
# changing production limits, or inflating provider prompts at runtime.
LOCAL_OUTCOMES = {
    "total": "The local contribution integrates with child totals.",
    "constant": "The module contributes exactly one to the aggregate, including when it has no descendants. Integrating a later consumer must not replace this contribution with zero or count it twice.",
    "interface": "Consumers can import the assigned module and call its total function without supplying arguments. The same public entrypoint remains available after other groups are integrated or repaired.",
    "leaf": "A module with no child interfaces returns its own contribution directly. It does not rely on a parent being present, a process-wide registry, or an initialization call by a future consumer.",
    "children": "A parent includes every immediate child's reported total exactly once in its result. No immediate child is omitted merely because another child exposes the same function name or returns the same value.",
    "delegation": "The parent consumes child totals through their public module interfaces. It does not duplicate descendant implementations or substitute a constant for the contribution computed by a child subtree.",
    "ownership": "Each delivered contribution belongs to one node in the hierarchy. Ancestors include it through their immediate child totals rather than adding that descendant again as a separate direct contribution.",
    "preservation": "Integrating a new module preserves previously delivered contributions and child entrypoints. A later write must not silently alter a completed leaf or reset an existing subtree's aggregate behavior.",
    "completion": "The completed hierarchy exposes an aggregate equal to the count of delivered original nodes. Completing an implementation repair restores the required result without creating an additional logical contribution.",
    "locality": "Computing a contribution uses the local module hierarchy only. It does not fetch values from external services or require a network connection to import a module or calculate the aggregate.",
    "privacy": "Private inputs and credentials are not written to logs by the delivered modules. The aggregation interface exposes only the required numerical result while keeping unrelated local information undisclosed.",
    "dependencies": "Child modules are delivered before consumers use their entrypoints. Each consumer can resolve all of its declared immediate child interfaces from the assembled candidate without inventing a replacement dependency.",
}


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

    def __init__(
        self,
        root: Path,
        depth: int,
        fanout: int,
        scenario: str,
        interrupt: bool,
        *,
        require_compact_parents: bool = False,
    ):
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
        self.before_repair: dict | None = None
        self.require_compact_parents = require_compact_parents
        self.review_routes: dict[str, dict] = {}

    def parent_review_route(self, prompt: str, context: dict) -> dict:
        """Require a measured complete route for this exact prompt, not a prior candidate."""
        from core.delivery_verification import MAX_DELIVERY_VERIFICATION_PACKET_BYTES

        route = self.review_routes.pop(sha256(prompt.encode()).hexdigest(), None)
        assert route is not None, "Parent review has no matching runtime route measurement"
        full, compact = route["full_packet_bytes"], route["composition_packet_bytes"]
        if "checkpoint_composition" in context:
            assert context["checkpoint_composition"].get("compact"), "Parent review must use compact authority"
            assert compact <= MAX_DELIVERY_VERIFICATION_PACKET_BYTES and compact < full, (
                "Composition must be bounded and smaller than full review"
            )
        else:
            assert not self.require_compact_parents, "Parent review must use compact authority"
            assert full <= MAX_DELIVERY_VERIFICATION_PACKET_BYTES and full <= compact, (
                "Full parent review must be bounded and no larger than composition"
            )
        return route

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
        outcomes = LOCAL_OUTCOMES if self.scenario == "local-repair" else {"total": LOCAL_OUTCOMES["total"]}
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
                    "id": key,
                    "summary": summary,
                    "source_fragment_ids": refs,
                    "unit_ids": ["work", *local_children],
                    "disposition": "preserved",
                }
                for key, summary in outcomes.items()
            ],
            "source_accounting": [
                {
                    "source_fragment_id": ref,
                    "disposition": "mapped",
                    "obligation_ids": list(outcomes),
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
            if node == "root" and self.scenario in {"repair", "local-repair"}:
                leaf = self.order[0]
                changes[f"src/{module(leaf)}.py"] = implementation(leaf, self.depth, self.fanout, value=0)
        else:
            assert self.scenario in {"repair", "local-repair"} and len(self.implemented) == len(self.order), (
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
            packet = json.loads(next(line for line in prompt.splitlines() if line.startswith('{"after":')))
            return self.record(
                "authority_renewal",
                packet["node_id"],
                prompt,
                json.dumps(
                    {
                        "schema_version": 1,
                        "packet_fingerprint": packet["packet_fingerprint"],
                        "disposition": "approved",
                        "summary": "Authority preserved.",
                    }
                ),
            )
        if "Author one small, self-contained delivery repair contract" in prompt:
            if self.scenario == "local-repair" and self.before_repair is None:
                progress = self.root / ".sikula/state/delivery/capacity/progress.json"
                self.before_repair = json.loads(progress.read_text(encoding="utf-8"))
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
        from core.delivery_verification import MAX_DELIVERY_VERIFICATION_PACKET_BYTES

        assert len(prompt.encode()) <= MAX_DELIVERY_VERIFICATION_PACKET_BYTES, "Review prompt exceeded its limit"
        route = {}
        if self.scenario == "local-repair" and children(node, self.depth, self.fanout):
            route = self.parent_review_route(prompt, context)
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
        self.calls[-1].update(route)
        self.calls[-1]["composed"] = "checkpoint_composition" in context
        self.calls[-1]["direct_units"] = len(context["units"])
        self.calls[-1]["checkpoint_outcomes"] = {
            item["id"]: item["outcome"] for item in control.get("checkpoint_results", [])
        }
        return output


@contextmanager
def observe_review_routes(fake: CapacityLLM):
    """Observe runtime sizing without replacing its decision or adding Git reads."""
    from core.delivery_review_route import review_route
    from core.delivery_verify import _composition_packet_fits

    def measured(context, snapshot, reviewer, worktree, source_task, validation, cfg, role):
        captured = []

        def capture(*args, **kwargs):
            result = review_route(*args, **kwargs)
            captured.append(result)
            return result

        with patch("core.delivery_review_route.review_route", new=capture):
            result = _composition_packet_fits(context, snapshot, reviewer, worktree, source_task, validation, cfg, role)
        assert len(captured) == 1, "Expected one complete runtime route measurement"
        route = captured[0]
        for selected, packet in (("full", snapshot.scope.plan_context()), ("composition", context)):
            prompt = reviewer._prompt(
                cwd=worktree,
                review_kind=role,
                source_task=source_task,
                plan_context=packet,
                validation_summary=validation.to_review_dict(
                    reviewer.project_config,
                    project_root=worktree,
                    include_final_checks=snapshot.scope.node_id == "root",
                ),
                candidate_commit=snapshot.identity.candidate_commit,
                candidate_tree=snapshot.identity.candidate_tree,
                known_obligation_ids=set(snapshot.scope.obligation_ids) if role == "semantic" else set(),
            )
            fake.review_routes[sha256(prompt.encode()).hexdigest()] = {
                "review_route": selected,
                "full_packet_bytes": route.full_bytes,
                "composition_packet_bytes": route.composition_bytes,
            }
        return result

    with patch("core.delivery_verify._composition_packet_fits", new=measured):
        yield


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


def assert_local_repair_lifecycle(root: Path, original_plan: dict, fake: CapacityLLM) -> None:
    """Finalization alone is not proof of recursive local recovery."""
    from core.delivery_repair import _read_state

    leaf = fake.order[0]
    chain = [".".join(leaf.split(".")[:level]) for level in range(fake.depth, 0, -1)] + ["root"]
    assert fake.interrupted and fake.gaps == 1
    assert [call["node"] for call in fake.calls if call["stage"] == "authority_renewal"] == chain
    assert len([call for call in fake.calls if call["stage"] == "repair_author"]) == 1
    plan = yaml.safe_load((root / ".sikula/delivery/capacity/plan.yaml").read_text())
    original_ids = {unit["id"] for unit in original_plan["units"]}
    assert [unit for unit in plan["units"] if unit["id"] in original_ids] == original_plan["units"]
    repairs = [unit for unit in plan["units"] if unit["id"] not in original_ids]
    assert len(repairs) == 1 and repairs[0]["repair_node"] == leaf
    control = _read_state(root, PLAN_ID, leaf)
    assert control["phase"] == "published" and control["attempts"] == 1
    assert control["candidate_recovery"]["node_id"] == "root"
    assert all(_read_state(root, PLAN_ID, ancestor) is None for ancestor in chain[1:])
    assert plan["final_gate_authority"]
    assert all(node in plan["composition_authority"] for node in chain[1:-1])
    assert fake.before_repair is not None
    progress = json.loads((root / ".sikula/state/delivery/capacity/progress.json").read_text(encoding="utf-8"))
    assert [unit for unit in progress["units"] if unit["unit_id"] in original_ids] == fake.before_repair["units"]
    assert_unaffected_checkpoints(original_plan, plan, fake.before_repair, progress, set(chain))
    for checkpoint in original_plan.get("checkpoints", []):
        if checkpoint["id"] not in chain:
            assert _read_state(root, PLAN_ID, checkpoint["id"]) is None
    repaired_at = max(index for index, call in enumerate(fake.calls) if call["stage"] == "implementation")
    fresh_reviews = {(call["node"], call["stage"]) for call in fake.calls[repaired_at + 1 :]}
    assert {(node, role) for node in chain for role in ("semantic", "security")} <= fresh_reviews
    for role in ("semantic", "security"):
        final = next(call for call in reversed(fake.calls) if call["node"] == "root" and call["stage"] == role)
        if final["composed"]:
            assert final["checkpoint_outcomes"] == {
                node: "applicable" for node in children("root", fake.depth, fake.fanout)
            }
        else:
            assert not fake.require_compact_parents and final["review_route"] == "full"


def assert_unaffected_checkpoints(
    before_plan: dict, after_plan: dict, before_progress: dict, after_progress: dict, repaired_chain: set[str]
) -> None:
    """Fresh candidate review must not rewrite unrelated historical admission."""
    after_nodes = {node["id"]: node for node in after_plan.get("checkpoints", [])}
    for node in before_plan.get("checkpoints", []):
        key = node["id"]
        if key in repaired_chain:
            continue
        assert after_nodes[key] == node, "Unrelated checkpoint ownership changed"
        for field in ("checkpoint_authority", "composition_authority"):
            assert after_plan.get(field, {}).get(key) == before_plan.get(field, {}).get(key), (
                "Unrelated checkpoint authority changed"
            )
        assert after_progress["checkpoint_verifications"][key] == before_progress["checkpoint_verifications"][key], (
            "Unrelated historical admission changed"
        )


def run_case(
    root: Path,
    *,
    depth: int,
    fanout: int = 2,
    scenario: str = "repair",
    prepare_only: bool = False,
    trace_memory: bool = False,
    require_compact_parents: bool = False,
) -> dict:
    if scenario == "local-repair" and depth < 1:
        raise ValueError("Local repair requires at least one checkpoint level")
    if require_compact_parents and scenario != "local-repair":
        raise ValueError("Compact parent acceptance requires the local-repair scenario")
    task = create_fixture(root)
    fake = CapacityLLM(
        root, depth, fanout, scenario, interrupt=not prepare_only, require_compact_parents=require_compact_parents
    )
    operations = []
    evidence = {"operations": 0, "reads": 0, "bytes_read": 0, "materialized_bytes": 0, "visits": 0, "expanded_items": 0}
    report = {
        "schema_version": 1,
        "depth": depth,
        "fanout": fanout,
        "scenario": scenario,
        "requested_units": len(fake.order),
        "prepare_only": prepare_only,
        "require_compact_parents": require_compact_parents,
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
            observe_review_routes(fake),
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
                assert len(fake.implemented) == len(fake.order) + (scenario in {"repair", "local-repair"})
                if scenario in {"repair", "local-repair"}:
                    assert fake.gaps
                if scenario == "local-repair":
                    assert all(
                        sha256((root / unit["task_path"]).read_bytes()).hexdigest()
                        == artifacts[root / unit["task_path"]]
                        for unit in plan["units"]
                    ), "Original unit contract bytes changed"
                    assert_local_repair_lifecycle(root, plan, fake)
                    report["local_repair_lifecycle_verified"] = True
                    report["compact_parent_lifecycle_verified"] = not any(
                        call.get("review_route") == "full" for call in fake.calls
                    )
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
        report["full_parent_reviews"] = sum(call.get("review_route") == "full" for call in fake.calls)
        (root / ".capacity/report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depths", nargs="+", type=int, default=[1, 2])
    parser.add_argument("--fanout", type=int, default=2)
    parser.add_argument(
        "--scenario",
        choices=["happy", "repair", "local-repair", "security", "readonly", "prerequisite"],
        default="repair",
    )
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--require-compact-parents",
        action="store_true",
        help="Require compact parent reviews in local-repair acceptance",
    )
    parser.add_argument(
        "--trace-memory",
        action="store_true",
        help="Measure Python allocations; adds runtime overhead, excludes subprocess memory",
    )
    args = parser.parse_args()
    if not all(0 <= depth <= 5 for depth in args.depths) or not 1 <= args.fanout <= 8:
        parser.error("Use depths 0..5 and fanout 1..8")
    if args.scenario == "local-repair" and min(args.depths) < 1:
        parser.error("Local repair requires at least one checkpoint level")
    if args.require_compact_parents and args.scenario != "local-repair":
        parser.error("--require-compact-parents requires --scenario local-repair")
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
            require_compact_parents=args.require_compact_parents,
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
