"""Read-only local authoring and independent checks for hierarchical preparation."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, TYPE_CHECKING

from agents.base_agent import AGENT_SECURITY_PREFIX, read_only_agent_prompt
from core.delivery_authority import authority_digest
from core.delivery_authoring import DeliveryAuthoringDraft, DeliveryAuthoringParseError
from core.delivery_obligations import delivery_authority_fragment_map
from core.delivery_obligations import MAX_DELIVERY_OBLIGATIONS
from core.delivery_plan import MAX_DELIVERY_CONSTRAINTS
from core.delivery_verification import MAX_DELIVERY_VERIFICATION_ACTIVE_UNITS, MAX_DELIVERY_VERIFICATION_SOURCE_BYTES
from core.delivery_preparation_tree import (
    AcceptedPreparation,
    MAX_PREPARATION_DEPTH,
    MAX_PREPARATION_NODES,
    MAX_PREPARATION_PACKET_BYTES,
    MAX_PREPARATION_RESPONSE_BYTES,
    MAX_PREPARATION_REVIEW_FEEDBACK_BYTES,
    parse_node,
    parse_local_review,
    PreparationProtocolError,
    PreparationDraftCorrection,
    remap_source_references,
    failure,
    assemble_preparation,
)
from core.delivery_preparation_session import PreparationSession
from core.delivery_progress import DeliveryProgressLock, DeliveryProgressLockError
from core.delivery_repair_storage import read_repair_state, write_repair_state

if TYPE_CHECKING:
    from agents.delivery_preparation_agent import DeliveryPreparationAgent


_LOCAL_STRUCTURE_RULES = """Local preparation structure (shared by authoring and independent review):
scope_contract defines this node's assigned contribution. All supplied source authority constrains that
contribution; broader context and root preparation guidance do not require duplicating other groups'
work or creating another hierarchy at every node. Prefer direct units for small self-contained work.
Use children for meaningful intermediate integration boundaries or to keep larger groups bounded.
Return only this node's immediate declarations. The host calls each child separately to author its
contents later. Never embed children or units inside a child declaration; describe any required nested
decomposition in its integration_context. Direct work belongs only in units, never duplicated in
children. IDs must be unique across the local children and units lists.
Source fragment IDs such as f1 are exact host-assigned references, stable across the tree. Only the
supplied local references are valid; the host maps them back to canonical source IDs for publication.
Local owner/dependency references expand to all units of the named child group. The host prefixes IDs;
root is reserved for final integration. Siblings do not own each other's work.
node_role is final_gate only at root; every other node is already a checkpoint. Its immediate children
are nested under that checkpoint. Do not add another wrapper to make those children nested.
Every child becomes an intermediate checkpoint with its integration_context. A dependency on a child
requires that child's accepted checkpoint, not just completion of its units. There is no separate
checkpoint field to author. Do not add redundant integration work merely to create a checkpoint.
Flat checkpoints must include their prerequisites. Prefer independent root children; for dependent
components, place them under a common child with nested checkpoints and keep their downstream consumer
at root, or use direct root units for consumers. Disjoint dependent flat sibling checkpoints are invalid.
dependency_interfaces are inherited prerequisites: the host carries their interfaces through every
preparation level and attaches their dependencies to EVERY descendant unit. Their IDs are qualified by
the declaring node (for example root.storage), distinct from new local IDs. These exact IDs are
deliberately absent from normalized local depends_on lists. Do not require adding
them there; verify that contracts correctly use the supplied interfaces. Other dependencies and all
ownership references must be local. An absent depends_on defaults to []; explicit null is invalid.
Only IDs actually listed in dependency_interfaces are inherited. When that list is empty, no dependency
is implicit: dependencies between local children/units must be stated in their depends_on lists.
There are two different contract boundaries:
- A child declaration delegates preparation, not execution. The child receives the exact selected
  source_fragments, integration_context and dependency_interfaces and authors its contracts later.
  Check complete source delegation and the promised contribution; integration_context need not repeat
  selected source text or contain the child's future implementation contracts.
- A direct unit is a runnable implementation contract. Its task_markdown must retain the exact source
  requirements governing its contribution; execution cannot read this parent preparation packet.
  It may use declared prerequisite results/codebase, but must say which behavior it consumes/preserves.
