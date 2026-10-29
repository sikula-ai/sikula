from __future__ import annotations

import json

import pytest

from agents.delivery_preparation_agent import DeliveryPreparationAgent
from agents.delivery_hierarchical_preparation_agent import preparation_directory
from core.delivery_authoring import DeliveryAuthoringParseError, public_delivery_preparation_failure
from core.delivery_prepare_writer import write_delivery_prepare_artifacts
from core.llm_client import LLMReadOnlyViolation
from tests.test_delivery_prepare_writer import _ready_task_markdown

SOURCE = (
    "# Feature delivery\n\nImplement a coherent repository feature with separate components and integrated behavior.\n"
)


def proposal(key, fragments, *, branch=False):
    children = (
        [
            {
                "id": name,
                "integration_context": f"Provide the {name} contribution through an explicit stable interface.",
                "source_fragment_ids": fragments,
                "depends_on": [],
            }
            for name in ("alpha", "beta")
        ]
        if branch
        else []
    )
    units = [
        {
            "id": "work",
            "title": "Implement scoped behavior",
            "depends_on": [],
            "scope_paths": ["src/"],
            "task_markdown": _ready_task_markdown(f"Local contract {key}"),
            "budget": {"max_planner_steps": 1},
        }
    ]
    if branch:
        units[0]["depends_on"] = ["alpha", "beta"]
    owners = [c["id"] for c in children] + ["work"]
    return {
        "title": "Coherent implementation",
        "children": children,
        "units": units,
        "constraints": [],
        "obligations": [
            {
                "id": "behavior",
                "summary": "The assigned behavior integrates through its intended interfaces.",
                "unit_ids": owners,
                "source_fragment_ids": fragments,
                "disposition": "preserved",
            }
        ],
        "source_accounting": [
            {
                "source_fragment_id": f,
                "disposition": "mapped",
                "obligation_ids": ["behavior"],
                "constraint_ids": [],
                "child_ids": [c["id"] for c in children],
                "rationale": "This fragment governs the assigned behavior.",
            }
            for f in fragments
        ],
    }


class TreeLLM:
    def __init__(self, *, depth=2, hook=None):
        self.depth, self.hook = depth, hook
        self.calls = []

    def generate(self, system, prompt):
        if "INPUT JSON:\n" not in prompt:
            self.calls.append(("route", {}))
            return json.dumps({"plan_id": "hierarchy", "preparation_mode": "hierarchical"})
        packet = json.loads(prompt.split("INPUT JSON:\n", 1)[1])
        stage = "integration" if "accepted_children" in packet else "review" if "proposal" in packet else "author"
        self.calls.append((stage, packet))
        if self.hook is not None:
            output = self.hook(stage, packet)
            if output is not None:
                return output
        if stage == "author":
            level = 0 if packet["node_id"] == "root" else len(packet["node_id"].split("."))
            data = proposal(packet["node_id"], [f["id"] for f in packet["source_fragments"]], branch=level < self.depth)
        else:
            data = {"kind": "approved", "input_fingerprint": packet["input_fingerprint"], "feedback": ""}
        return json.dumps(data)


@pytest.fixture
def setup(tmp_path):
    root = tmp_path.resolve()
    task = root / "source.md"
    task.write_text(SOURCE, encoding="utf-8")
    (root / "src").mkdir()
    cfg = {
        "project": {"root_path": str(root), "build_tool": "python"},
        "run_build": True,
        "run_tests": True,
        "run_checks": True,
        "build": {"test_command": "pytest", "checks": [{"name": "ruff", "command": "ruff check ."}]},
    }
    return root, task, cfg


def prepare(setup, llm, audit=None):
    root, task, cfg = setup
    return DeliveryPreparationAgent(llm, cfg).author_delivery_plan(
        task_description=task.read_text(encoding="utf-8"),
        task_path=task,
        plan_id="hierarchy",
        project_root=root,
        output_dir=root / ".sikula/delivery/hierarchy",
        project_context={},
        audit_recorder=audit,
    )


def test_tree_prepares_checked_plan_and_reuses_all_accepted_work(setup):
    llm = TreeLLM()
    audit = []
    draft = prepare(setup, llm, audit.append)
    assert len(draft.units) == 7 and len(draft.checkpoints) == 6
    assert draft.constraint_verification.checkpoint_authority_complete
    assert draft.constraint_verification.final_gate_authority_complete
    assert len(llm.calls) == 18  # route + 7 author/review pairs + 3 parent checks
    assert sum("prompt" in r for r in audit) == 18
    assert sum(r.get("parsed", {}).get("status") == "approved" for r in audit) == 10
    for stage, packet in llm.calls:
        if stage != "route":
            assert packet["node_role"] == ("final_gate" if packet["node_id"] == "root" else "checkpoint")
    before = len(llm.calls)
    repeated = prepare(setup, llm, audit.append)
    assert repeated == draft and len(llm.calls) == before
    root, task, cfg = setup
    written = write_delivery_prepare_artifacts(
        draft,
        output_dir=".sikula/delivery/hierarchy",
        project_root=root,
        project_config=cfg,
        source_task_description=SOURCE,
        source_task_path=task,
    )
    assert written.prepared, written
    import yaml

    plan = yaml.safe_load((root / ".sikula/delivery/hierarchy/plan.yaml").read_text())
    assert plan["final_gate_authority"]
    assert len(plan["composition_authority"]) == 2
    assert len(plan["checkpoint_authority"]) == 6


def test_short_prompt_references_preserve_exact_published_source_authority(setup):
    from core.delivery_obligations import delivery_authority_fragment_map

    llm = TreeLLM(depth=1)
    draft = prepare(setup, llm)
    original_fragments = delivery_authority_fragment_map(SOURCE)
    original_ids = set(original_fragments)
    source_text = {f"f{index}": fragment.text for index, fragment in enumerate(original_fragments.values(), start=1)}
    assert {ref for outcome in draft.obligations for ref in outcome.source_fragment_ids} == original_ids
    assert {record.source_fragment_id for record in draft.source_accounting} == original_ids
    for stage, packet in llm.calls:
        if stage == "route":
            continue
        references = {fragment["id"] for fragment in packet["source_fragments"]}
        assert references == {f"f{index}" for index in range(1, len(original_ids) + 1)}
        assert {f["id"]: f["text"] for f in packet["source_fragments"]} == source_text
        for outcome in packet.get("proposal", {}).get("obligations", []):
            assert set(outcome["source_fragment_ids"]) <= references


