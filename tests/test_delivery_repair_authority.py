from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from agents.delivery_repair_authority_agent import (
    DeliveryRepairAuthorityAgent,
    build_authority_renewal_prompt,
    parse_authority_renewal,
)
from agents.delivery_repair_agent import DeliveryRepairAuthoringError
from core.delivery_repair_authority import build_renewal, apply_accepted_renewal, renewal_nodes
from tests.test_delivery_nested_checkpoints import _prepared


class AuthorityLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.packets = []

    def run_readonly_agent(self, prompt, cwd):
        packet, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"after":') :])
        self.calls.append(prompt)
        self.packets.append(packet)
        response = self.responses.pop(0) if self.responses else "approved"
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(packet, cwd)
        return json.dumps(
            {
                "schema_version": 1,
                "packet_fingerprint": packet["packet_fingerprint"],
                "disposition": response,
                "summary": "Preserve the failure case explicitly.",
            }
        )


@pytest.fixture(params=["lf", "crlf"])
def proposal(tmp_path, request):
    from core.delivery_repair import _appended_plan

    path, cfg, checked, *_ = _prepared(tmp_path)
    old = yaml.safe_load(path.read_text())
    if request.param == "crlf":
        for item in old["units"]:
            contract = tmp_path / item["task_path"]
            contract.write_bytes(contract.read_text().replace("\n", "\r\n").encode("utf-8"))
    unit = {
        **old["units"][0],
        "id": "integration-repair-aaaaaaaaaaaaaaaa",
        "task_path": ".sikula/delivery/team-invites/units/integration-repair-aaaaaaaaaaaaaaaa.md",
        "depends_on": ["foundation"],
        "repair_node": "storage",
    }
    unit.pop("disposition", None)
    content = (tmp_path / old["units"][0]["task_path"]).read_text()
    state = {
        "source_plan": path.read_text(),
        "contracts": {old["units"][0]["task_path"]: content},
        "task_markdown": content,
        "unit_id": "integration-repair-aaaaaaaaaaaaaaaa",
        "node_id": "storage",
        "attempts": 1,
        "verification": {"candidate_commit": "a" * 40},
        "config_fingerprint": "b" * 64,
        "authority_renewal": {"nodes": renewal_nodes(checked.plan, "storage"), "status": "pending", "reviews": {}},
    }
    data = _appended_plan(
        old,
        unit,
        ["deliver-team-invites"],
        tmp_path,
        cfg,
        str(path),
        node_id="storage",
        completed_unit_ids=frozenset({"foundation", "consumer", "shipping"}),
    )
    return path, cfg, state, data


def test_renewal_requires_independent_exact_approvals_and_restores_all_receipts(proposal):
    from core.delivery_plan import check_delivery_plan_file

    path, cfg, state, data = proposal
    root = Path(cfg["project"]["root_path"])
    packets, renewed = build_renewal(state, data, root, root, cfg, str(path))
    assert [packet["node_id"] for packet in packets] == ["storage", "integration", "root"]
    assert apply_accepted_renewal(state, data, root, root, cfg, str(path)) == data
    for packet in packets:
        result = AuthorityLLM().run_readonly_agent(build_authority_renewal_prompt(packet, cfg, root), root)
        state["authority_renewal"]["reviews"][packet["node_id"]] = {
            "attempts": 1,
            "fingerprint": packet["packet_fingerprint"],
            "assessment": json.loads(result),
        }
    state["authority_renewal"]["status"] = "accepted"
    assert apply_accepted_renewal(state, data, root, root, cfg, str(path)) == renewed
    changed = deepcopy(state)
    changed["task_markdown"] += "\nDifferent contract."
    with pytest.raises(ValueError, match="reservation does not match"):
        apply_accepted_renewal(changed, data, root, root, cfg, str(path))
    (root / ".sikula/delivery/team-invites/units/integration-repair-aaaaaaaaaaaaaaaa.md").write_text(
        state["task_markdown"]
    )
    path.write_bytes(renewed)
    checked = check_delivery_plan_file(path, project_root=root)
    assert checked.valid, checked.errors
    assert set(checked.plan.verified_checkpoint_authority) == {"storage", "integration"}
    assert set(checked.plan.verified_composition_authority) == {"integration"}
    assert checked.plan.verified_final_gate_authority