Shared decisions need an accountable owner and compatible consumers. When source authority leaves a
choice open, one owner may decide it within scope and dependent consumers must adopt that decision.
Independent owners must not make conflicting assumptions. A source-defined rule cannot be delegated
away as an implementation choice, and a missing external prerequisite remains a stop.
Parent review owns cross-child/global integration, children own their assigned contribution.

"""

_PREPARATION_RULES = (
    _LOCAL_STRUCTURE_RULES
    + """You are Sikula's read-only hierarchical delivery preparation assistant.
Do not run commands, access the network or modify files. Use only supplied source and project evidence.
Do not invent replacement dependencies, new product requirements or work outside the authorized scope.
If repository evidence is needed, you may request it once for this node by returning only
{"context_paths":["src/relevant-file.py"]}. Request at most 8 existing project files, within the
configured read scope. The host supplies a bounded read-only result; never invent unavailable evidence.
A missing external prerequisite must return {"kind":"stop_and_follow_up"}; do not propose its substitute.
Public titles/requirement summaries must paraphrase source without private excerpts, paths or secrets.
Exact source literals belong in private unit Markdown and integration interfaces only.
New unit and child IDs are local stable lowercase slugs (letters/digits/hyphens/underscores).
Choose short names: the full name including ancestor prefixes must fit the checkpoint ID limit
(80 characters). Do not use dots in local IDs; the host adds the namespace separators.
Obligation and constraint IDs may use letters, digits, hyphens and underscores; use concise descriptive
names. Do not use dots: the host reserves them to separate hierarchical namespaces.
Every child must have a downstream consumer, locally or inherited from an enclosing group.
Top-level children need consumers among the root's direct units or sibling groups.
Do not invent integration work merely to create a checkpoint; use direct units for work without consumers.
Author at most 8 direct units and 8 immediate children per exchange. Split larger groups recursively.
A group must have direct work or at least two children. Every child must ultimately own units and outcomes.
A composing group may delegate all its outcomes to children; do not invent redundant local obligations.
Never include grandchildren's contracts or results in a parent exchange.
If feedback is nonempty, this is a correction of previous_proposal, not a fresh decomposition.
Resolve EVERY named defect in that unapproved proposal under the same source and scope contract.
Return the complete corrected proposal, not an explanation or patch. Keep unrelated valid work intact.
Feedback cannot override source authority or prerequisite stops. Correct genuine omissions/conflicts;
when feedback mistakes already delegated authority for an omission, make its ownership and consumer
agreement explicit without inventing a new requirement or duplicating another owner's work.