def test_source_reference_translation_is_exact_and_never_rewrites_contract_text():
    from core.delivery_preparation_tree import remap_source_references

    data = proposal("root", ["f1"], branch=True)
    data["units"][0]["task_markdown"] += "\nPreserve the literal f1 in the public interface.\n"
    data["source_accounting"][0]["rationale"] = "The f1 reference is described here."
    translated = remap_source_references(data, {"f1": "source-1-2-exact"})
    assert translated["units"] == data["units"]
    assert translated["source_accounting"][0]["rationale"] == data["source_accounting"][0]["rationale"]
    assert translated["obligations"][0]["source_fragment_ids"] == ["source-1-2-exact"]
    assert data["obligations"][0]["source_fragment_ids"] == ["f1"]
    assert remap_source_references(translated, {"source-1-2-exact": "f1"}) == data
    with pytest.raises(DeliveryAuthoringParseError):
        remap_source_references(data, {"f2": "source-1-2-exact"})


@pytest.mark.parametrize("unknown", [False, True])
def test_inherited_dependencies_are_preserved_in_assembly_and_not_local_ownership(setup, unknown):
    def dependencies(stage, packet):
        if stage != "author":
            return None
        key = packet["node_id"]
        if key == "root":
            data = proposal(key, [f["id"] for f in packet["source_fragments"]], branch=True)
            data["children"][0]["id"] = "upstream"
            data["children"][1]["depends_on"] = ["upstream"]
            data["units"][0]["depends_on"] = ["upstream", "beta"]
            data["obligations"][0]["unit_ids"] = ["upstream", "beta", "work"]
            for record in data["source_accounting"]:
                record["child_ids"] = ["upstream", "beta"]
            return json.dumps(data)
        if key == "beta":
            assert [v["id"] for v in packet["dependency_interfaces"]] == ["root.upstream"]
            data = proposal(key, [f["id"] for f in packet["source_fragments"]], branch=True)
            for item in data["units"] + data["children"]:
                item["depends_on"].append("unknown" if unknown else "root.upstream")
            return json.dumps(data)

    llm = TreeLLM(depth=2, hook=dependencies)
    if unknown:
        with pytest.raises(DeliveryAuthoringParseError):
            prepare(setup, llm)
        assert not any(stage == "review" and p.get("node_id") == "beta" for stage, p in llm.calls)
        return
    draft = prepare(setup, llm)
    upstream = {unit.id for unit in draft.units if unit.id.startswith("upstream.")}
    for unit in draft.units:
        if unit.id.startswith("beta."):
            assert upstream <= set(unit.depends_on)
    beta_review = next(p for stage, p in llm.calls if stage == "review" and p["node_id"] == "beta")
    assert all(
        "root.upstream" not in v["depends_on"]
        for v in beta_review["proposal"]["units"] + beta_review["proposal"]["children"]
    )
    assert all("root.upstream" not in o["unit_ids"] for o in beta_review["proposal"]["obligations"])
    downstream_packets = [p for stage, p in llm.calls if p.get("node_id", "").startswith("beta.")]
    assert downstream_packets
    for packet in downstream_packets:
        assert packet["dependency_interfaces"] == beta_review["dependency_interfaces"]
    count = len(llm.calls)
    assert prepare(setup, llm) == draft and len(llm.calls) == count


def test_nested_prerequisite_interfaces_keep_their_origin_and_do_not_shadow_local_owners(setup):
    def dependencies(stage, packet):
        if stage == "author" and packet["node_id"] in {"root", "beta"}:
            data = proposal(packet["node_id"], [f["id"] for f in packet["source_fragments"]], branch=True)
            data["children"][1]["depends_on"] = ["alpha"]
            if packet["node_id"] == "beta":
                # The local alpha remains a dependency/owner; root.alpha is an
                # inherited input and must not replace that local edge.
                data["children"][1]["depends_on"].append("root.alpha")
            return json.dumps(data)

    llm = TreeLLM(depth=2, hook=dependencies)
    draft = prepare(setup, llm)
    packets = [p for stage, p in llm.calls if p.get("node_id") == "beta.beta"]
    assert {stage for stage, p in llm.calls if p.get("node_id") == "beta.beta"} == {"author", "review"}
    for packet in packets:
        assert [item["id"] for item in packet["dependency_interfaces"]] == ["root.alpha", "beta.alpha"]
    units = {u.id: u for u in draft.units}
    assert {"alpha.alpha.work", "alpha.beta.work", "alpha.work", "beta.alpha.work"} <= set(
        units["beta.beta.work"].depends_on
    )
    count = len(llm.calls)
    assert prepare(setup, llm) == draft and len(llm.calls) == count


@pytest.mark.parametrize("kind", ["unit", "child"])
@pytest.mark.parametrize("invalid", [False, True])
def test_absent_dependencies_default_to_empty_but_null_is_invalid(setup, kind, invalid):
    def dependencies(stage, packet):
        if stage != "author":
            return None
        key = packet["node_id"]
        branch = key == "root"
        data = proposal(key, [f["id"] for f in packet["source_fragments"]], branch=branch)
        target = None
        if branch and kind == "child":
            target = data["children"][0]
        elif not branch and kind == "unit":
            target = data["units"][0]
        if target is not None:
            if invalid:
                target["depends_on"] = None
            else:
                del target["depends_on"]
        return json.dumps(data)

    llm = TreeLLM(depth=1, hook=dependencies)
    if invalid:
        with pytest.raises(DeliveryAuthoringParseError):
            prepare(setup, llm)
        return
    draft = prepare(setup, llm)
    units = {unit.id: unit for unit in draft.units}
    assert units["alpha.work"].depends_on == units["beta.work"].depends_on == []
    assert set(units["work"].depends_on) == {"alpha.work", "beta.work"}
    count = len(llm.calls)
    assert prepare(setup, llm) == draft and len(llm.calls) == count


def test_changed_preparation_instructions_invalidate_saved_work(setup, monkeypatch):
    import agents.delivery_hierarchical_preparation_agent as hierarchy

    llm = TreeLLM(depth=1)
    prepare(setup, llm)
    count = len(llm.calls)
    monkeypatch.setattr(hierarchy, "_PREPARATION_RULES", hierarchy._PREPARATION_RULES + "\nChanged instructions.\n")
    with pytest.raises(DeliveryAuthoringParseError) as exc:
        prepare(setup, llm)
    assert exc.value.code == "delivery_prepare.hierarchy_stale"
    assert len(llm.calls) == count


