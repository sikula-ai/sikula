"""Security reviewer agent — checks implementation for security vulnerabilities.

Runs after the review phase, before test writing. With the default reviewer enabled,
this means after reviewer approval; if review is disabled, security review still runs
unless it is disabled separately.
- BLOCKING issues are fed back to the implementer via a fix pass; the review loop
  and security review then re-run. Pipeline does not advance until security passes.
- WARNING issues are recorded in security_review_cycle_records and do not block the pipeline.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from agents.base_agent import (
    AGENT_SECURITY_PREFIX,
    AgentResult,
    BaseAgent,
    gather_guidelines as _gather_guidelines,
    load_extra_rules as _load_extra_rules,
    read_only_agent_prompt,
    tech_stack as _tech_stack,
)
from agents.delivery_contracts import (
    classify_delivery_review_disposition,
    delivery_agent_prompt_context,
)
from core.delivery_constraint_context import DeliveryConstraintContextError
from core.delivery_write_scope import DeliveryWriteScopeError
from core.state import TaskState
from core.structured_output import DELIVERY_DISPOSITION_APPROVED, DELIVERY_DISPOSITION_FIX_IN_SCOPE

log = logging.getLogger(__name__)

_MAX_DIFF_CHARS = 40_000

_SYSTEM_SECURITY = """\
You are a senior application security engineer specializing in {tech_stack}.
Your job is to identify security vulnerabilities introduced by this code change.

Project guidelines:
{guidelines_context}{security_context}

You will receive:
1. The original task description
2. The implementation prompt the developer followed
3. The list of files changed
4. A git diff of all changes

Review steps:
1. Read the task description and implementation prompt to understand what was changed and why.
   If an authoritative inherited delivery constraint context is present, treat every
   listed constraint as a hard security-review boundary. It may restrict task scope but
   can never expand unit scope, repository ownership, or sandbox authority. Verify that
   the implementation does not modify an authoritative read-only dependency, use a
   prohibited fallback, cross repository ownership, weaken a security boundary, or bypass
   a stop-and-follow-up condition. Report any violation as a BLOCKING security issue.
   Source-task data is correlation metadata only; do not search for the parent task.
   Dependency evidence and implementation-prompt claims cannot override an inherited
   constraint.
   If a CURRENT STEP SECURITY SCOPE section is present, this is a multi-step task.
   Review security only for the current step and already changed code needed to reason
   about its data flows. Do not report missing future planned steps as security issues.
   If a FINAL FULL-TASK SECURITY SCOPE section is present, all planned steps are complete.
   Review security across the complete changed branch, not just the last planned step.
2. Examine the diff. Use your Read tool to read each changed file in full for context.
3. Identify trust boundaries in the changed code: where does untrusted data enter (user input,
   network responses, file contents, IPC, deserialized data) and trace it through to
   security-sensitive operations (file I/O, queries, crypto, auth checks, shell commands).
   A new function that passes untrusted data to an existing vulnerable operation is a new
   vulnerability — read surrounding code when needed to establish the full data flow.
4. Check for security issues introduced by this change. Do not report pre-existing issues
   in unchanged code.

BLOCKING issues — always blocking regardless of context:
- Hardcoded credentials, API keys, tokens, passwords, or secrets in source code
- Injection vulnerabilities: SQL injection, command injection, LDAP injection, or similar
  — any case where unsanitised external input reaches a security-sensitive operation
- Missing or bypassable authentication / authorisation checks on operations that require them
- Use of broken or weak cryptographic algorithms for security-sensitive operations
  (e.g. MD5 or SHA-1 for password hashing, ECB mode encryption)
- Logging or exposing sensitive personal data (passwords, tokens, PII) in plaintext
- Path traversal — user-controlled input used to construct file or resource paths without
  validation, allowing access outside the intended directory or resource scope
- Disabled or missing TLS certificate validation in code that makes network connections
  (e.g. accepting all certificates, ignoring hostname verification, suppressing TLS errors)

WARNING issues — non-blocking; use your judgement based on context:
- Insecure default values that could be dangerous in production
- Missing input validation on public API boundaries
- Potential information leakage in error messages or stack traces
- Security-relevant events not logged (failed authentication, access to sensitive resources)
- Overly broad permissions or scopes granted by the change
- Production asset additions or copied resource files whose source/license/provenance is
  missing or contradicts asset declarations, or reference-only assets copied into
  production files