@pytest.mark.parametrize("change", ["fingerprint", "fields", "duplicate", "surrogate", "oversized"])
def test_renewal_parser_rejects_unbound_or_malformed_decisions(change):
    value = {
        "schema_version": 1,
        "packet_fingerprint": "sha256:" + "a" * 64,
        "disposition": "approved",
        "summary": "Checked.",
    }
    if change == "fingerprint":
        value["packet_fingerprint"] = "sha256:" + "b" * 64
    elif change == "fields":
        value["unknown"] = True
    elif change == "surrogate":
        value["summary"] = "\ud800"
    elif change == "oversized":
        value["summary"] = "a" * 2049
    output = json.dumps(value)
    if change == "duplicate":
        output = output[:-1] + ',"disposition":"approved"}'
    with pytest.raises(DeliveryRepairAuthoringError):
        parse_authority_renewal(output, "sha256:" + "a" * 64)


def test_composing_renewal_does_not_inline_leaf_contract_or_history(proposal):
    path, cfg, state, data = proposal
    root = Path(cfg["project"]["root_path"])
    state["task_markdown"] += "\nPRIVATE_LEAF_CONTRACT\n" + "detail " * 4000
    packets, _ = build_renewal(state, data, root, root, cfg, str(path))
    assert "PRIVATE_LEAF_CONTRACT" in json.dumps(packets[0])
    for packet in packets[1:]:
        assert "PRIVATE_LEAF_CONTRACT" not in json.dumps(packet)
        assert "integration-repair-aaaaaaaaaaaaaaaa" not in packet["contracts"]
        assert packet["renewed_child"]["packet_fingerprint"]


def test_renewal_binds_rule_contents_and_normalizes_contract_fingerprints(proposal):
    from core.delivery_authority import authority_contract_fingerprint

    path, cfg, state, data = proposal
    root = Path(cfg["project"]["root_path"])
    rules = root / "review-rules.md"
    cfg["reviewer"] = {"extra_rules": "review-rules.md"}
    rules.write_text("Check negative cases.\n")
    before, _ = build_renewal(state, data, root, root, cfg, str(path))
    rules.write_text("Check negative cases and exact literals.\n")
    after, _ = build_renewal(state, data, root, root, cfg, str(path))
    assert all(left["packet_fingerprint"] != right["packet_fingerprint"] for left, right in zip(before, after))
    assert authority_contract_fingerprint(b"task\r\n") == authority_contract_fingerprint(b"task\n")


def test_renewal_respects_configured_private_contract_directories(proposal):
    path, cfg, state, data = proposal
    root = Path(cfg["project"]["root_path"])
    cfg["tasks"] = {"state_dir": ".sikula/delivery/team-invites/units"}
    with pytest.raises(ValueError, match="private artifacts"):
        build_renewal(state, data, root, root, cfg, str(path))