@pytest.mark.parametrize("key", ["root", "alpha.beta"])
@pytest.mark.parametrize("oversized", [False, True])
def test_requirement_ids_use_the_published_bound_after_prefixing(setup, key, oversized):
    from core.delivery_obligations import delivery_authority_fragment_map
    from core.delivery_plan import MAX_DELIVERY_UNIT_ID_LENGTH
    from core.delivery_preparation_tree import parse_node

    fragments = tuple(delivery_authority_fragment_map(SOURCE))
    data = proposal(key, list(fragments))
    prefix_size = 0 if key == "root" else len(key) + 1
    stem = "Order_complete-1"
    name = stem + "r" * (MAX_DELIVERY_UNIT_ID_LENGTH - prefix_size - len(stem) + int(oversized))
    data["obligations"][0]["id"] = name
    data["constraints"] = [
        {
            "id": name,
            "kind": "security_boundary",
            "summary": "Protect private local evidence.",
            "unit_ids": ["work"],
            "disposition": "preserved",
        }
    ]
    for record in data["source_accounting"]:
        record.update(obligation_ids=[name], constraint_ids=[name])
    if oversized:
        with pytest.raises(DeliveryAuthoringParseError):
            parse_node(json.dumps(data), key=key, source=SOURCE, fragment_ids=fragments, root=setup[0])
    else:
        node = parse_node(json.dumps(data), key=key, source=SOURCE, fragment_ids=fragments, root=setup[0])
        assert node.obligations[0].id == node.constraints[0].id == name


@pytest.mark.parametrize("kind", ["unit", "child"])
@pytest.mark.parametrize("key", ["root", "inventory"])
@pytest.mark.parametrize("oversized", [False, True])
def test_local_names_use_the_complete_checkpoint_namespace_bound(setup, kind, key, oversized):
    from core.delivery_obligations import delivery_authority_fragment_map
    from core.delivery_preparation_tree import parse_node

    fragments = tuple(delivery_authority_fragment_map(SOURCE))
    data = proposal(key, list(fragments), branch=True)
    prefix_size = 0 if key == "root" else len(key) + 1
    stem = "inventory-api"
    name = stem + "x" * (80 - prefix_size - len(stem) + int(oversized))
    old = "work" if kind == "unit" else "alpha"

    def rename(value):
        return name if value == old else value

    for record in data["units"] + data["children"]:
        record["id"] = rename(record["id"])
        record["depends_on"] = [rename(ref) for ref in record["depends_on"]]
    for record in data["obligations"]:
        record["unit_ids"] = [rename(ref) for ref in record["unit_ids"]]
    for record in data["source_accounting"]:
        record["child_ids"] = [rename(ref) for ref in record["child_ids"]]
    if oversized:
        with pytest.raises(DeliveryAuthoringParseError):
            parse_node(json.dumps(data), key=key, source=SOURCE, fragment_ids=fragments, root=setup[0])
    else:
        node = parse_node(json.dumps(data), key=key, source=SOURCE, fragment_ids=fragments, root=setup[0])
        assert name in node.obligations[0].unit_ids


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_parent_packets_do_not_accumulate_descendant_contracts(setup, depth):
    llm = TreeLLM(depth=depth)
    prepare(setup, llm)
    root_packet = next(p for stage, p in llm.calls if stage == "integration" and p["node_id"] == "root")
    assert "Local contract alpha" not in json.dumps(root_packet)
    assert len(root_packet["accepted_children"]) == 2
    assert len(json.dumps(root_packet)) < 12000
    for stage, packet in llm.calls:
        if stage == "integration":
            assert len(packet["accepted_children"]) == 2
            assert "Local contract " + packet["node_id"] + "." not in json.dumps(packet)


def test_interruption_consumes_reservation_but_preserves_accepted_sibling(setup):
    interrupted = False

    def interrupt(stage, packet):
        nonlocal interrupted
        if stage == "author" and packet["node_id"] == "beta" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    llm = TreeLLM(depth=1, hook=interrupt)
    with pytest.raises(KeyboardInterrupt):
        prepare(setup, llm)
    before = len(llm.calls)
    draft = prepare(setup, llm)
    assert len(draft.units) == 3
    later = llm.calls[before:]
    assert all(packet["node_id"] != "alpha" for _, packet in later)
    assert [stage for stage, p in later] == ["author", "review", "integration"]


@pytest.mark.parametrize("boundary", ["prerequisite", "readonly"])
def test_terminal_boundary_survives_audit_failure_and_resume(setup, boundary):
    def stop(stage, packet):
        if stage == "review":
            if boundary == "readonly":
                raise LLMReadOnlyViolation("mutation")
            return '{"kind":" stop_and_follow_up ","detail":"missing tool",}'

    llm = TreeLLM(depth=1, hook=stop)

    def audit(record):
        if record.get("stage") == "local_review":
            raise OSError("audit unavailable")

    with pytest.raises((DeliveryAuthoringParseError, OSError)):
        prepare(setup, llm, audit)
    before = len(llm.calls)
    with pytest.raises(DeliveryAuthoringParseError) as caught:
        prepare(setup, llm)
    assert caught.value.code.endswith("readonly_violation" if boundary == "readonly" else "prerequisite_stop")
    assert len(llm.calls) == before


def test_independent_gap_gets_bounded_local_correction(setup):
    rejected = False

    def reject(stage, packet):
        nonlocal rejected
        if stage == "review" and packet["node_id"] == "alpha" and not rejected:
            rejected = True
            return json.dumps(
                {
                    "kind": "revise",
                    "input_fingerprint": packet["input_fingerprint"],
                    "feedback": "Specify failure handling in the local acceptance criteria.",
                }
            )
        if stage == "author" and packet["node_id"] == "alpha" and packet.get("feedback"):
            data = proposal("alpha", [f["id"] for f in packet["source_fragments"]])
            data["units"][0]["task_markdown"] += "\nFailure handling must preserve stored state.\n"
            return json.dumps(data)

    llm = TreeLLM(depth=1, hook=reject)
    draft = prepare(setup, llm)
    assert "Failure handling" in next(u for u in draft.units if u.id == "alpha.work").task_markdown
    assert sum(stage == "author" and p["node_id"] == "alpha" for stage, p in llm.calls) == 2
    reviews = [p for stage, p in llm.calls if stage == "review" and p["node_id"] == "alpha"]
    assert "correction_feedback" not in reviews[0]
    assert reviews[1]["correction_feedback"] == "Specify failure handling in the local acceptance criteria."
    assert "previous_proposal" not in reviews[1]
    count = len(llm.calls)
    assert prepare(setup, llm) == draft and len(llm.calls) == count


def test_corrected_review_can_reject_a_new_gap_without_refilling_the_budget(setup):
    first = "Unit work: preserve the source-required failure behavior."
    second = "Unit work: its revised contract drops the source-required privacy boundary."

    def reject(stage, packet):
        if stage == "author" and packet.get("feedback"):
            data = packet["previous_proposal"]
            data["units"][0]["task_markdown"] += "\nFailure handling preserves stored state.\n"
            return json.dumps(data)
        if stage == "review":
            if "correction_feedback" in packet:
                assert packet["correction_feedback"] == first
            return json.dumps(
                {
                    "kind": "revise",
                    "input_fingerprint": packet["input_fingerprint"],
                    "feedback": second if "correction_feedback" in packet else first,
                }
            )

    llm = TreeLLM(depth=1, hook=reject)
    for _ in range(2):
        with pytest.raises(DeliveryAuthoringParseError) as exc:
            prepare(setup, llm)
        assert exc.value.code == "delivery_prepare.hierarchy_budget_exhausted"
    assert [stage for stage, _ in llm.calls] == ["route", "author", "review", "author", "review"]