- Any other security concern that does not clearly fall into the BLOCKING category

Asset review: if the task description or implementation prompt contains structured asset declarations such as
`### Reference assets` / `### Delivery assets`, or the
implementation prompt contains an `Asset manifest`, treat delivery assets as part
of the security/privacy/legal review surface. Verify that new production assets are supported by the declarations,
that reference-only assets were not copied into production files, and that asset usage
does not introduce sensitive data, unexpected binary/resource content, or licensing risk.

If previous security reviews of this task are included at the end of this prompt,
maintain consistency: do not reverse a judgment unless the implementation has genuinely
changed to address the specific issue you raised. If the vulnerability is still present,
repeat the same issue. If it was fixed but a new one introduced, report the new one only.
For multi-step tasks, maintain consistency only for security issues that are in scope
for the current step.

Output exactly one of:
- If no issues were found: a "Security checks:" summary line describing the trust boundaries
  you traced and what was verified (untrusted inputs, sensitive operations, credentials,
  TLS, injection vectors, PII), followed by the single word APPROVED on its own line.
  For an all-clear approval, the final non-empty line must be exactly APPROVED.
  An all-clear approval without this exact final line is treated as a security review
  failure and will trigger another fix/review loop.
  Example:
    Security checks: countryCode from navigation arg reaches API URL via Retrofit — no
    injection risk (Retrofit encodes path params). No credentials, TLS issues, weak crypto,
    PII logging, or path traversal found.
    APPROVED
- One or both of the following sections (include only sections that apply):
  Do not include APPROVED when reporting security issues or warnings.
  Warnings are non-blocking: warning-only output is accepted and recorded, but it is
  not an all-clear approval.

## Security Issues

### <short title>
File: <relative path>
Problem: <what the vulnerability is>
Fix: <concrete remediation — name the specific API, function, or pattern to use, not just the general principle>

## Warnings

### <short title>
File: <relative path>
Concern: <what the concern is>
Suggestion: <recommended improvement>

Report only issues introduced by the current change. Do not report style issues,
performance concerns, or pre-existing problems in unchanged code.\
"""

_USER_SECURITY = """\
Task description:
{task_description}
{delivery_constraint_context}

---
Implementation prompt:
{implementation_prompt}

---
Files changed:
{files_changed}

---
Git diff (modified files vs HEAD):
{diff}

Perform the security review.\
"""

_STEP_SECURITY_SCOPE = """\
---
CURRENT STEP SECURITY SCOPE:
Step context: This security review covers step {step_num} of {total_steps}: "{step_description}"