def test_renewal_prompt_stays_bounded_as_unrelated_subtree_source_grows(tmp_path):
    from core.delivery_obligations import delivery_authority_fragments
    from core.delivery_repair import _appended_plan
    from tests.test_delivery_nested_checkpoints import _data, SOURCE
    from tests.test_delivery_authority import _prepare

    sizes = []
    for count in (1, 100):
        root = tmp_path / str(count)
        source = (
            SOURCE + "\n\n## Separate subsystem\nUNRELATED_AUTHORITY " + "Other implementation detail. " * count + "\n"
        )
        draft = _data(source)
        draft["units"].append({**draft["units"][0], "id": "separate", "title": "Separate subsystem", "depends_on": []})
        fragment = delivery_authority_fragments(source)[-1]
        # _data maps the last fragment to the consumer. Restore that requirement
        # to its original fragment and assign the added authority to the sibling.
        prior_fragment = delivery_authority_fragments(source)[-2]
        visible = next(item for item in draft["obligations"] if item["id"] == "visible")
        visible["source_fragment_ids"] = [prior_fragment.id]
        for record in draft["source_accounting"]:
            if record["source_fragment_id"] == prior_fragment.id:
                record.update(disposition="mapped", obligation_ids=["visible"])
            if record["source_fragment_id"] == fragment.id:
                record.update(
                    disposition="mapped",
                    obligation_ids=["separate-outcome"],
                    checkpoint_ids=["separate-node"],
                    final_gate=False,
                )
        draft["obligations"].append(
            {
                "id": "separate-outcome",
                "summary": "Separate behavior remains available.",
                "source_fragment_ids": [fragment.id],
                "unit_ids": ["separate"],
                "disposition": "preserved",
            }
        )
        draft["checkpoints"].append(
            {
                "id": "separate-node",
                "unit_ids": ["separate"],
                "obligation_ids": ["separate-outcome"],
                "integration_context": "Separate public API.",
            }
        )
        draft["units"][-2]["depends_on"].append("separate")
        path, cfg, checked, *_ = _prepare(root, source, draft, verification={"final_gate_authority_complete": True})
        old = yaml.safe_load(path.read_text())
        unit = {
            **old["units"][0],
            "id": "integration-repair-aaaaaaaaaaaaaaaa",
            "task_path": ".sikula/delivery/team-invites/units/integration-repair-aaaaaaaaaaaaaaaa.md",
            "depends_on": ["foundation"],
            "repair_node": "storage",
        }
        text = (root / old["units"][0]["task_path"]).read_text()
        state = {
            "source_plan": path.read_text(),
            "contracts": {old["units"][0]["task_path"]: text},
            "task_markdown": text,
            "unit_id": unit["id"],
            "node_id": "storage",
            "attempts": 1,
            "verification": {"candidate_commit": "a" * 40},
            "config_fingerprint": "b" * 64,
            "authority_renewal": {"nodes": renewal_nodes(checked.plan, "storage")},
        }
        data = _appended_plan(
            old,
            unit,
            ["deliver-team-invites"],
            root,
            cfg,
            str(path),
            node_id="storage",
            completed_unit_ids=frozenset(item.id for item in checked.plan.units),
        )
        packets, _ = build_renewal(state, data, root, root, cfg, str(path))
        assert len(packets) == 3
        prompts = [build_authority_renewal_prompt(packet, cfg, root) for packet in packets]
        assert all("UNRELATED_AUTHORITY" not in prompt for prompt in prompts)
        sizes.append([len(prompt.encode("utf-8")) for prompt in prompts])
    assert sizes[0] == sizes[1]


@pytest.fixture
def repairable(tmp_path):
    from tests.test_delivery_nested_checkpoints import nested_plan
    from tests.test_delivery_authority_flow import _run
    from tests.test_delivery_recursive_reverification import _gap
    from tests.test_delivery_repair import _LLM

    path, cfg = nested_plan.__wrapped__(tmp_path)
    result, _ = _run(path, cfg, _LLM(_gap({"deliver-team-invites"})))
    assert result.stop_code == "delivery_verification.repair_required", result
    return path, cfg


def test_independent_renewal_resumes_without_reauthoring_or_repeating_accepted_nodes(repairable):
    from core.delivery_repair import _read_state
    from core.delivery_progress import get_delivery_status
    from tests.test_delivery_repair import _LLM, _draft, _repair

    path, cfg = repairable
    root = Path(cfg["project"]["root_path"])
    before = get_delivery_status(path)
    author = _LLM(_draft(markdown=(root / before.plan.units[0].task_path).read_text()))
    reviewer = AuthorityLLM("approved", KeyboardInterrupt())

    def factory():
        return DeliveryRepairAuthorityAgent(reviewer)

    with pytest.raises(KeyboardInterrupt):
        _repair(path, cfg, author, node_id="storage", authority_agent_factory=factory)
    control = _read_state(root, before.plan.plan_id, "storage")
    assert control["phase"] == "prepared"
    assert control["authority_renewal"]["reviews"]["storage"]["assessment"]["disposition"] == "approved"
    assert get_delivery_status(path).plan == before.plan
    from core.delivery_repair import _state_path
    from core.delivery_repair_storage import write_repair_state

    corrupted = deepcopy(control)
    corrupted["authority_renewal"]["reviews"]["storage"]["assessment"]["packet_fingerprint"] = "sha256:" + "0" * 64
    state_path = _state_path(root, before.plan.plan_id, "storage")
    write_repair_state(root, state_path, corrupted)
    blocked = _repair(path, cfg, _LLM(), node_id="storage", authority_agent_factory=factory)
    assert blocked.issue.code == "delivery_repair.evidence_unavailable"
    assert len(reviewer.calls) == 2
    write_repair_state(root, state_path, control)
    unused = _LLM()
    recovered = _repair(path, cfg, unused, node_id="storage", authority_agent_factory=factory)
    assert recovered.ready, recovered
    assert not unused.calls and len(author.calls) == 1
    assert [packet["node_id"] for packet in reviewer.packets] == ["storage", "integration", "integration", "root"]
    after = get_delivery_status(path)
    assert after.plan.verified_final_gate_authority
    assert after.plan.verified_composition_authority.keys() == {"integration"}
    assert after.plan.verified_checkpoint_authority.keys() == {"integration", "storage"}
    assert after.verification_status != "passed"