@pytest.mark.parametrize("change", ["source", "config", "control", "selection", "exchange"])
def test_changed_or_missing_authority_preempts_provider_calls(setup, change):
    llm = TreeLLM(depth=1)
    prepare(setup, llm)
    root, task, cfg = setup
    directory = preparation_directory(root, "hierarchy")
    if change == "source":
        task.write_text(SOURCE + "Changed authority.\n")
    elif change == "config":
        cfg["run_tests"] = False
    elif change in {"control", "selection"}:
        (directory / f"{change}.json").unlink()
    else:
        (directory / "exchange-0.json").unlink()
    before = len(llm.calls)
    with pytest.raises(DeliveryAuthoringParseError):
        prepare(setup, llm)
    assert len(llm.calls) == before


def test_missing_selection_and_control_preserves_surviving_exchange_evidence(setup):
    llm = TreeLLM(depth=1)
    prepare(setup, llm)
    directory = preparation_directory(setup[0], "hierarchy")
    for name in ("selection.json", "control.json"):
        (directory / name).unlink()
    evidence = {path.name: path.read_bytes() for path in directory.iterdir() if path.name != "lock"}
    before = len(llm.calls)
    for _ in range(2):
        with pytest.raises(DeliveryAuthoringParseError) as exc:
            prepare(setup, llm)
        assert exc.value.code == "delivery_prepare.hierarchy_evidence_unavailable"
        assert len(llm.calls) == before
        assert {path.name: path.read_bytes() for path in directory.iterdir() if path.name != "lock"} == evidence


@pytest.mark.parametrize("feedback", ["", " ", "\t\r\n", "\u2003\u00a0"])
def test_whitespace_only_approval_does_not_consume_correction_round(setup, feedback):
    def approve(stage, packet):
        if stage in {"review", "integration"}:
            return json.dumps(
                {"kind": "approved", "input_fingerprint": packet["input_fingerprint"], "feedback": feedback}
            )

    llm = TreeLLM(depth=1, hook=approve)
    audit = []
    draft = prepare(setup, llm, audit.append)
    assert len(draft.units) == 3
    assert [stage for stage, _ in llm.calls].count("author") == 3
    assert sum(r.get("parsed", {}).get("status") == "approved" for r in audit) == 4
    assert not any(r.get("parsed", {}).get("status") == "revise" for r in audit)
    before = len(llm.calls)
    assert prepare(setup, llm, audit.append) == draft
    assert len(llm.calls) == before


@pytest.mark.parametrize(
    "damage", ["unknown_source", "missing_accounting", "cycle", "missing_shared_source", "oversized"]
)
def test_invalid_proposals_fail_before_independent_review(setup, damage):
    def broken(stage, packet):
        if stage != "author":
            return None
        data = proposal("root", [f["id"] for f in packet["source_fragments"]], branch=True)
        if damage == "unknown_source":
            data["obligations"][0]["source_fragment_ids"] = ["unknown"]
        elif damage == "missing_accounting":
            data["source_accounting"].pop()
        elif damage == "cycle":
            data["children"][0]["depends_on"] = ["beta"]
            data["children"][1]["depends_on"] = ["alpha"]
        elif damage == "missing_shared_source":
            data["children"][0]["source_fragment_ids"] = []
        else:
            data["children"][0]["integration_context"] = "x" * 5000
        return json.dumps(data)

    llm = TreeLLM(hook=broken)
    with pytest.raises(DeliveryAuthoringParseError) as caught:
        prepare(setup, llm)
    assert [stage for stage, _ in llm.calls] == ["route", "author"]
    assert public_delivery_preparation_failure(caught.value) is not None


@pytest.fixture
def correction_setup(setup):
    setup[1].write_text(
        SOURCE + "\n## Shared rules\n\nKeep operation failures atomic across components.\n", encoding="utf-8"
    )
    return setup


def inconsistent_proposal(packet, damage="combined"):
    data = proposal("root", [f["id"] for f in packet["source_fragments"]], branch=True)
    if damage in {"heading", "combined"}:
        data["children"][0]["source_fragment_ids"] = data["children"][0]["source_fragment_ids"][1:]
    if damage in {"accounting", "combined"}:
        data["source_accounting"][-1]["obligation_ids"] = []
    if damage in {"delegation", "combined"}:
        data["source_accounting"][-1]["child_ids"] = []
        data["source_accounting"][-1]["disposition"] = "context_only"
    if damage in {"summary", "combined"}:
        data["obligations"][0]["summary"] = SOURCE.splitlines()[-1]
    if damage in {"markdown", "combined"}:
        data["units"][0]["task_markdown"] = data["units"][0]["task_markdown"].replace(
            "## Verification", "## Verification\n\nRun from the repository root:"
        )
    return data


@pytest.mark.parametrize("damage", ["heading", "accounting", "delegation", "summary", "markdown", "combined"])
def test_unapproved_draft_collects_independent_feedback_before_its_one_correction(correction_setup, damage):
    def broken(stage, packet):
        if stage == "author" and packet["node_id"] == "root" and not packet["feedback"]:
            return json.dumps(inconsistent_proposal(packet, damage))

    llm = TreeLLM(depth=1, hook=broken)
    audit = []
    draft = prepare(correction_setup, llm, audit.append)
    assert len(draft.units) == 3
    root_calls = [(stage, p) for stage, p in llm.calls if p.get("node_id") == "root"]
    assert [stage for stage, _ in root_calls] == ["author", "review", "author", "review", "integration"]
    original, correction = root_calls[0][1], root_calls[2][1]
    assert correction["feedback"] and correction["previous_proposal"]
    assert len(correction["feedback"].encode()) <= 4096
    if damage in {"delegation", "combined"}:
        assert (
            'child_ids is [], but the current child source_fragment_ids require ["alpha", "beta"]'
            in correction["feedback"]
        )
    if damage in {"accounting", "combined"}:
        assert (
            'obligation_ids is [], but the current obligation source_fragment_ids require ["behavior"]'
            in correction["feedback"]
        )
    assert {k: v for k, v in original.items() if k not in {"feedback", "previous_proposal"}} == {
        k: v for k, v in correction.items() if k not in {"feedback", "previous_proposal"}
    }
    assert any(r.get("parsed", {}).get("error_code") == "delivery_prepare.hierarchy_draft_invalid" for r in audit)
    count = len(llm.calls)
    assert prepare(correction_setup, llm, audit.append) == draft
    assert len(llm.calls) == count