Return exactly these proposal fields, with no other fields:
{"title":"Safe group title", "children":[{"id":"storage","integration_context":"Exact bounded behavior and integration interface promised by this group", "source_fragment_ids":["f1"], "depends_on":[]}],
"units":[{"id":"consumer","title":"Safe unit title","depends_on":["storage"],"scope_paths":["src/"],"asset_paths":[],"task_markdown":"Complete implementation contract", "budget":{"max_planner_steps":1}}],
"constraints":[{"id":"ownership","kind":"repository_ownership","summary":"Safe paraphrase","unit_ids":["storage","consumer"],"disposition":"preserved"}],
"obligations":[{"id":"integrated","summary":"Safe outcome paraphrase","source_fragment_ids":["f1"],"unit_ids":["storage","consumer"],"disposition":"preserved"}],
"source_accounting":[{"source_fragment_id":"f1","disposition":"mapped","obligation_ids":["integrated"],"constraint_ids":["ownership"],"child_ids":["storage"],"rationale":"Private explanation"}]}
Use empty lists when appropriate. Units use the existing delivery unit contract: Goal, Current behavior,
Desired behavior, Acceptance criteria, Out of scope, Security and privacy, Reviewer focus, Verification
sections, with runnable configured verification commands. Each unit is one implementation pass by default,
two tightly coupled steps at most. Preserve exact source values, required assets and sandbox scope.
Assign structured source assets through each unit's asset_paths: use only paths declared in the
source's canonical direct-list ## Assets section or their project-relative equivalents, with no
duplicates within a unit. Assign each asset to every unit needing it; use [] when none is needed.
Delegate the exact asset declarations and their enclosing context to children that own the relevant
work. Every source asset must ultimately be assigned to at least one relevant unit in the full tree.
Do not handwrite asset-root sections (## Assets, ## Asset, ## Task assets, ## Task asset),
## Asset manifest, or sikula:generated-* markers in task_markdown. The host renders exact assigned
source declarations before independent review and publication; reviewers must retain that rendering.
Use explicit ## section headings with nonempty content. Put configured command bullets directly under
## Verification. Do not introduce the commands with a standalone colon-ended line (which starts another
section in the contract format); use a normal sentence ending in a period if introductory prose is needed.
Constraint kinds are repository_ownership, authoritative_read_only_dependency, prohibited_fallback, security_boundary,
stop_and_follow_up. Dispositions are preserved, needs_review or conflict; do not hide authored blockers.
Include exactly one source_accounting record for EVERY supplied fragment, including contextual text.
A record is mapped if it has direct obligation/constraint references, otherwise delegated if it is sent
only to child groups, otherwise context_only. child_ids must exactly match children receiving the fragment.
For every record, obligation_ids must exactly equal the obligations whose source_fragment_ids include
that fragment, in BOTH directions. Do not add thematic mappings absent from obligation provenance.
Each child receives its exact source fragments, enclosing headings and a scope contract. Include shared
rules/prohibitions and all fragments governing its contribution, not only locally interesting statements.
Include the top-level task heading in every child receiving its enclosed content. Headings and background
sent to children use delegated accounting when they have no local obligation/constraint references.
Omissions cannot be justified by a child's summary. Parent obligations/constraints name direct units or
immediate children as owners. Pass their source authority to every contributing child. A parent reviews
cross-child/global integration itself; children independently establish their local contribution.
Rationales are private, nonempty and at most 2000 UTF-8 bytes; interfaces at most 4096 UTF-8 bytes.
"""
)

_REVIEW_RULES = (
    _LOCAL_STRUCTURE_RULES
    + """Independently assess this local proposal against ALL supplied exact source authority