def test_independent_rejection_uses_remaining_authoring_budget(repairable):
    from core.delivery_progress import get_delivery_status
    from tests.test_delivery_repair import _LLM, _draft, _repair

    path, cfg = repairable
    status = get_delivery_status(path)
    markdown = (Path(status.project_root) / status.plan.units[0].task_path).read_text()
    author = _LLM(_draft(markdown=markdown), _draft(markdown=markdown + "\nExplicit failure coverage.\n"))
    reviewer = AuthorityLLM("rejected")
    result = _repair(
        path, cfg, author, node_id="storage", authority_agent_factory=lambda: DeliveryRepairAuthorityAgent(reviewer)
    )
    assert result.ready, result
    assert len(author.calls) == 2
    assert "Preserve the failure case explicitly." in author.calls[1]
    assert [packet["node_id"] for packet in reviewer.packets] == ["storage", "storage", "integration", "root"]


def test_rejected_authority_feedback_is_sized_in_shared_preflight(repairable, monkeypatch):
    from agents import delivery_repair_agent
    from core import delivery_repair as module
    from core.delivery_progress import get_delivery_status
    from tests.test_delivery_repair import _LLM, _draft, _repair

    path, cfg = repairable
    status = get_delivery_status(path)
    root = Path(status.project_root)
    author = _LLM(_draft(markdown=(root / status.plan.units[0].task_path).read_text()))
    reviewer = AuthorityLLM("rejected")

    def factory():
        if author.calls:
            raise KeyboardInterrupt()
        return delivery_repair_agent.DeliveryRepairAgent(author)

    with pytest.raises(KeyboardInterrupt):
        module.coordinate_delivery_repair(
            path,
            cfg,
            project_root=root,
            node_id="storage",
            agent_factory=factory,
            authority_agent_factory=lambda: DeliveryRepairAuthorityAgent(reviewer),
        )
    state_path = module._state_path(root, status.plan.plan_id, "storage")
    before = state_path.read_bytes()
    state = module._read_state(root, status.plan.plan_id, "storage")
    assert state["phase"] == "authoring" and state["attempts"] == 1
    assert state["authority_feedback"]
    # The original prompt fits exactly; the additional review feedback does not.
    limit = len(author.calls[0].encode("utf-8"))
    monkeypatch.setattr(
        delivery_repair_agent,
        "delivery_verification_prompt_is_bounded",
        lambda prompt: len(prompt.encode("utf-8")) <= limit,
    )
    unused = _LLM()
    for dry_run in (True, False):
        result = _repair(path, cfg, unused, node_id="storage", dry_run=dry_run)
        assert not result.ready and result.issue.code == "delivery_repair.hierarchy_required"
        assert state_path.read_bytes() == before
        assert not unused.calls


@pytest.mark.parametrize("failure", ["format", "unchanged", "stale"])
def test_renewal_failure_does_not_publish_or_advertise_resumable_success(repairable, failure):
    from core.delivery_progress import get_delivery_status
    from core.delivery_repair import _read_state
    from tests.test_delivery_repair import _LLM, _draft, _repair

    path, cfg = repairable
    status = get_delivery_status(path)
    root = Path(status.project_root)
    markdown = (root / status.plan.units[0].task_path).read_text()
    author = _LLM(_draft(markdown=markdown), _draft(markdown=markdown))

    def stale(packet, cwd):
        path.write_bytes(path.read_bytes() + b"\n# concurrent plan update\n")
        return json.dumps(
            {
                "schema_version": 1,
                "packet_fingerprint": packet["packet_fingerprint"],
                "disposition": "approved",
                "summary": "Checked.",
            }
        )

    reviewer = AuthorityLLM(
        *([lambda *_: "invalid"] * 2 if failure == "format" else ["rejected"] if failure == "unchanged" else [stale])
    )
    result = _repair(
        path, cfg, author, node_id="storage", authority_agent_factory=lambda: DeliveryRepairAuthorityAgent(reviewer)
    )
    assert not result.ready
    assert (
        result.issue.code
        == {
            "format": "delivery_repair.output_invalid",
            "unchanged": "delivery_repair.authority_rejected",
            "stale": "delivery_repair.stale",
        }[failure]
    )
    calls = len(reviewer.calls)
    repeated = _repair(path, cfg, _LLM(), node_id="storage", dry_run=True)
    assert not repeated.ready and len(reviewer.calls) == calls
    if failure == "format":
        assert repeated.issue.code == "delivery_repair.authority_budget_exhausted"
        assert len(author.calls) == 1 and calls == 2
    if failure == "unchanged":
        assert len(author.calls) == 2 and calls == 1
    assert _read_state(root, status.plan.plan_id, "storage")["phase"] != "published"