@pytest.mark.parametrize(
    "boundary", ["source", "constraint_source", "owner", "accounting_ref", "prerequisite", "scope", "marker", "asset"]
)
def test_draft_correction_never_masks_unknown_authority_or_prerequisite_stop(correction_setup, boundary):
    def broken(stage, packet):
        if stage != "author":
            return None
        data = inconsistent_proposal(packet)
        if boundary in {"source", "constraint_source"}:
            data["obligations"][0]["source_fragment_ids"].append("invented-source")
            if boundary == "constraint_source":
                data["constraints"] = [
                    {
                        "id": "rule",
                        "kind": "security_boundary",
                        "summary": SOURCE.splitlines()[-1],
                        "unit_ids": ["work"],
                        "disposition": "preserved",
                    }
                ]
        elif boundary == "owner":
            data["obligations"][0]["unit_ids"].append("invented-owner")
        elif boundary == "accounting_ref":
            data["source_accounting"][-1]["constraint_ids"] = ["invented-constraint"]
        elif boundary == "scope":
            data["units"][0]["scope_paths"] = ["../outside"]
        elif boundary == "marker":
            data["units"][0]["task_markdown"] += "\nsikula:generated-answer\n"
        elif boundary == "asset":
            data["units"][0]["asset_paths"] = ["../outside"]
        else:
            data["constraints"] = [{"kind": " stop_and_follow_up ", "detail": "Missing required external input."}]
        return json.dumps(data)

    llm = TreeLLM(depth=1, hook=broken)
    for _ in range(2):
        with pytest.raises(DeliveryAuthoringParseError) as exc:
            prepare(correction_setup, llm)
        assert exc.value.code != "delivery_prepare.hierarchy_draft_invalid"
    assert [stage for stage, _ in llm.calls] == ["route", "author"]


def test_repeated_inconsistent_draft_cannot_refill_correction_budget(correction_setup):
    def broken(stage, packet):
        if stage == "author":
            return json.dumps(inconsistent_proposal(packet))

    llm = TreeLLM(depth=1, hook=broken)
    for _ in range(2):
        with pytest.raises(DeliveryAuthoringParseError) as exc:
            prepare(correction_setup, llm)
        assert exc.value.code == "delivery_prepare.hierarchy_draft_invalid"
    assert [stage for stage, _ in llm.calls] == ["route", "author", "review", "author"]


def test_draft_correction_and_semantic_recovery_share_the_same_budget(correction_setup):
    def correction_then_gap(stage, packet):
        if stage == "author" and not packet["feedback"]:
            return json.dumps(inconsistent_proposal(packet))
        if stage == "review":
            return json.dumps(
                {"kind": "revise", "input_fingerprint": packet["input_fingerprint"], "feedback": "Missing behavior."}
            )

    llm = TreeLLM(depth=1, hook=correction_then_gap)
    for _ in range(2):
        with pytest.raises(DeliveryAuthoringParseError) as exc:
            prepare(correction_setup, llm)
        assert exc.value.code == "delivery_prepare.hierarchy_budget_exhausted"
    assert [stage for stage, _ in llm.calls] == ["route", "author", "review", "author", "review"]


def test_deterministic_and_semantic_gaps_are_resolved_together_without_extra_rounds(correction_setup):
    interface = "reserve(key, quantity) -> Result; release(key) -> Result; both are idempotent."

    def repair(stage, packet):
        if packet["node_id"] != "root":
            return None
        if stage == "author":
            if not packet["feedback"]:
                return json.dumps(inconsistent_proposal(packet))
            assert "Deterministic checks:" in packet["feedback"]
            assert "Independent review:" in packet["feedback"]
            assert interface in packet["feedback"]
            data = proposal("root", [f["id"] for f in packet["source_fragments"]], branch=True)
            data["children"][0]["integration_context"] = interface
            return json.dumps(data)
        if stage == "review" and packet["proposal"]["children"][0]["integration_context"] != interface:
            return json.dumps(
                {
                    "kind": "revise",
                    "input_fingerprint": packet["input_fingerprint"],
                    "feedback": "Declare the missing reservation interface: " + interface,
                }
            )

    llm = TreeLLM(depth=1, hook=repair)
    draft = prepare(correction_setup, llm)
    assert len(draft.units) == 3
    assert [s for s, p in llm.calls if p.get("node_id") == "root"] == [
        "author",
        "review",
        "author",
        "review",
        "integration",
    ]
    count = len(llm.calls)
    assert prepare(correction_setup, llm) == draft and len(llm.calls) == count


@pytest.mark.parametrize("boundary", ["prerequisite", "readonly", "authority"])
def test_review_of_correctable_draft_still_retains_terminal_boundaries(correction_setup, boundary):
    def reject(stage, packet):
        if stage == "author":
            return json.dumps(inconsistent_proposal(packet))
        if stage == "review":
            if boundary == "readonly":
                raise LLMReadOnlyViolation("Mutation detected")
            return json.dumps(
                {
                    "kind": "stop_and_follow_up" if boundary == "prerequisite" else "approved",
                    "input_fingerprint": "changed" if boundary == "authority" else packet["input_fingerprint"],
                    "feedback": "Missing prerequisite" if boundary == "prerequisite" else "",
                }
            )

    llm = TreeLLM(depth=1, hook=reject)
    with pytest.raises((DeliveryAuthoringParseError, LLMReadOnlyViolation)):
        prepare(correction_setup, llm)
    with pytest.raises(DeliveryAuthoringParseError) as caught:
        prepare(correction_setup, llm)
    assert caught.value.code.endswith(
        {
            "prerequisite": "prerequisite_stop",
            "readonly": "readonly_violation",
            "authority": "hierarchy_authority_changed",
        }[boundary]
    )
    assert [s for s, p in llm.calls] == ["route", "author", "review"]


@pytest.mark.parametrize("interrupted_stage", ["author", "review"])
def test_interrupted_draft_correction_reuses_initial_response_and_feedback(correction_setup, interrupted_stage):
    interrupted = False

    def interrupt(stage, packet):
        nonlocal interrupted
        if stage == "author" and packet["node_id"] == "root":
            if not packet["feedback"]:
                return json.dumps(inconsistent_proposal(packet))
        correction = packet.get("feedback") if stage == "author" else packet.get("correction_feedback")
        if stage == interrupted_stage and packet["node_id"] == "root" and correction and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    llm = TreeLLM(depth=1, hook=interrupt)
    with pytest.raises(KeyboardInterrupt):
        prepare(correction_setup, llm)
    assert len(prepare(correction_setup, llm).units) == 3
    root_authors = [p for stage, p in llm.calls if stage == "author" and p["node_id"] == "root"]
    if interrupted_stage == "author":
        assert len(root_authors) == 3 and root_authors[1] == root_authors[2]
    else:
        assert len(root_authors) == 2
        corrected_reviews = [p for stage, p in llm.calls if stage == "review" and p.get("correction_feedback")]
        assert len(corrected_reviews) == 2 and corrected_reviews[0] == corrected_reviews[1]