and its incoming scope contract. This is review, not authoring.
If draft_validation_feedback is supplied, the host has found correctable draft defects. Assess the
proposal's other semantic gaps now so a single correction can address both kinds of findings. An approval
cannot waive those deterministic defects; corrected contracts still require independent approval.
Check completeness, shared/global rules, source attribution (including context_only), exact literals,
unit readiness, prerequisites, dependencies and compatible child promises. Apply the two contract
boundaries above: assess a child's selected exact source plus its scope together, while assessing a
direct unit's task_markdown as an execution contract. A delegated requirement need not be repeated in
the child's interface summary, but it must reach every affected child and direct unit. A shared choice
is unresolved when no owner can decide it or consumers are not bound to that owner's decision; it is
not unresolved merely because its implementation has not been prepared yet.
For a parent completion check, accepted child references establish exact checked local contributions;
assess their integration with the parent's direct work and retained obligations. Do not request or echo
all descendant contracts. A child's approval cannot replace the parent's own integration decision.
Reject a changed or unsatisfied promised interface. Return kind=revise with bounded actionable feedback
for an ordinary correctable gap; use kind=stop_and_follow_up for unresolved external prerequisites.
Collect all detectable local semantic gaps together. For each feedback item, name the affected local
child/unit or accounting record, cite the source fragment or incoming interface requiring the behavior,
and explain the missing authority, incompatible promise, unowned shared decision or incomplete unit
contract. Do not present an unspecified implementation choice as a source-mandated value.
When correction_feedback is supplied, reassess those findings against the corrected proposal and all
current authority. Keep a finding only if it remains unsupported or unresolved; additional genuine
defects must still block approval. Prior feedback is review context, not new source authority.
Return ONLY {"kind":"approved|revise|stop_and_follow_up", "input_fingerprint":"exact supplied fingerprint",
"feedback":"empty on approval, otherwise actionable private explanation (max 4096 UTF-8 bytes)"}.
Do not rewrite the supplied authority, proposal or child records.
"""
)


def preparation_directory(root: Path, plan_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", plan_id):
        raise failure("hierarchy_output_invalid")
    return root / ".sikula/state/delivery-preparation" / plan_id


def has_hierarchical_preparation(root: Path, plan_id: str) -> bool:
    directory = preparation_directory(root, plan_id)
    return any(
        read_repair_state(root, directory / name) is not None
        for name in ("selection.json", "control.json", "location.json")
    )


def prepare_hierarchy(
    agent: DeliveryPreparationAgent,
    *,
    task_description: str,
    task_path: str | Path,
    plan_id: str,
    project_root: Path,
    output_dir: str | Path,
    project_context: dict[str, Any] | None,
    audit_recorder: Any,
) -> DeliveryAuthoringDraft:
    source = task_description
    if len(source.encode("utf-8")) > MAX_DELIVERY_VERIFICATION_SOURCE_BYTES:
        raise failure("hierarchy_required")
    directory = preparation_directory(project_root, plan_id)
    guidelines = agent._guidelines_context(project_root)
    task = Path(task_path)
    if not task.is_absolute():
        task = project_root / task
    task_relative = agent._project_relative_path(task, project_root)
    if task_relative == "<outside-project>":
        raise failure("hierarchy_authority_changed")
    binding = authority_digest(
        {
            "policy": "hierarchical-preparation-v1",
            "instructions": {"author": _PREPARATION_RULES, "review": _REVIEW_RULES},
            "source": source,
            "task": task_relative,
            "output": str((project_root / output_dir).resolve()),
            "config": agent.project_config,
            "provider": {
                key: getattr(getattr(agent.llm, "_config", None), key, None)
                for key in ("provider", "model", "max_tokens", "temperature", "agent_timeout")
            },
            "context": project_context,
            "guidelines": guidelines,
        }
    )
    # Secure creation/check of the private directory precedes acquiring its exclusive lock.
    write_repair_state(project_root, directory / "location.json", {"policy": "hierarchical-preparation-v1"})
    try:
        with DeliveryProgressLock(directory / "lock", owner="delivery_prepare_hierarchy"):
            selected = read_repair_state(project_root, directory / "selection.json")
            control = read_repair_state(project_root, directory / "control.json")
            if selected is not None and selected != {"binding": binding}:
                raise failure("hierarchy_stale")
            # Session creation persists empty control before the selection
            # marker. Only that exact, unused state can finish initialization
            # after interruption; missing evidence after reservations still stops.
            initializing = selected is None and control == {
                "binding": binding,
                "calls": [],
                "terminal": None,
                "contexts": {},
            }
            if (selected is None) != (control is None) and not initializing:
                raise failure("hierarchy_evidence_unavailable")
            if selected is None and any(
                path.name not in {"location.json", "lock", "control.json"} for path in directory.iterdir()
            ):
                # Missing control is recoverable only before any work was saved.
                # Surviving evidence must never be overwritten by a fresh budget.
                raise failure("hierarchy_evidence_unavailable")

            def validate() -> None:
                if task.read_text(encoding="utf-8") != source or agent._guidelines_context(project_root) != guidelines:
                    raise failure("hierarchy_authority_changed")

            def read_context(paths: list[str]) -> dict[str, Any]:
                if agent.context_reader is not None:
                    return agent.context_reader(project_root, paths)
                from tools.delivery_context_tool import read_delivery_context

                return read_delivery_context(project_root, paths, agent.project_config)

            session = PreparationSession(
                project_root,
                directory,
                binding,
                lambda prompt: agent.llm.generate("", prompt),
                audit_recorder,
                validate,
                read_context,
            )
            write_repair_state(project_root, directory / "selection.json", {"binding": binding})
            runner = _HierarchyPreparation(session, project_root, source, project_context or {}, guidelines)
            tree = runner.node("root", tuple(runner.fragments), "Deliver the complete authoritative source task.", 0)
            session.validate_inputs()
            return assemble_preparation(
                tree,
                source=source,
                task_path=task_relative,
                plan_id=plan_id,
                root=project_root,
                output_dir=Path(output_dir),
            )
    except DeliveryProgressLockError:
        raise failure("hierarchy_locked") from None
    except DeliveryAuthoringParseError as exc:
        exc.phase = "delivery_prepare_hierarchy"
        raise
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise failure("hierarchy_evidence_unavailable") from None


class _HierarchyPreparation:
    def __init__(
        self, session: PreparationSession, root: Path, source: str, context: dict[str, Any], guidelines: str
    ) -> None:
        self.session, self.root, self.source = session, root, source
        self.context, self.guidelines = context, guidelines
        self.fragments = delivery_authority_fragment_map(source)
        self.source_references = {fragment: f"f{index}" for index, fragment in enumerate(self.fragments, start=1)}
        self.nodes: set[str] = set()
        self.proposals = {}

    def prompt(self, rules: str, packet: dict[str, Any]) -> str:
        prompt = read_only_agent_prompt(
            AGENT_SECURITY_PREFIX + rules + "\nINPUT JSON:\n" + json.dumps(packet, ensure_ascii=True, sort_keys=True)
        )
        # Include the bounded response/control allowance in preflight.
        # A feedback byte can expand to six ASCII bytes in JSON (e.g. a
        # control character). Include the complete response envelope as well.
        allowance = (
            len(
                json.dumps(
                    {
                        "kind": "stop_and_follow_up",
                        "input_fingerprint": "sha256:" + "0" * 64,
                        "feedback": "\0" * MAX_PREPARATION_REVIEW_FEEDBACK_BYTES,
                    },
                    ensure_ascii=True,
                ).encode("utf-8")
            )
            if rules == _REVIEW_RULES
            else MAX_PREPARATION_RESPONSE_BYTES
        )
        if len(prompt.encode("utf-8")) + allowance > MAX_PREPARATION_PACKET_BYTES:
            raise failure("hierarchy_required")
        return prompt

    def review(self, key: str, stage: str, packet: dict[str, Any]) -> str:
        fingerprint = authority_digest(packet)
        correction = {}
        for attempt in range(2):
            prompt = self.prompt(_REVIEW_RULES, {"input_fingerprint": fingerprint, **packet, **correction})
            output = self.session.exchange(key, stage, prompt)
            try:
                feedback = parse_local_review(output, fingerprint)
            except DeliveryAuthoringParseError as exc:
                self.session.assessment(key, stage, prompt, error=exc.code)
                if exc.code.endswith("hierarchy_authority_changed"):
                    self.session.stop("hierarchy_authority_changed")
                if isinstance(exc, PreparationProtocolError) and attempt == 0:
                    correction = {"protocol_correction": {"error_code": exc.code, "previous_response": output}}
                    continue
                raise
            self.session.assessment(key, stage, prompt, verdict="revise" if feedback else "approved")
            return feedback
        raise AssertionError("Bounded review did not return or raise")

    def node(
        self, key: str, fragments: tuple[str, ...], contract: str, depth: int, inputs: list[dict] | None = None
    ) -> AcceptedPreparation:
        self.nodes.add(key)
        if depth > MAX_PREPARATION_DEPTH or len(self.nodes) > MAX_PREPARATION_NODES:
            raise failure("hierarchy_required")
        references = {fragment: self.source_references[fragment] for fragment in fragments}
        packet = {
            "node_id": key,
            "node_role": "final_gate" if key == "root" else "checkpoint",
            "scope_contract": contract,
            "dependency_interfaces": inputs or [],
            "source_fragments": [{**self.fragments[f].to_prompt_dict(), "id": references[f]} for f in fragments],
            "project_context": self.context,
            "guidelines": self.guidelines,
        }
        feedback = ""
        previous = None
        for attempt in range(2):
            draft_feedback = ""
            draft_error = None
            prompt = self.prompt(_PREPARATION_RULES, {**packet, "feedback": feedback, "previous_proposal": previous})
            output = self.session.exchange(key, "author", prompt)
            retrieving_context = False
            try:
                from core.delivery_preparation_tree import object_output

                request = object_output(output)
                if set(request) == {"context_paths"}:
                    self.session.assessment(key, "author", prompt, verdict="context_requested")
                    retrieving_context = True
                    packet["repository_evidence"] = self.session.context(key, request["context_paths"])
                    prompt = self.prompt(
                        _PREPARATION_RULES, {**packet, "feedback": feedback, "previous_proposal": previous}
                    )
                    output = self.session.exchange(key, "author", prompt)
                    retrieving_context = False
                try:
                    node = parse_node(
                        output,
                        key=key,
                        source=self.source,
                        fragment_ids=fragments,
                        root=self.root,
                        source_references={alias: fragment for fragment, alias in references.items()},
                        prerequisite_ids=frozenset(item["id"] for item in inputs or []),
                    )
                except PreparationDraftCorrection as exc:
                    if attempt != 0:
                        raise
                    # Collect semantic findings before spending the one correction
                    # on independently detectable draft defects. This exception is
                    # available only after all non-retryable authority checks pass.
                    node, draft_feedback, draft_error = exc.node, exc.feedback, exc.code
                    for fragment, alias in references.items():
                        draft_feedback = draft_feedback.replace(fragment, alias)
                if node.proposal["children"] and depth == MAX_PREPARATION_DEPTH:
                    raise failure("hierarchy_required")
                if len(self.nodes | {key + "." + c["id"] for c in node.proposal["children"]}) > MAX_PREPARATION_NODES:
                    raise failure("hierarchy_required")
                self.proposals[key] = node
                if (
                    sum(len(n.units) for n in self.proposals.values()) > MAX_DELIVERY_VERIFICATION_ACTIVE_UNITS
                    or sum(len(n.obligations) for n in self.proposals.values()) > MAX_DELIVERY_OBLIGATIONS
                    or sum(len(n.constraints) for n in self.proposals.values()) > MAX_DELIVERY_CONSTRAINTS
                ):
                    raise failure("hierarchy_required")
            except DeliveryAuthoringParseError as exc:
                if not retrieving_context:
                    self.session.assessment(key, "author", prompt, error=exc.code)
                raise
            self.session.assessment(key, "author", prompt, error=draft_error, verdict=None if draft_error else "parsed")
            proposal = remap_source_references(node.proposal, references)
            review_packet = {**packet, "proposal": proposal}
            if feedback and proposal != previous:
                # Retain only the feedback that caused this correction, not an
                # accumulating review history. An unchanged proposal must reuse
                # its original assessment rather than try the reviewer again.
                review_packet["correction_feedback"] = feedback
            previous = proposal
            if draft_feedback:
                review_packet["draft_validation_feedback"] = draft_feedback
            feedback = self.review(key, "local_review", review_packet)
            if any(i.disposition != "preserved" for i in [*node.obligations, *node.constraints]):
                feedback = (
                    feedback
                    or "Resolve the authored needs_review/conflict dispositions without dropping their authority."
                )
            if draft_feedback:
                # Both inputs are independently bounded, with no accumulated
                # history. An approving review cannot clear deterministic defects.
                feedback = (
                    "Deterministic checks:\n"
                    + draft_feedback
                    + ("\nIndependent review:\n" + feedback if feedback else "")
                )
            if feedback:
                continue
            children = []
            for child in node.proposal["children"]:
                child_key = child["id"] if key == "root" else key + "." + child["id"]
                interfaces = {
                    **{
                        c["id"]: {"id": key + "." + c["id"], "integration_context": c["integration_context"]}
                        for c in node.proposal["children"]
                    },
                    **{u.id: {"id": key + "." + u.id, "task_markdown": u.task_markdown} for u in node.units},
                }
                children.append(
                    self.node(
                        child_key,
                        tuple(child["source_fragment_ids"]),
                        child["integration_context"],
                        depth + 1,
                        [*(inputs or []), *(interfaces[k] for k in child["depends_on"])],
                    )
                )
            if children:
                review_packet["accepted_children"] = [
                    {
                        "id": child.node.key,
                        "receipt": child.receipt,
                        "integration_context": proposal["integration_context"],
                    }
                    for child, proposal in zip(children, node.proposal["children"])
                ]
                feedback = self.review(key, "integration_review", review_packet)
                if feedback:
                    continue
            receipt = authority_digest({"proposal": node.proposal, "children": [c.receipt for c in children]})
            return AcceptedPreparation(node, tuple(children), receipt)
        raise failure("hierarchy_budget_exhausted")