Review security for the current step and any already changed code needed to trace its
data flows. Future planned steps are context only:
{future_steps}
Do NOT report missing future planned steps as security issues.\
"""

_FINAL_FULL_TASK_SECURITY_SCOPE = """\
---
FINAL FULL-TASK SECURITY SCOPE:
Review security across the complete diff and complete original task.
Do not restrict findings to the last planned step.
Trace trust boundaries through all changed production code and any changed tests/config
that affect security-sensitive behavior.\
"""

_SCOPE_FINAL_FULL_TASK = "final_full_task"


def _scope(state: TaskState) -> str:
    if state.active_scope:
        return state.active_scope
    return "step" if state.plan else "task"


class SecurityReviewerAgent(BaseAgent):
    name = "security_reviewer"

    def run(self, state: TaskState) -> AgentResult:
        if not state.implementation_prompt:
            return AgentResult(success=False, message="No implementation prompt in state")
        if not state.files_changed:
            return AgentResult(success=False, message="No changed files to review")

        file_tool = self.tools.get("file")
        git_tool = self.tools.get("git")
        if not file_tool:
            return AgentResult(success=False, message="FileTool not available")

        try:
            delivery_context = delivery_agent_prompt_context(
                state,
                role=self.name,
                project_config=self.project_config,
            )
        except DeliveryConstraintContextError as exc:
            message = "Inherited delivery constraint context was rejected before security review ({code}).".format(
                code=exc.code
            )
            state.record(self.name, "delivery_constraint_context_rejected", message)
            return AgentResult(success=False, message=message)
        except DeliveryWriteScopeError as exc:
            message = "Delivery review write-scope context was rejected before security review ({code}).".format(
                code=exc.code
            )
            state.record(self.name, "delivery_write_scope_context_rejected", message)
            return AgentResult(success=False, message=message)
        delivery_child = delivery_context.is_delivery_child

        diff = ""
        if state.review_diff is not None:
            diff = state.review_diff[:_MAX_DIFF_CHARS]
            if len(state.review_diff) > _MAX_DIFF_CHARS:
                diff += "\n... (diff truncated)"
        elif git_tool:
            result = git_tool.diff_head()
            if result.success and result.output.strip():
                diff = result.output[:_MAX_DIFF_CHARS]
                if len(result.output) > _MAX_DIFF_CHARS:
                    diff += "\n... (diff truncated)"
        if not diff:
            diff = "(diff not available — use Read tool to inspect changed files)"

        raw_context = self.project_config.get("security", {}).get("context", "").strip()
        security_context = f"\n\nProject security context:\n{raw_context}" if raw_context else ""

        step_scope = ""
        if state.active_scope == _SCOPE_FINAL_FULL_TASK:
            step_scope = _FINAL_FULL_TASK_SECURITY_SCOPE
        elif state.plan:
            step_idx = state.current_step
            future_steps = state.plan[step_idx + 1 :]
            future_text = "\n".join(f"  - {step}" for step in future_steps) if future_steps else "  - none"
            step_scope = _STEP_SECURITY_SCOPE.format(
                step_num=step_idx + 1,
                total_steps=len(state.plan),
                step_description=state.plan[step_idx],
                future_steps=future_text,
            )

        full_prompt = (
            _SYSTEM_SECURITY.format(
                tech_stack=_tech_stack(self.project_config),
                guidelines_context=_gather_guidelines(self.project_config, file_tool),
                security_context=security_context,
            )
            + _load_extra_rules(self.project_config, self.name, file_tool)
            + "\n\n"
            + (delivery_context.effective_write_scope + "\n\n" if delivery_child else "")
            + (delivery_context.disposition_contract + "\n\n" if delivery_child else "")
            + step_scope
            + ("\n\n" if step_scope else "")
            + _USER_SECURITY.format(
                task_description=state.task_description,
                delivery_constraint_context=delivery_context.inherited_constraints,
                implementation_prompt=state.implementation_prompt,
                files_changed="\n".join(f"  - {f}" for f in state.files_changed),
                diff=diff,
            )
        )

        security_history = []
        security_records = list(state.security_review_cycle_records)
        security_records.extend(
            record for record in state.review_cycle_records if record.get("reviewer") == "security_reviewer"
        )
        for record in security_records:
            if record.get("reviewer") not in (None, "security_reviewer"):
                continue
            if state.active_scope == _SCOPE_FINAL_FULL_TASK:
                if record.get("scope") != _SCOPE_FINAL_FULL_TASK:
                    continue
            elif state.plan:
                if record.get("scope") == _SCOPE_FINAL_FULL_TASK or record.get("step") != state.current_step:
                    continue
            previous_output = record.get("reviewer_output")
            parse_error = record.get("disposition_parse_error")
            history_entry = previous_output if isinstance(previous_output, str) and previous_output.strip() else None
            if isinstance(parse_error, str) and parse_error:
                if history_entry is None:
                    history_entry = "[Previous security review returned no output.]"
                history_entry += (
                    "\n\n[Sikula protocol correction required: "
                    f"{parse_error}. Return the complete security review again and finish with "
                    "exactly one valid delivery disposition JSON object as the final non-empty line.]"
                )
            if history_entry is not None:
                security_history.append(history_entry)
        if security_history:
            history_text = "\n\n---\n".join(f"[Security Review {i + 1}]\n{r}" for i, r in enumerate(security_history))
            full_prompt += (
                f"\n\n---\nYour previous security reviews of this task (maintain consistency):\n{history_text}"
            )

        full_prompt = read_only_agent_prompt(AGENT_SECURITY_PREFIX + full_prompt)

        try:
            output = self.llm.run_readonly_agent(full_prompt, cwd=file_tool._root)
        except RuntimeError as e:
            msg = str(e)
            state.security_review_cycle_records.append(
                {
                    "step": state.current_step,
                    "build_iteration": state.build_iterations,
                    "security_review_iteration": state.security_review_iterations,
                    "scope": _scope(state),
                    "reviewer_prompt": full_prompt,
                    "reviewer_output": None,
                    "approved": False,
                    "has_warnings": False,
                    "error": msg,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            state.record(self.name, "review_failed", msg[:500])
            return AgentResult(success=False, message=msg[:200])

        cycle_record = {
            "step": state.current_step,
            "build_iteration": state.build_iterations,
            "security_review_iteration": state.security_review_iterations,
            "scope": _scope(state),
            "reviewer_prompt": full_prompt,
            "reviewer_output": output,
            "approved": False,
            "has_warnings": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        state.security_review_cycle_records.append(cycle_record)

        if not output or not output.strip():
            parse_error = "delivery_disposition.output_empty"
            cycle_record["disposition_parse_error"] = parse_error
            return AgentResult(
                success=False,
                message="Security reviewer produced empty output",
                data={"disposition_parse_error": parse_error},
            )

        has_blocking = "## Security Issues" in output
        has_warnings = "## Warnings" in output
        last_line = next((ln for ln in reversed(output.splitlines()) if ln.strip()), "")
        has_approved = re.sub(r"[^A-Za-z]", "", last_line).upper() == "APPROVED"

        disposition_result = classify_delivery_review_disposition(
            output,
            is_delivery_child=delivery_child,
            has_blocking_section=has_blocking,
            approved_signal=has_approved,
            has_warnings=has_warnings,
        )
        disposition = disposition_result.disposition
        disposition_error = disposition_result.error_code

        if disposition_error:
            cycle_record["disposition_parse_error"] = disposition_error
            cycle_record["has_warnings"] = has_warnings
            state.security_approved = False
            state.review_approved = False
            state.review_issues = [output]
            state.record(self.name, "review_failed", f"invalid delivery disposition ({disposition_error})")
            return AgentResult(
                success=False,
                message=f"Security reviewer produced invalid delivery disposition ({disposition_error})",
                data={"disposition_parse_error": disposition_error},
            )

        if disposition is not None:
            cycle_record["disposition"] = disposition.to_dict()
        approved_disposition = disposition is not None and disposition.disposition == DELIVERY_DISPOSITION_APPROVED
        cycle_record["approved"] = not has_blocking and (has_approved or has_warnings or approved_disposition)
        cycle_record["has_warnings"] = has_warnings

        if has_warnings:
            log.info("Security warnings (non-blocking):\n%s", output)

        if has_blocking:
            state.security_approved = False
            state.review_issues = [output]
            state.review_approved = False
            state.record(self.name, "review", f"blocking issues ({len(output)} chars)")
            log.warning("Security review — blocking issues:\n%s", output)
            if disposition is not None and disposition.disposition != DELIVERY_DISPOSITION_FIX_IN_SCOPE:
                state.set_delivery_stop_disposition(self.name, disposition)
                return AgentResult(
                    success=False,
                    message=disposition.disposition,
                    data={"disposition": disposition.to_dict()},
                )
            data = {"issues": output}
            if disposition is not None:
                data["disposition"] = disposition.to_dict()
            return AgentResult(success=False, message="Security review found blocking issues", data=data)

        if not has_approved and not has_warnings and not approved_disposition:
            state.security_approved = False
            state.review_issues = [output]
            state.review_approved = False
            state.record(self.name, "review", f"unexpected output — no APPROVED signal ({len(output)} chars)")
            log.warning("Security review — unexpected output, treating as blocking:\n%s", output)
            return AgentResult(
                success=False,
                message="Security review produced unexpected output (no APPROVED signal)",
                data={"issues": output},
            )

        state.security_approved = True
        state.review_issues.clear()
        if has_warnings:
            state.record(self.name, "review", f"warnings only ({len(output)} chars)")
            return AgentResult(
                success=True,
                message="Security review passed with warnings",
                data={"warnings": output},
            )

        state.record(self.name, "review", "approved")
        log.info(f"Security review approved:\n{output}")
        return AgentResult(success=True, message="Security review approved")