@pytest.mark.parametrize("envelope", ["unknown_field", "trailing_comma", "multiple_objects"])
def test_local_protocol_correction_keeps_inputs_and_finite_budget(setup, envelope):
    def malformed(stage, packet):
        if stage != "review" or packet["node_id"] != "alpha" or "protocol_correction" in packet:
            return None
        value = {"kind": "approved", "input_fingerprint": packet["input_fingerprint"], "feedback": ""}
        if envelope == "unknown_field":
            value["detail"] = "Reviewed"
            return json.dumps(value)
        if envelope == "trailing_comma":
            return json.dumps(value)[:-1] + ",}"
        return json.dumps(value) + " {}"

    llm = TreeLLM(depth=1, hook=malformed)
    prepare(setup, llm)
    calls = [p for stage, p in llm.calls if stage == "review" and p["node_id"] == "alpha"]
    assert len(calls) == 2
    assert calls[0] == {k: v for k, v in calls[1].items() if k != "protocol_correction"}
    count = len(llm.calls)
    prepare(setup, llm)
    assert len(llm.calls) == count


@pytest.mark.parametrize("changed_policy", [False, True])
@pytest.mark.parametrize("orphaned_evidence", [False, True])
def test_interrupted_initialization_resumes_only_unused_matching_control(
    setup, monkeypatch, changed_policy, orphaned_evidence
):
    import agents.delivery_hierarchical_preparation_agent as hierarchy

    write = hierarchy.write_repair_state

    def interrupt(root, path, state):
        if path.name == "selection.json":
            raise KeyboardInterrupt
        return write(root, path, state)

    llm = TreeLLM(depth=1)
    with monkeypatch.context() as patch:
        patch.setattr(hierarchy, "write_repair_state", interrupt)
        with pytest.raises(KeyboardInterrupt):
            prepare(setup, llm)
    assert [stage for stage, _ in llm.calls] == ["route"]
    if orphaned_evidence:
        directory = preparation_directory(setup[0], "hierarchy")
        (directory / "exchange-0.json").write_text("{}", encoding="utf-8")
    if changed_policy:
        setup[2]["run_tests"] = False
    if changed_policy or orphaned_evidence:
        with pytest.raises(DeliveryAuthoringParseError):
            prepare(setup, llm)
        assert len(llm.calls) == 1
    else:
        draft = prepare(setup, llm)
        assert len(draft.units) == 3
        assert sum(stage == "route" for stage, _ in llm.calls) == 1


@pytest.mark.parametrize(
    "orphan", [None, "exchange-0.json", "exchange-0-response-0.json", "context-x.json", "assessment-x.json"]
)
def test_location_only_initialization_requires_unused_directory(setup, orphan):
    from core.delivery_repair_storage import write_repair_state

    root = setup[0]
    directory = preparation_directory(root, "hierarchy")
    write_repair_state(root, directory / "location.json", {"policy": "hierarchical-preparation-v1"})
    if orphan is not None:
        write_repair_state(root, directory / orphan, {"evidence": "retained"})
    llm = TreeLLM(depth=1)
    if orphan is None:
        assert len(prepare(setup, llm).units) == 3
        assert all(stage != "route" for stage, _ in llm.calls)
    else:
        with pytest.raises(DeliveryAuthoringParseError) as exc:
            prepare(setup, llm)
        assert exc.value.code == "delivery_prepare.hierarchy_evidence_unavailable"
        assert not llm.calls
        assert not (directory / "control.json").exists()
        assert not (directory / "selection.json").exists()


@pytest.mark.parametrize("field", ["rationale", "integration_context", "feedback"])
def test_decoded_invalid_unicode_is_audited_without_losing_parser_classification(setup, field):
    def malformed(stage, packet):
        if field == "feedback" and stage == "review":
            return json.dumps(
                {"kind": "revise", "input_fingerprint": packet["input_fingerprint"], "feedback": "\ud800"}
            )
        if field != "feedback" and stage == "author":
            data = proposal("root", [f["id"] for f in packet["source_fragments"]], branch=True)
            if field == "rationale":
                data["source_accounting"][0][field] = "\ud800"
            else:
                data["children"][0][field] = "\ud800"
            return json.dumps(data)

    llm = TreeLLM(depth=1, hook=malformed)
    audit = []
    with pytest.raises(DeliveryAuthoringParseError) as exc:
        prepare(setup, llm, audit.append)
    assert exc.value.code == "delivery_prepare.hierarchy_output_invalid"
    assert any(record.get("parsed", {}).get("error_code") == exc.value.code for record in audit)
    count = len(llm.calls)
    with pytest.raises(DeliveryAuthoringParseError):
        prepare(setup, llm, audit.append)
    assert len(llm.calls) == count


@pytest.mark.parametrize("bad", ["stop", "fingerprint", "gap"])
@pytest.mark.parametrize("envelope", ["extra_field", "trailing_comma", "other_object"])
def test_malformed_local_reviews_cannot_drop_readable_boundaries_or_gaps(setup, bad, envelope):
    def malformed(stage, packet):
        if stage != "review":
            return None
        value = {"kind": "approved", "input_fingerprint": packet["input_fingerprint"], "feedback": ""}
        if bad == "stop":
            value["kind"] = " stop_and_follow_up "
        elif bad == "fingerprint":
            value["input_fingerprint"] = "changed"
        else:
            value.update(kind="revise", feedback="Missing shared prohibition.")
        if envelope == "extra_field":
            value["detail"] = "unsupported"
            return json.dumps(value)
        if envelope == "trailing_comma":
            return json.dumps(value)[:-1] + ",}"
        return json.dumps(
            {
                "kind": "approved",
                "input_fingerprint": packet["input_fingerprint"],
                "feedback": "",
                "detail": "unsupported",
            }
        ) + json.dumps(value)

    llm = TreeLLM(depth=1, hook=malformed)
    with pytest.raises(DeliveryAuthoringParseError):
        prepare(setup, llm)
    assert [stage for stage, p in llm.calls] == ["route", "author", "review"]
    count = len(llm.calls)
    with pytest.raises(DeliveryAuthoringParseError):
        prepare(setup, llm)
    assert len(llm.calls) == count