def test_cli_renewal_uses_reviewer_configuration(monkeypatch):
    import argparse
    import sikula
    from core import llm_client

    captured = []

    def client(config):
        captured.append(config)
        return AuthorityLLM()

    monkeypatch.setattr(llm_client, "create_llm_client", client)
    cfg = {
        "llm": {"provider": "codex", "model": "default"},
        "agents": {
            "reviewer": {"llm": {"provider": "claude", "model": "independent-review"}},
            "delivery_preparer": {"llm": {"provider": "codex", "model": "author"}},
        },
    }
    agent = sikula._create_delivery_repair_authority_agent(argparse.Namespace(), cfg)
    assert isinstance(agent, DeliveryRepairAuthorityAgent)
    assert captured[0].provider == "claude" and captured[0].model == "independent-review"


@pytest.mark.parametrize("boundary", ["readonly_mutation", "security_stop", "external_dependency_gap"])
@pytest.mark.parametrize("unavailable", [False, True])
def test_workspace_failure_cannot_replace_a_known_boundary(tmp_path, monkeypatch, boundary, unavailable):
    from core import delivery_repair as module
    from core.delivery_repair_storage import read_repair_state

    code = "delivery_repair." + boundary
    state = {"phase": "blocked", "stop_code": code}
    path = tmp_path / "state.json"

    def changed(*args):
        if unavailable:
            raise OSError("workspace unavailable")
        return False

    monkeypatch.setattr(module, "_review_workspace_unchanged", changed)
    with pytest.raises(module.DeliveryRepairError) as error:
        module._assert_readonly(tmp_path, {}, None, "a" * 40, tmp_path, path, state)
    assert error.value.issue.code == code
    assert read_repair_state(tmp_path, path) == state


@pytest.mark.parametrize(
    "boundary,audit_fails", [("readonly_mutation", False), ("readonly_mutation", True), ("security_stop", True)]
)
def test_renewal_boundary_survives_audit_failure_and_blocks_resume(repairable, monkeypatch, boundary, audit_fails):
    from core import delivery_repair as module
    from core.delivery_progress import get_delivery_status
    from core.llm_client import LLMReadOnlyViolation
    from tests.test_delivery_repair import _LLM, _draft, _repair

    path, cfg = repairable
    status = get_delivery_status(path)
    root = Path(status.project_root)
    author = _LLM(_draft(markdown=(root / status.plan.units[0].task_path).read_text()))
    reviewer = AuthorityLLM(LLMReadOnlyViolation("mutation") if boundary == "readonly_mutation" else boundary)
    original = module._append_audit

    def audit(path, value, **kwargs):
        if audit_fails and value["event"] == "authority_review_finished":
            raise OSError("audit unavailable")
        return original(path, value, **kwargs)

    monkeypatch.setattr(module, "_append_audit", audit)
    result = _repair(
        path, cfg, author, node_id="storage", authority_agent_factory=lambda: DeliveryRepairAuthorityAgent(reviewer)
    )
    assert result.issue.code == "delivery_repair." + boundary
    control = module._read_state(root, status.plan.plan_id, "storage")
    assert control["phase"] == "blocked" and control["stop_code"] == result.issue.code
    again = _repair(path, cfg, _LLM(), node_id="storage", dry_run=True)
    assert again.issue.code == result.issue.code
    assert len(reviewer.calls) == 1
    assert get_delivery_status(path).plan == status.plan