def test_audit_append_failure_reuses_received_response(setup):
    failed = False

    def audit(record):
        nonlocal failed
        if record.get("stage") == "local_review" and not failed:
            failed = True
            raise OSError("full audit unavailable")

    llm = TreeLLM(depth=1)
    with pytest.raises(DeliveryAuthoringParseError):
        prepare(setup, llm, audit)
    count = len(llm.calls)
    prepare(setup, llm, audit)
    assert not any(stage == "review" and p["node_id"] == "root" for stage, p in llm.calls[count:])


@pytest.mark.parametrize("audit_failure", [False, True])
def test_returned_rejection_survives_unavailable_context_and_resume(setup, audit_failure):
    root, _, _ = setup
    context = root / "src/api.py"
    context.write_bytes(b"API = 'existing'\n")
    rejected = False
    failed_audit = False

    def response(stage, packet):
        nonlocal rejected
        if stage == "author" and packet["node_id"] == "root" and "repository_evidence" not in packet:
            return json.dumps({"context_paths": ["src/api.py"]})
        if stage == "review" and packet["node_id"] == "root" and not rejected:
            rejected = True
            context.unlink()
            return json.dumps(
                {"kind": "revise", "input_fingerprint": packet["input_fingerprint"], "feedback": "Missing behavior."}
            )
        # A repeated provider call would approve, without correcting the draft.

    def audit(record):
        nonlocal failed_audit
        if audit_failure and record.get("stage") == "local_review" and not failed_audit:
            failed_audit = True
            raise OSError("Audit temporarily unavailable")

    llm = TreeLLM(depth=1, hook=response)
    with pytest.raises(DeliveryAuthoringParseError):
        prepare(setup, llm, audit)
    context.write_bytes(b"API = 'existing'\n")
    for _ in range(2):
        with pytest.raises(DeliveryAuthoringParseError) as exc:
            prepare(setup, llm, audit)
        assert exc.value.code == "delivery_prepare.hierarchy_budget_exhausted"
    assert sum(stage == "review" for stage, _ in llm.calls) == 1
    corrected = [p for stage, p in llm.calls if stage == "author" and p.get("feedback")]
    assert len(corrected) == 1 and corrected[0]["feedback"] == "Missing behavior."


def test_source_change_during_provider_call_is_durable_even_when_restored(setup):
    root, task, cfg = setup

    def mutate(stage, packet):
        if stage == "review":
            task.write_text(SOURCE + "Unexpected new scope.\n", encoding="utf-8")

    llm = TreeLLM(depth=1, hook=mutate)
    with pytest.raises(DeliveryAuthoringParseError):
        prepare(setup, llm)
    task.write_text(SOURCE, encoding="utf-8")
    before = len(llm.calls)
    with pytest.raises(DeliveryAuthoringParseError) as exc:
        prepare(setup, llm)
    assert exc.value.code.endswith("hierarchy_authority_changed")
    assert len(llm.calls) == before


def test_parent_correction_does_not_repeat_accepted_children(setup):
    def revise(stage, packet):
        if stage == "integration" and packet["proposal"]["title"] != "Revised integration":
            return json.dumps(
                {
                    "kind": "revise",
                    "input_fingerprint": packet["input_fingerprint"],
                    "feedback": "Make the existing integration intent explicit.",
                }
            )
        if stage == "author" and packet["node_id"] == "root" and packet["feedback"]:
            value = packet["previous_proposal"]
            value["title"] = "Revised integration"
            return json.dumps(value)

    llm = TreeLLM(depth=1, hook=revise)
    prepare(setup, llm)
    assert sum(stage == "author" and p["node_id"] == "alpha" for stage, p in llm.calls) == 1
    assert sum(stage == "integration" for stage, p in llm.calls) == 2
    corrected = [p for stage, p in llm.calls if stage in {"review", "integration"} and "correction_feedback" in p]
    assert len(corrected) == 2
    assert all(p["correction_feedback"] == "Make the existing integration intent explicit." for p in corrected)


def test_rejection_budget_is_not_refilled_by_resume(setup):
    def reject(stage, packet):
        if stage == "review":
            return json.dumps(
                {
                    "kind": "revise",
                    "input_fingerprint": packet["input_fingerprint"],
                    "feedback": "Keep the omitted shared privacy restriction.",
                }
            )

    llm = TreeLLM(depth=1, hook=reject)
    for _ in range(2):
        with pytest.raises(DeliveryAuthoringParseError) as exc:
            prepare(setup, llm)
        assert exc.value.code.endswith("hierarchy_budget_exhausted")
    assert [stage for stage, p in llm.calls] == ["route", "author", "review", "author"]
    # Unchanged proposals reuse the same rejection, not another independent call.


def test_review_sizing_reserves_json_escaped_feedback_before_call(setup, monkeypatch):
    import agents.delivery_hierarchical_preparation_agent as hierarchy
    from core.delivery_preparation_session import PreparationSession
    from core.delivery_preparation_tree import MAX_PREPARATION_REVIEW_FEEDBACK_BYTES

    root, _, _ = setup
    calls = []
    session = PreparationSession(root, preparation_directory(root, "sizing"), "bound", calls.append, None, lambda: None)
    runner = hierarchy._HierarchyPreparation(session, root, SOURCE, {}, "")
    packet = {"node_id": "root", "source": SOURCE}
    prompt = runner.prompt(hierarchy._REVIEW_RULES, packet)
    response = json.dumps(
        {
            "kind": "revise",
            "input_fingerprint": "sha256:" + "0" * 64,
            "feedback": "\0" * MAX_PREPARATION_REVIEW_FEEDBACK_BYTES,
        }
    )
    monkeypatch.setattr(hierarchy, "MAX_PREPARATION_PACKET_BYTES", len(prompt.encode()) + len(response.encode()) - 1)
    with pytest.raises(DeliveryAuthoringParseError) as exc:
        runner.review("root", "local_review", packet)
    assert exc.value.code == "delivery_prepare.hierarchy_required"
    assert not calls and not session.state["calls"]


def test_unicode_prompt_limit_preempts_reservation(setup):
    from agents.delivery_hierarchical_preparation_agent import _HierarchyPreparation, _PREPARATION_RULES
    from core.delivery_preparation_session import PreparationSession

    root, task, cfg = setup
    calls = []
    session = PreparationSession(root, preparation_directory(root, "sizing"), "bound", calls.append, None, lambda: None)
    runner = _HierarchyPreparation(session, root, SOURCE, {}, "")
    with pytest.raises(DeliveryAuthoringParseError) as exc:
        runner.prompt(_PREPARATION_RULES, {"source": "漢" * 70000})
    assert exc.value.code.endswith("hierarchy_required")
    assert not calls and not session.state["calls"]


def test_nested_group_can_use_inherited_consumers_without_invented_units(setup):
    def omit_parent_unit(stage, packet):
        if stage == "author" and packet["node_id"] in {"alpha", "beta"}:
            value = proposal(packet["node_id"], [f["id"] for f in packet["source_fragments"]], branch=True)
            value["units"] = []
            value["obligations"] = []
            for record in value["source_accounting"]:
                record.update(disposition="delegated", obligation_ids=[])
            return json.dumps(value)

    draft = prepare(setup, TreeLLM(depth=2, hook=omit_parent_unit))
    assert len(draft.units) == 5 and len(draft.checkpoints) == 6
    assert len(draft.obligations) == 5
    assert next(u for u in draft.units if u.id == "work").depends_on == [
        "alpha.alpha.work",
        "alpha.beta.work",
        "beta.alpha.work",
        "beta.beta.work",
    ]


def test_asset_rendering_is_reviewed_before_publication(setup):
    from core.delivery_prepare_writer import _checkpoint_authority_matches_draft
    from agents.delivery_hierarchical_preparation_agent import _PREPARATION_RULES

    example = json.loads(
        _PREPARATION_RULES.split("Return exactly these proposal fields, with no other fields:\n")[1].split(
            "\nUse empty lists"
        )[0]
    )
    assert example["units"][0]["asset_paths"] == []
    assert "Do not handwrite asset-root sections" in _PREPARATION_RULES

    root, task, cfg = setup
    (root / "assets").mkdir()
    (root / "assets/guide.png").write_bytes(b"Exact reference.\n")
    task.write_text(SOURCE + "\n## Assets\n\n### Reference assets\n\n- Path: `assets/guide.png`\n", encoding="utf-8")

    def assets(stage, packet):
        if stage == "author":
            value = proposal(
                packet["node_id"], [f["id"] for f in packet["source_fragments"]], branch=packet["node_id"] == "root"
            )
            value["units"][0]["asset_paths"] = ["assets/guide.png"]
            return json.dumps(value)

    llm = TreeLLM(depth=1, hook=assets)
    draft = prepare(setup, llm)
    assert _checkpoint_authority_matches_draft(draft)
    for stage, packet in llm.calls:
        if stage == "review":
            assert "assets/guide.png" in packet["proposal"]["units"][0]["task_markdown"]
    written = write_delivery_prepare_artifacts(
        draft,
        output_dir=".sikula/delivery/hierarchy",
        project_root=root,
        project_config=cfg,
        source_task_description=task.read_text(encoding="utf-8"),
        source_task_path=task,
    )
    assert written.prepared, written
    import yaml

    data = yaml.safe_load((root / ".sikula/delivery/hierarchy/plan.yaml").read_text(encoding="utf-8"))
    assert data["final_gate_authority"]


def test_crlf_source_reuses_exact_normalized_authority(setup):
    root, task, cfg = setup
    task.write_bytes(SOURCE.replace("\n", "\r\n").encode("utf-8"))
    llm = TreeLLM(depth=1)
    first = prepare(setup, llm)
    count = len(llm.calls)
    assert prepare(setup, llm) == first
    assert len(llm.calls) == count


def test_known_source_size_limit_preempts_initial_routing_call(setup):
    root, task, cfg = setup
    task.write_text("x" * (128 * 1024 + 1), encoding="utf-8")
    llm = TreeLLM()
    with pytest.raises(DeliveryAuthoringParseError) as exc:
        prepare(setup, llm)
    assert exc.value.code.endswith("hierarchy_required")
    assert not llm.calls


@pytest.mark.parametrize("change", ["unchanged", "changed", "missing", "missing_record"])
def test_bounded_local_context_is_checked_and_bound_across_resume(setup, change):
    root, task, cfg = setup
    (root / "src/api.py").write_bytes(b"API = 'existing'\n")

    def request(stage, packet):
        if stage == "author" and packet["node_id"] == "alpha" and "repository_evidence" not in packet:
            return json.dumps({"context_paths": ["src/api.py"]})

    llm = TreeLLM(depth=1, hook=request)
    prepare(setup, llm)
    packets = [p for stage, p in llm.calls if p.get("node_id") == "alpha" and stage == "review"]
    assert packets[0]["repository_evidence"]["files"][0]["text"] == "API = 'existing'\n"
    assert all("repository_evidence" not in p for stage, p in llm.calls if p.get("node_id") == "root")
    count = len(llm.calls)
    if change == "changed":
        (root / "src/api.py").write_bytes(b"API = 'different'\n")
    elif change == "missing":
        (root / "src/api.py").unlink()
    elif change == "missing_record":
        next(preparation_directory(root, "hierarchy").glob("context-*.json")).unlink()
    if change == "unchanged":
        prepare(setup, llm)
    else:
        with pytest.raises(DeliveryAuthoringParseError):
            prepare(setup, llm)
    assert len(llm.calls) == count


def test_unavailable_required_context_preempts_calls_and_can_be_restored(setup):
    root, task, cfg = setup

    def request(stage, packet):
        if stage == "author" and packet["node_id"] == "root" and "repository_evidence" not in packet:
            return json.dumps({"context_paths": ["src/api.py"]})

    llm = TreeLLM(depth=1, hook=request)
    with pytest.raises(DeliveryAuthoringParseError) as exc:
        prepare(setup, llm)
    assert exc.value.code.endswith("hierarchy_evidence_unavailable")
    assert len(llm.calls) == 2
    (root / "src/api.py").write_bytes(b"API = 'existing'\n")
    prepare(setup, llm)
    assert (
        sum(stage == "author" and p["node_id"] == "root" and "repository_evidence" not in p for stage, p in llm.calls)
        == 1
    )


@pytest.mark.parametrize("budget", ["node", "overall", "interrupted_exchange"])
def test_persistent_exchange_reservations_cannot_be_refilled(setup, monkeypatch, budget):
    import core.delivery_preparation_session as sessions

    root, task, cfg = setup
    calls = []

    def generate(prompt):
        calls.append(prompt)
        if budget == "interrupted_exchange":
            raise KeyboardInterrupt
        return "{}"

    monkeypatch.setattr(sessions, "MAX_NODE_CALLS", 2 if budget == "node" else 12)
    monkeypatch.setattr(sessions, "MAX_PREPARATION_CALLS", 2 if budget == "overall" else 100)
    directory = preparation_directory(root, "budget")
    for index in range(3):
        session = sessions.PreparationSession(root, directory, "same", generate, None, lambda: None)
        node = str(index) if budget == "overall" else "root"
        prompt = "same" if budget == "interrupted_exchange" else str(index)
        if index == 2:
            with pytest.raises(DeliveryAuthoringParseError) as exc:
                session.exchange(node, "author", prompt)
            assert exc.value.code.endswith("hierarchy_budget_exhausted")
        elif budget == "interrupted_exchange":
            with pytest.raises(KeyboardInterrupt):
                session.exchange(node, "author", prompt)
        else:
            session.exchange(node, "author", prompt)
    assert len(calls) == 2
