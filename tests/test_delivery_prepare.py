from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.delivery_authoring import (
    DeliveryAuthoringDerivedPaths,
    DeliveryAuthoringDraft,
    DeliveryAuthoringParseError,
    DeliveryAuthoringUnitDraft,
)
from core.delivery_prepare_writer import (
    DeliveryPreparePlanValidationSummary,
    DeliveryPrepareUnitReadinessAggregate,
    DeliveryPrepareUnitReadinessSummary,
    DeliveryPrepareWriteIssue,
    DeliveryPrepareWriteResult,
    DeliveryPrepareWrittenArtifact,
)
import sikula
from sikula_cli.agent_overrides import (
    DELIVERY_PREPARATION_AGENT_NAMES,
    PREPARATION_AGENT_NAMES,
    parse_agent_llm_overrides,
)
from sikula_cli.delivery import (
    DeliveryPrepareArtifact,
    DeliveryPrepareContext,
    DeliveryPrepareIssue,
    DeliveryPrepareResult,
    cmd_delivery_prepare,
    register_parser,
    render_delivery_prepare,
)


def _cfg(root: Path) -> dict:
    return {
        "project": {"root_path": str(root), "build_tool": "python"},
        "build": {"test_command": "python3 -m pytest tests/test_delivery_prepare.py"},
    }


def _args(
    task_file: str | Path,
    *,
    output: str | Path | None = None,
    force: bool = False,
    json_output: bool = False,
    agent_model: list[str] | None = None,
    agent_provider: list[str] | None = None,
    agent_timeout: list[str] | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        task_file=str(task_file),
        output=str(output) if output is not None else None,
        force=force,
        json=json_output,
        agent_model=agent_model,
        agent_provider=agent_provider,
        agent_timeout=agent_timeout,
    )


def _write_task(root: Path, rel_path: str = "tasks/team-invites.md", body: str = "# Team invites\n") -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _ready_unit_markdown(title: str) -> str:
    return f"""# {title}

## Goal

Deliver the {title} unit while keeping generated command output privacy-safe.

## Current behavior

- Operators do not yet have this delivery unit as a tracked source artifact.
- The generated unit body contains PRIVATE UNIT MARKDOWN that must not appear in CLI output.

## Desired behavior

- The unit is represented as a focused task description with observable outcomes.
- The unit can be checked for readiness before any delivery execution starts.
- Generated metadata reports only project-relative paths and readiness summaries.

## Acceptance criteria

- The unit task has a deterministic success path.
- Invalid or incomplete generated content is reported as blocked.
- Raw prompts, provider output, and generated task bodies are not printed in normal output.

## Security and privacy

- Do not expose raw provider output, task bodies, source excerpts, secrets, or absolute paths.
- Keep audit artifacts local and separate from ordinary text and JSON projections.

## Reviewer focus

- Confirm readiness checks and privacy-safe output metadata.
- Confirm generated source artifacts remain project-relative.

## Out of scope

- Do not run generated delivery units.
- Do not create delivery progress state.

## Tests

- Cover successful artifact writing through the delivery prepare command.
- Cover blocked readiness, plan validation failure, and writer failure outputs.

## Verification

- `python3 -m pytest tests/test_delivery_prepare.py`
"""


def _authoring_draft(
    *,
    plan_id: str = "team-invites",
    unit_ids: list[str] | None = None,
    planning_mode: str | None = "fixed_window",
    audit_path: str | Path | None = None,
    task_markdown: str | None = None,
    scope_paths: list[str] | None = None,
    warnings: list[str] | None = None,
) -> DeliveryAuthoringDraft:
    units = [
        DeliveryAuthoringUnitDraft(
            id=unit_id,
            title=f"{unit_id} title",
            depends_on=[],
            task_markdown=task_markdown if task_markdown is not None else _ready_unit_markdown(f"{unit_id} title"),
            scope_paths=scope_paths or [],
        )
        for unit_id in (unit_ids or ["foundation", "cli"])
    ]
    draft = DeliveryAuthoringDraft(
        plan_id=plan_id,
        title="Team invites delivery",
        units=units,
        planning_mode=planning_mode,
        warnings=warnings or [],
    )
    if audit_path is not None:
        setattr(draft, "audit_path", str(audit_path))
    return draft


def _authoring_context(
    *,
    calls: list[dict] | None = None,
    draft: DeliveryAuthoringDraft | None = None,
    failure: Exception | None = None,
    invalid_result: bool = False,
) -> DeliveryPrepareContext:
    def run_authoring_assistant(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if failure is not None:
            raise failure
        if invalid_result:
            return object()
        return draft or _authoring_draft(plan_id=kwargs["selected_plan_id"])

    return DeliveryPrepareContext(run_authoring_assistant=run_authoring_assistant)


def _blocked_payload(
    args: argparse.Namespace,
    cfg: dict,
    capsys: pytest.CaptureFixture[str],
    *,
    context: DeliveryPrepareContext | None = None,
) -> dict:
    with pytest.raises(SystemExit) as exc:
        cmd_delivery_prepare(args, cfg, context=context)

    assert exc.value.code == 1
    return json.loads(capsys.readouterr().out)


def test_delivery_prepare_help_documents_command_and_options(capsys: pytest.CaptureFixture[str]) -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_parser(subparsers)

    with pytest.raises(SystemExit) as parent_help:
        parser.parse_args(["delivery", "--help"])
    assert parent_help.value.code == 0
    parent_out = capsys.readouterr().out
    assert "prepare" in parent_out
    assert "Prepare delivery plan artifacts from a task file" in parent_out

    with pytest.raises(SystemExit) as prepare_help:
        parser.parse_args(["delivery", "prepare", "--help"])

    assert prepare_help.value.code == 0
    out = capsys.readouterr().out
    assert "TASK_FILE" in out
    assert "--output DIR" in out
    assert "--force" in out
    assert "--json" in out
    assert "--agent-model AGENT=MODEL" in out
    assert "--agent-provider AGENT=PROVIDER" in out
    assert "--agent-timeout AGENT=SECONDS" in out

    delivery_parser = subparsers.choices["delivery"]
    delivery_subparsers = next(
        action for action in delivery_parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    prepare_parser = delivery_subparsers.choices["prepare"]
    help_by_dest = {action.dest: action.help for action in prepare_parser._actions}
    assert help_by_dest["task_file"] == "Path to source task .txt/.md file"
    assert help_by_dest["output"] == "Delivery plan output directory; defaults to .sikula/delivery/<task-stem>/"
    assert help_by_dest["force"] == "Allow replacing existing delivery plan artifacts in the output directory"
    assert help_by_dest["json"] == "Print structured JSON output"
    assert (
        help_by_dest["agent_model"]
        == "Override model for delivery_preparer, e.g. --agent-model delivery_preparer=gpt-5.5"
    )
    assert (
        help_by_dest["agent_provider"]
        == "Override provider for delivery_preparer, e.g. --agent-provider delivery_preparer=claude"
    )
    assert (
        help_by_dest["agent_timeout"]
        == "Override timeout for delivery_preparer, e.g. --agent-timeout delivery_preparer=1200"
    )


def test_delivery_prepare_register_parser_sets_flags() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_parser(subparsers)

    args = parser.parse_args(
        [
            "delivery",
            "prepare",
            ".sikula/tasks/task.md",
            "--output",
            ".sikula/delivery/task",
            "--force",
            "--json",
            "--agent-model",
            "delivery_preparer=gpt-5.5",
            "--agent-provider",
            "delivery_preparer=claude",
            "--agent-timeout",
            "delivery_preparer=1200",
        ]
    )

    assert args.command == "delivery"
    assert args.delivery_command == "prepare"
    assert args.task_file == ".sikula/tasks/task.md"
    assert args.output == ".sikula/delivery/task"
    assert args.force is True
    assert args.json is True
    assert args.agent_model == ["delivery_preparer=gpt-5.5"]
    assert args.agent_provider == ["delivery_preparer=claude"]
    assert args.agent_timeout == ["delivery_preparer=1200"]


def test_cmd_delivery_prepare_blocks_without_context_after_successful_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path, "tasks/Team Invites.md", "# Team invites\n\nDo not echo this raw task body.\n")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_prepare(_args("tasks/Team Invites.md"), _cfg(tmp_path))

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Delivery prepare: tasks/Team Invites.md" in out
    assert "Status: blocked" in out
    assert "Selected plan: team-invites" in out
    assert "Output: .sikula/delivery/team-invites" in out
    assert "Plan file: .sikula/delivery/team-invites/plan.yaml" in out
    assert "Units dir: .sikula/delivery/team-invites/units" in out
    assert "Overwrite allowed: no" in out
    assert "Draft units: 0" in out
    assert "Delivery prepare authoring requires the main Sikula command context." in out
    assert "Do not echo this raw task body" not in out
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


def test_cmd_delivery_prepare_writes_artifacts_after_authoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path, "tasks/Team Invites.md", "# Team invites\n\nDo not echo this raw task body.\n")
    audit_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.audit.json"
    calls: list[dict] = []
    monkeypatch.chdir(tmp_path)

    cmd_delivery_prepare(
        _args("tasks/Team Invites.md"),
        _cfg(tmp_path),
        context=_authoring_context(calls=calls, draft=_authoring_draft(audit_path=audit_path)),
    )

    out = capsys.readouterr().out
    assert "Delivery prepare: tasks/Team Invites.md" in out
    assert "Status: ready" in out
    assert "Selected plan: team-invites" in out
    assert "Draft units: 2" in out
    assert "Authoring audit: .sikula/contract-reports/team-invites.audit.json" in out
    assert "Written artifacts:" in out
    assert "- plan: .sikula/delivery/team-invites/plan.yaml" in out
    assert "- unit_task: .sikula/delivery/team-invites/units/foundation.md" in out
    assert "- unit_task: .sikula/delivery/team-invites/units/cli.md" in out
    assert "Unit task paths:" in out
    assert "- foundation: .sikula/delivery/team-invites/units/foundation.md" in out
    assert "- cli: .sikula/delivery/team-invites/units/cli.md" in out
    assert "Plan validation:" in out
    assert "- status: valid" in out
    assert "- valid: yes" in out
    assert "Unit readiness:" in out
    assert "- status: ready" in out
    assert "Delivery plan artifacts written." in out
    assert "Do not echo this raw task body" not in out
    assert "PRIVATE UNIT MARKDOWN" not in out
    assert str(tmp_path) not in out
    assert (tmp_path / ".sikula" / "delivery" / "team-invites" / "plan.yaml").is_file()
    assert (tmp_path / ".sikula" / "delivery" / "team-invites" / "units" / "foundation.md").is_file()
    assert (tmp_path / ".sikula" / "delivery" / "team-invites" / "units" / "cli.md").is_file()
    assert not (tmp_path / ".sikula" / "state" / "delivery" / "team-invites").exists()
    assert len(calls) == 1
    assert calls[0]["task_path"] == (tmp_path / "tasks" / "Team Invites.md").resolve()
    assert calls[0]["output_dir"] == (tmp_path / ".sikula" / "delivery" / "team-invites").resolve()
    assert calls[0]["selected_plan_id"] == "team-invites"
    assert calls[0]["project_root"] == tmp_path.resolve()


def test_sikula_cmd_delivery_prepare_wrapper_delegates_to_cli(tmp_path: Path) -> None:
    args = _args("tasks/team-invites.md")
    cfg = _cfg(tmp_path)

    with patch("sikula.cli_delivery.cmd_delivery_prepare", return_value="prepared") as prepare:
        result = sikula.cmd_delivery_prepare(args, cfg)

    assert result == "prepared"
    prepare.assert_called_once()
    called_args = prepare.call_args.args
    assert called_args[:2] == (args, cfg)
    context = called_args[2]
    assert isinstance(context, DeliveryPrepareContext)
    assert context.run_authoring_assistant is sikula._run_delivery_prepare_authoring


@pytest.mark.parametrize(
    ("cfg", "args_kwargs", "expected"),
    [
        pytest.param(
            {
                "llm": {"provider": "codex", "model": "global-model", "agent_timeout": 111},
                "agents": {"task_preparer": {"llm": {"model": "task-preparer-model"}}},
            },
            {},
            ("codex", "global-model", 111),
            id="global_fallback",
        ),
        pytest.param(
            {
                "llm": {"provider": "codex", "model": "global-model", "agent_timeout": 111},
                "agents": {
                    "task_preparer": {"llm": {"model": "task-preparer-model"}},
                    "delivery_preparer": {
                        "llm": {"provider": "claude", "model": "delivery-yaml-model", "agent_timeout": 222}
                    },
                },
            },
            {},
            ("claude", "delivery-yaml-model", 222),
            id="delivery_preparer_yaml",
        ),
        pytest.param(
            {
                "llm": {"provider": "codex", "model": "global-model", "agent_timeout": 111},
                "agents": {"delivery_preparer": {"llm": {"model": "delivery-yaml-model"}}},
            },
            {},
            ("codex", "delivery-yaml-model", 111),
            id="delivery_preparer_yaml_field_fallback",
        ),
        pytest.param(
            {
                "llm": {"provider": "codex", "model": "global-model", "agent_timeout": 111},
                "agents": {
                    "delivery_preparer": {
                        "llm": {"provider": "claude", "model": "delivery-yaml-model", "agent_timeout": 222}
                    }
                },
            },
            {
                "agent_model": ["delivery_preparer=delivery-cli-model"],
                "agent_provider": ["delivery_preparer=gemini"],
                "agent_timeout": ["delivery_preparer=333"],
            },
            ("gemini", "delivery-cli-model", 333),
            id="delivery_preparer_cli_overrides",
        ),
    ],
)
def test_create_delivery_preparation_agent_resolves_delivery_preparer_llm_config(
    cfg: dict,
    args_kwargs: dict,
    expected: tuple[str, str, int],
) -> None:
    args = _args("tasks/team-invites.md", **args_kwargs)
    llm = object()
    agent = object()

    with (
        patch("core.llm_client.create_llm_client", return_value=llm) as create_llm_client,
        patch("agents.delivery_preparation_agent.DeliveryPreparationAgent", return_value=agent) as agent_cls,
    ):
        result = sikula._create_delivery_preparation_agent(args, cfg)

    assert result is agent
    llm_config = create_llm_client.call_args.args[0]
    assert (llm_config.provider, llm_config.model, llm_config.agent_timeout) == expected
    agent_cls.assert_called_once_with(llm=llm, project_config=cfg)


def test_run_delivery_prepare_authoring_records_audit_and_forwards_context(tmp_path: Path) -> None:
    task_path = _write_task(
        tmp_path,
        ".sikula/tasks/team-invites.md",
        "# Team invites\n\nPRIVATE TASK BODY\n",
    )
    output_dir = tmp_path / ".sikula" / "delivery" / "team-invites"
    args = _args(".sikula/tasks/team-invites.md")
    cfg = _cfg(tmp_path)
    project_context = {"validation_commands": ["python3 -m pytest tests/"]}
    calls: list[dict] = []

    class FakeDeliveryPreparationAgent:
        def author_delivery_plan(self, **kwargs):
            calls.append(kwargs)
            kwargs["audit_recorder"](
                {
                    "phase": "delivery_prepare_authoring",
                    "raw_output": "PRIVATE_PROVIDER_OUTPUT",
                    "parsed": {"status": "parsed", "unit_ids": ["foundation"]},
                }
            )
            return _authoring_draft(plan_id=kwargs["plan_id"], unit_ids=["foundation"])

    fake_agent = FakeDeliveryPreparationAgent()

    with (
        patch("sikula._create_delivery_preparation_agent", return_value=fake_agent) as create_agent,
        patch("sikula._prepare_project_context_from_config", return_value=project_context) as prepare_context,
    ):
        draft = sikula._run_delivery_prepare_authoring(
            args=args,
            cfg=cfg,
            task_path=task_path.resolve(),
            output_dir=output_dir.resolve(),
            selected_plan_id="team-invites",
            project_root=tmp_path.resolve(),
        )

    create_agent.assert_called_once_with(args, cfg)
    prepare_context.assert_called_once_with(cfg)
    assert [unit.id for unit in draft.units] == ["foundation"]
    assert draft.audit_path == ".sikula/contract-reports/team-invites.delivery-prepare.auto-llm.jsonl"
    assert len(calls) == 1
    call = calls[0]
    assert call["task_description"] == "# Team invites\n\nPRIVATE TASK BODY\n"
    assert call["task_path"] == task_path.resolve()
    assert call["plan_id"] == "team-invites"
    assert call["project_root"] == tmp_path.resolve()
    assert call["output_dir"] == output_dir.resolve()
    assert call["project_context"] is project_context
    assert callable(call["audit_recorder"])

    audit_path = tmp_path / ".sikula" / "contract-reports" / "team-invites.delivery-prepare.auto-llm.jsonl"
    audit_record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert audit_record["generated_by"] == "sikula.delivery_prepare"
    assert audit_record["task"]["path"] == ".sikula/tasks/team-invites.md"
    assert audit_record["task"]["sha256"].startswith("sha256:")
    assert audit_record["output"]["path"] == ".sikula/delivery/team-invites"
    assert audit_record["record"] == {
        "phase": "delivery_prepare_authoring",
        "parsed": {"status": "parsed", "unit_ids": ["foundation"]},
        "raw_output": "PRIVATE_PROVIDER_OUTPUT",
    }
    assert not output_dir.exists()


def test_run_delivery_prepare_authoring_attaches_audit_path_to_failures(tmp_path: Path) -> None:
    task_path = _write_task(
        tmp_path,
        ".sikula/tasks/team-invites.md",
        "# Team invites\n\nPRIVATE TASK BODY\n",
    )
    output_dir = tmp_path / ".sikula" / "delivery" / "team-invites"
    args = _args(".sikula/tasks/team-invites.md")
    cfg = _cfg(tmp_path)

    class FailingDeliveryPreparationAgent:
        def author_delivery_plan(self, **kwargs):
            kwargs["audit_recorder"](
                {
                    "phase": "delivery_prepare_authoring",
                    "raw_output": None,
                    "parsed": {"status": "failed", "error_code": "delivery_prepare.authoring_failed"},
                }
            )
            raise RuntimeError("SECRET_PROVIDER_OUTPUT from prompt")

    with (
        patch("sikula._create_delivery_preparation_agent", return_value=FailingDeliveryPreparationAgent()),
        patch("sikula._prepare_project_context_from_config", return_value={}),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            sikula._run_delivery_prepare_authoring(
                args=args,
                cfg=cfg,
                task_path=task_path.resolve(),
                output_dir=output_dir.resolve(),
                selected_plan_id="team-invites",
                project_root=tmp_path.resolve(),
            )

    audit_path = ".sikula/contract-reports/team-invites.delivery-prepare.auto-llm.jsonl"
    assert getattr(exc_info.value, "audit_path") == audit_path
    audit_file = tmp_path / audit_path
    assert audit_file.is_file()
    audit_record = json.loads(audit_file.read_text(encoding="utf-8").splitlines()[0])
    assert audit_record["generated_by"] == "sikula.delivery_prepare"


def test_main_dispatches_delivery_prepare_through_runtime_config(tmp_path: Path) -> None:
    task_path = _write_task(tmp_path)
    cfg = _cfg(tmp_path)
    argv = [
        "sikula",
        "delivery",
        "prepare",
        str(task_path),
        "--output",
        ".sikula/delivery/team-invites",
        "--force",
        "--json",
        "--agent-model",
        "delivery_preparer=gpt-5.5",
    ]

    with patch("sys.argv", argv):
        with patch("sikula._load_runtime_config", return_value=cfg) as load_config:
            with patch("sikula.cmd_delivery_prepare") as prepare:
                sikula.main()

    load_config.assert_called_once_with(None, required=True)
    prepare.assert_called_once()
    args, called_cfg = prepare.call_args.args
    assert called_cfg is cfg
    assert args.delivery_command == "prepare"
    assert args.task_file == str(task_path)
    assert args.output == ".sikula/delivery/team-invites"
    assert args.force is True
    assert args.json is True
    assert args.agent_model == ["delivery_preparer=gpt-5.5"]


def test_main_bare_delivery_prints_help_without_loading_project_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch("sys.argv", ["sikula", "delivery"]):
        with patch("sikula._load_runtime_config") as load_config:
            with pytest.raises(SystemExit) as exc:
                sikula.main()

    assert exc.value.code == 1
    load_config.assert_not_called()
    out = capsys.readouterr().out
    assert "usage:" in out
    assert "prepare" in out


def test_cmd_delivery_prepare_json_is_allowlisted_project_relative_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path, body="# Team invites\n\nPRIVATE TASK BODY\n")
    draft = _authoring_draft(audit_path=".sikula/contract-reports/team-invites.audit.json")
    monkeypatch.chdir(tmp_path)

    cmd_delivery_prepare(
        _args(
            "tasks/team-invites.md",
            output=".sikula/delivery/team-invites",
            json_output=True,
            agent_model=["delivery_preparer=gpt-5.5"],
            agent_provider=["delivery_preparer=claude"],
            agent_timeout=["delivery_preparer=1200"],
        ),
        _cfg(tmp_path),
        context=_authoring_context(draft=draft),
    )

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "ready"
    assert payload["ready"] is True
    assert payload["prepared"] is True
    assert payload["force"] is False
    assert payload["overwrite_allowed"] is False
    assert payload["selected_plan_id"] == "team-invites"
    assert payload["unit_ids"] == ["foundation", "cli"]
    assert payload["paths"] == {
        "output_dir": ".sikula/delivery/team-invites",
        "plan_file": ".sikula/delivery/team-invites/plan.yaml",
        "task_file": "tasks/team-invites.md",
        "units_dir": ".sikula/delivery/team-invites/units",
    }
    assert payload["unit_task_paths"] == {
        "foundation": ".sikula/delivery/team-invites/units/foundation.md",
        "cli": ".sikula/delivery/team-invites/units/cli.md",
    }
    assert payload["written_artifacts"] == [
        {"kind": "plan", "path": ".sikula/delivery/team-invites/plan.yaml"},
        {"kind": "unit_task", "path": ".sikula/delivery/team-invites/units/foundation.md"},
        {"kind": "unit_task", "path": ".sikula/delivery/team-invites/units/cli.md"},
    ]
    assert payload["existing_artifacts"] == []
    assert payload["plan_validation"] == {"status": "valid", "valid": True, "errors": [], "warnings": []}
    assert payload["unit_readiness"]["status"] == "ready"
    assert [unit["unit_id"] for unit in payload["unit_readiness"]["units"]] == ["foundation", "cli"]
    assert all(unit["ready_for_autonomous_delivery"] is True for unit in payload["unit_readiness"]["units"])
    assert all(unit["blocking_gap_count"] == 0 for unit in payload["unit_readiness"]["units"])
    assert all(unit["blocking_gap_ids"] == [] for unit in payload["unit_readiness"]["units"])
    assert payload["authoring"] == {
        "audit_path": ".sikula/contract-reports/team-invites.audit.json",
        "drafted": True,
        "planning_mode": "fixed_window",
        "unit_count": 2,
    }
    assert payload["errors"] == []
    assert payload["warnings"] == []
    assert payload["message"] == "Delivery plan artifacts written."
    assert str(tmp_path) not in out
    assert "PRIVATE TASK BODY" not in out
    assert "PRIVATE UNIT MARKDOWN" not in out
    assert (tmp_path / ".sikula" / "delivery" / "team-invites" / "plan.yaml").is_file()
    assert (tmp_path / ".sikula" / "delivery" / "team-invites" / "units" / "foundation.md").is_file()


def test_cmd_delivery_prepare_surfaces_authoring_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    draft = _authoring_draft(
        audit_path=".sikula/contract-reports/team-invites.delivery-prepare.auto-llm.jsonl",
        warnings=["SECRET_PROVIDER_OUTPUT and PRIVATE TASK BODY"],
    )
    monkeypatch.chdir(tmp_path)

    cmd_delivery_prepare(
        _args("tasks/team-invites.md", json_output=True),
        _cfg(tmp_path),
        context=_authoring_context(draft=draft),
    )

    payload = json.loads(capsys.readouterr().out)
    payload_text = json.dumps(payload)
    assert payload["status"] == "ready"
    assert payload["warnings"] == [
        {
            "code": "delivery_prepare.authoring_warnings_present",
            "message": "Delivery authoring assistant reported warnings; inspect the local audit artifact for details.",
            "path": ".sikula/contract-reports/team-invites.delivery-prepare.auto-llm.jsonl",
            "severity": "warning",
        }
    ]
    assert "SECRET_PROVIDER_OUTPUT" not in payload_text
    assert "PRIVATE TASK BODY" not in payload_text


def test_cmd_delivery_prepare_blocks_unit_readiness_failures_without_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path, body="# Team invites\n\nPRIVATE TASK BODY\n")
    weak_draft = _authoring_draft(unit_ids=["weak"], task_markdown="# Weak\n\nToo vague.")
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args("tasks/team-invites.md", json_output=True),
        _cfg(tmp_path),
        capsys,
        context=_authoring_context(draft=weak_draft),
    )

    payload_text = json.dumps(payload)
    assert payload["status"] == "blocked"
    assert payload["ready"] is False
    assert payload["prepared"] is False
    assert payload["errors"] == [
        {
            "code": "delivery_prepare.unit_readiness_blocked",
            "message": (
                "Generated unit task contracts have blocking readiness gaps; no source artifacts were finalized."
            ),
            "path": None,
            "severity": "error",
        }
    ]
    assert payload["plan_validation"] == {"status": "not_run", "valid": None, "errors": [], "warnings": []}
    assert payload["unit_readiness"]["status"] == "blocked"
    assert payload["unit_readiness"]["units"][0]["unit_id"] == "weak"
    assert payload["unit_readiness"]["units"][0]["blocking_gap_count"] > 0
    assert payload["written_artifacts"] == []
    assert (
        payload["message"]
        == "Generated unit task contracts have blocking readiness gaps; no source artifacts were finalized."
    )
    assert "PRIVATE TASK BODY" not in payload_text
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


def test_cmd_delivery_prepare_blocks_plan_validation_failures_and_rolls_back_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    draft = _authoring_draft(unit_ids=["foundation"], scope_paths=["../outside"])
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args("tasks/team-invites.md", json_output=True),
        _cfg(tmp_path),
        capsys,
        context=_authoring_context(draft=draft),
    )

    assert payload["status"] == "blocked"
    assert payload["prepared"] is False
    assert payload["errors"][0] == {
        "code": "delivery_prepare.plan_validation_failed",
        "message": "Generated delivery plan artifacts failed validation; no source artifacts were finalized.",
        "path": None,
        "severity": "error",
    }
    assert payload["plan_validation"]["status"] == "invalid"
    assert payload["plan_validation"]["valid"] is False
    assert payload["plan_validation"]["errors"][0]["code"] == "units.scope_path_outside_project"
    assert payload["unit_readiness"]["status"] == "ready"
    assert payload["written_artifacts"] == []
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


def test_cmd_delivery_prepare_maps_writer_failures_to_safe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    invalid_draft = _authoring_draft(unit_ids=["bad/path"])
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args("tasks/team-invites.md", json_output=True),
        _cfg(tmp_path),
        capsys,
        context=_authoring_context(draft=invalid_draft),
    )

    payload_text = json.dumps(payload)
    assert payload["status"] == "blocked"
    assert payload["ready"] is False
    assert payload["prepared"] is False
    assert payload["errors"][0] == {
        "code": "delivery_prepare.write_failed",
        "message": "Delivery prepare failed while writing artifacts; no source artifacts were finalized.",
        "path": None,
        "severity": "error",
    }
    assert payload["errors"][1]["code"] == "delivery_authoring.unit_id_invalid"
    assert payload["written_artifacts"] == []
    assert "PRIVATE UNIT MARKDOWN" not in payload_text
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


def test_cmd_delivery_prepare_preserves_writer_artifacts_after_rollback_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    draft = _authoring_draft(
        unit_ids=["foundation"],
        audit_path=".sikula/contract-reports/team-invites.delivery-prepare.auto-llm.jsonl",
        warnings=["SECRET_PROVIDER_OUTPUT and PRIVATE TASK BODY"],
    )
    monkeypatch.chdir(tmp_path)

    def blocked_writer(*_args, **_kwargs):
        return DeliveryPrepareWriteResult(
            status="blocked",
            prepared=False,
            paths=DeliveryAuthoringDerivedPaths(
                plan_file=".sikula/delivery/team-invites/plan.yaml",
                units_dir=".sikula/delivery/team-invites/units",
                unit_task_paths={
                    "foundation": ".sikula/delivery/team-invites/units/foundation.md",
                },
            ),
            unit_task_paths={
                "foundation": ".sikula/delivery/team-invites/units/foundation.md",
            },
            written_artifacts=[
                DeliveryPrepareWrittenArtifact("plan", ".sikula/delivery/team-invites/plan.yaml"),
                DeliveryPrepareWrittenArtifact("unit_task", ".sikula/delivery/team-invites/units/foundation.md"),
            ],
            plan_validation=DeliveryPreparePlanValidationSummary(status="not_run", valid=None),
            unit_readiness=DeliveryPrepareUnitReadinessAggregate(
                status="ready",
                units=[
                    DeliveryPrepareUnitReadinessSummary(
                        unit_id="foundation",
                        path=".sikula/delivery/team-invites/units/foundation.md",
                        readiness_score=100,
                        status="ready",
                        ready_for_autonomous_delivery=True,
                        blocking_gap_count=0,
                        warning_gap_count=0,
                    )
                ],
            ),
            errors=[
                DeliveryPrepareWriteIssue(
                    "error",
                    "delivery_prepare.write_failed",
                    "Delivery prepare failed while writing artifacts.",
                ),
                DeliveryPrepareWriteIssue(
                    "error",
                    "delivery_prepare.rollback_failed",
                    "Delivery prepare failed while restoring artifacts; inspect the selected output directory.",
                    ".sikula/delivery/team-invites/plan.yaml",
                ),
            ],
            failure_reason="write_failed",
        )

    monkeypatch.setattr("sikula_cli.delivery.write_delivery_prepare_artifacts", blocked_writer)

    payload = _blocked_payload(
        _args("tasks/team-invites.md", json_output=True),
        _cfg(tmp_path),
        capsys,
        context=_authoring_context(draft=draft),
    )

    assert payload["status"] == "blocked"
    assert payload["written_artifacts"] == [
        {"kind": "plan", "path": ".sikula/delivery/team-invites/plan.yaml"},
        {"kind": "unit_task", "path": ".sikula/delivery/team-invites/units/foundation.md"},
    ]
    assert payload["errors"][0]["code"] == "delivery_prepare.write_failed"
    assert payload["errors"][1]["code"] == "delivery_prepare.rollback_failed"
    assert payload["warnings"] == [
        {
            "code": "delivery_prepare.authoring_warnings_present",
            "message": "Delivery authoring assistant reported warnings; inspect the local audit artifact for details.",
            "path": ".sikula/contract-reports/team-invites.delivery-prepare.auto-llm.jsonl",
            "severity": "warning",
        }
    ]
    payload_text = json.dumps(payload)
    assert "SECRET_PROVIDER_OUTPUT" not in payload_text
    assert "PRIVATE TASK BODY" not in payload_text


def test_cmd_delivery_prepare_does_not_call_authoring_when_preflight_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict] = []
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args("tasks/missing.md", json_output=True),
        _cfg(tmp_path),
        capsys,
        context=_authoring_context(calls=calls),
    )

    assert payload["errors"][0]["code"] == "delivery_prepare.task_missing"
    assert calls == []
    assert not (tmp_path / ".sikula" / "delivery" / "missing").exists()


def test_cmd_delivery_prepare_blocks_provider_failure_with_safe_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path, body="# Team invites\n\nPRIVATE TASK BODY\n")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_prepare(
            _args("tasks/team-invites.md"),
            _cfg(tmp_path),
            context=_authoring_context(failure=RuntimeError("SECRET_PROVIDER_OUTPUT from prompt")),
        )

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Status: blocked" in out
    assert "delivery_prepare.authoring_failed" in out
    assert "Delivery authoring assistant failed; see local audit artifacts for details." in out
    assert "Delivery prepare authoring failed; inspect the local audit artifact and retry." in out
    assert "SECRET_PROVIDER_OUTPUT" not in out
    assert "PRIVATE TASK BODY" not in out
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


def test_cmd_delivery_prepare_propagates_authoring_failure_audit_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path, body="# Team invites\n\nPRIVATE TASK BODY\n")
    failure = RuntimeError("SECRET_PROVIDER_OUTPUT from prompt")
    setattr(failure, "audit_path", ".sikula/contract-reports/team-invites.delivery-prepare.auto-llm.jsonl")
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args("tasks/team-invites.md", json_output=True),
        _cfg(tmp_path),
        capsys,
        context=_authoring_context(failure=failure),
    )

    payload_text = json.dumps(payload)
    assert payload["errors"][0]["code"] == "delivery_prepare.authoring_failed"
    assert payload["authoring"]["audit_path"] == ".sikula/contract-reports/team-invites.delivery-prepare.auto-llm.jsonl"
    assert "SECRET_PROVIDER_OUTPUT" not in payload_text
    assert "PRIVATE TASK BODY" not in payload_text


def test_cmd_delivery_prepare_blocks_parse_failure_with_safe_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path, body="# Team invites\n\nPRIVATE TASK BODY\n")
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args("tasks/team-invites.md", json_output=True),
        _cfg(tmp_path),
        capsys,
        context=_authoring_context(
            failure=DeliveryAuthoringParseError(
                "delivery_authoring.json_invalid",
                "SECRET_PROVIDER_OUTPUT and raw prompt details",
            )
        ),
    )

    payload_text = json.dumps(payload)
    assert payload["ready"] is False
    assert payload["prepared"] is False
    assert payload["status"] == "blocked"
    assert payload["authoring"] == {
        "audit_path": None,
        "drafted": False,
        "planning_mode": None,
        "unit_count": 0,
    }
    assert payload["errors"] == [
        {
            "code": "delivery_prepare.authoring_invalid",
            "message": "Delivery authoring assistant returned an invalid draft; see local audit artifacts for details.",
            "path": None,
            "severity": "error",
        }
    ]
    assert (
        payload["message"]
        == "Delivery authoring assistant returned an invalid draft; see local audit artifacts for details."
    )
    assert "SECRET_PROVIDER_OUTPUT" not in payload_text
    assert "PRIVATE TASK BODY" not in payload_text


def test_cmd_delivery_prepare_blocks_invalid_context_return_as_invalid_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args("tasks/team-invites.md", json_output=True),
        _cfg(tmp_path),
        capsys,
        context=_authoring_context(invalid_result=True),
    )

    assert payload["errors"][0]["code"] == "delivery_prepare.authoring_invalid"
    assert payload["authoring"]["drafted"] is False
    assert payload["unit_ids"] == []


def test_cmd_delivery_prepare_omits_authoring_audit_path_outside_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    outside_audit = tmp_path.parent / "secret-audit.json"
    monkeypatch.chdir(tmp_path)

    cmd_delivery_prepare(
        _args("tasks/team-invites.md", json_output=True),
        _cfg(tmp_path),
        context=_authoring_context(draft=_authoring_draft(audit_path=outside_audit)),
    )

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ready"] is True
    assert payload["authoring"]["audit_path"] is None
    assert str(outside_audit) not in out


def test_cmd_delivery_prepare_resolves_task_path_from_current_directory_and_writes_default_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    workdir = tmp_path / "nested"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    cmd_delivery_prepare(
        _args("../tasks/team-invites.md", json_output=True),
        _cfg(tmp_path),
        context=_authoring_context(),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["prepared"] is True
    assert payload["selected_plan_id"] == "team-invites"
    assert payload["paths"] == {
        "output_dir": ".sikula/delivery/team-invites",
        "plan_file": ".sikula/delivery/team-invites/plan.yaml",
        "task_file": "tasks/team-invites.md",
        "units_dir": ".sikula/delivery/team-invites/units",
    }
    assert (tmp_path / ".sikula" / "delivery" / "team-invites" / "plan.yaml").is_file()


@pytest.mark.parametrize("task_suffix", [".refined", ".v4", ".v12.refined"])
def test_cmd_delivery_prepare_strips_generated_suffixes_from_default_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    task_suffix: str,
) -> None:
    task_file = f"tasks/team-invites{task_suffix}.md"
    _write_task(tmp_path, task_file)
    monkeypatch.chdir(tmp_path)

    cmd_delivery_prepare(
        _args(task_file, json_output=True),
        _cfg(tmp_path),
        context=_authoring_context(),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["selected_plan_id"] == "team-invites"
    assert payload["paths"]["output_dir"] == ".sikula/delivery/team-invites"
    assert payload["paths"]["plan_file"] == ".sikula/delivery/team-invites/plan.yaml"
    assert (tmp_path / ".sikula" / "delivery" / "team-invites" / "plan.yaml").is_file()


def test_cmd_delivery_prepare_uses_cwd_when_config_root_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    monkeypatch.chdir(tmp_path)

    cmd_delivery_prepare(_args("tasks/team-invites.md", json_output=True), {}, context=_authoring_context())

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["paths"]["task_file"] == "tasks/team-invites.md"
    assert payload["paths"]["output_dir"] == ".sikula/delivery/team-invites"
    assert str(tmp_path) not in json.dumps(payload)


@pytest.mark.parametrize("plan_id", ["alpha", "alpha.beta", "alpha_beta", "alpha-beta", "a1", "1alpha"])
def test_cmd_delivery_prepare_accepts_plan_id_character_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    plan_id: str,
) -> None:
    _write_task(tmp_path)
    monkeypatch.chdir(tmp_path)

    cmd_delivery_prepare(
        _args("tasks/team-invites.md", output=f".sikula/delivery/{plan_id}", json_output=True),
        _cfg(tmp_path),
        context=_authoring_context(),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["selected_plan_id"] == plan_id
    assert payload["paths"]["output_dir"] == f".sikula/delivery/{plan_id}"


def test_cmd_delivery_prepare_uses_fallback_slug_for_empty_task_stem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path, "tasks/---.md")
    monkeypatch.chdir(tmp_path)

    cmd_delivery_prepare(_args("tasks/---.md", json_output=True), _cfg(tmp_path), context=_authoring_context())

    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_plan_id"] == "delivery-plan"
    assert payload["paths"]["output_dir"] == ".sikula/delivery/delivery-plan"


@pytest.mark.parametrize(
    ("case_name", "expected_code", "expected_task_path"),
    [
        ("missing", "delivery_prepare.task_missing", "tasks/missing.md"),
        ("directory", "delivery_prepare.task_not_file", "tasks/directory.md"),
        ("outside", "delivery_prepare.task_outside_project", None),
        ("non_utf8", "delivery_prepare.task_not_utf8", "tasks/non-utf8.md"),
    ],
)
def test_cmd_delivery_prepare_rejects_invalid_task_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case_name: str,
    expected_code: str,
    expected_task_path: str | None,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "tasks" / "directory.md").mkdir(parents=True)
    non_utf8 = project / "tasks" / "non-utf8.md"
    non_utf8.parent.mkdir(parents=True, exist_ok=True)
    non_utf8.write_bytes(b"\xff\xfe")
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    monkeypatch.chdir(project)
    task_files = {
        "missing": "tasks/missing.md",
        "directory": "tasks/directory.md",
        "outside": outside,
        "non_utf8": "tasks/non-utf8.md",
    }

    payload = _blocked_payload(_args(task_files[case_name], json_output=True), _cfg(project), capsys)

    assert payload["ready"] is False
    assert payload["status"] == "blocked"
    assert payload["paths"]["task_file"] == expected_task_path
    assert payload["errors"][0]["code"] == expected_code
    assert payload["message"] == "Delivery prepare is blocked; fix the reported errors and retry."


def test_cmd_delivery_prepare_reports_unreadable_task_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_path = _write_task(tmp_path)
    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):
        if self == task_path.resolve():
            raise OSError("permission denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "read_text", fake_read_text)

    payload = _blocked_payload(_args("tasks/team-invites.md", json_output=True), _cfg(tmp_path), capsys)

    assert payload["errors"][0]["code"] == "delivery_prepare.task_unreadable"
    assert payload["errors"][0]["path"] == "tasks/team-invites.md"


@pytest.mark.parametrize(
    ("task_file", "tasks_config"),
    [
        (".sikula/state/private.json", {}),
        ("private-reports/private.jsonl", {"contract_report_dir": "private-reports"}),
    ],
)
def test_cmd_delivery_prepare_rejects_private_artifact_task_files_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    task_file: str,
    tasks_config: dict[str, str],
) -> None:
    _write_task(tmp_path, task_file, body="PRIVATE PROVIDER INPUT")
    calls: list[dict] = []
    cfg = _cfg(tmp_path)
    cfg["tasks"] = tasks_config
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args(task_file, json_output=True),
        cfg,
        capsys,
        context=_authoring_context(calls=calls),
    )

    assert payload["errors"][0]["code"] == "delivery_prepare.task_runtime_artifact"
    assert calls == []


@pytest.mark.parametrize(
    ("case_name", "expected_code", "expected_path", "expected_plan_id"),
    [
        ("outside", "delivery_prepare.output_absolute", None, "outside-delivery"),
        ("invalid_plan_id", "delivery_prepare.plan_id_invalid", ".sikula/delivery/-bad", None),
        ("invalid_plan_id_char", "delivery_prepare.plan_id_invalid", ".sikula/delivery/bad name", None),
        ("file_collision", "delivery_prepare.output_not_directory", "existing-output", "existing-output"),
        (
            "runtime_artifact",
            "delivery_prepare.output_runtime_artifact",
            ".sikula/state/team-invites",
            "team-invites",
        ),
    ],
)
def test_cmd_delivery_prepare_rejects_invalid_output_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case_name: str,
    expected_code: str,
    expected_path: str | None,
    expected_plan_id: str | None,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_task(project)
    outside = tmp_path / "outside-delivery"
    output_file = project / "existing-output"
    output_file.write_text("not a directory\n", encoding="utf-8")
    monkeypatch.chdir(project)
    outputs = {
        "outside": outside,
        "invalid_plan_id": ".sikula/delivery/-bad",
        "invalid_plan_id_char": ".sikula/delivery/bad name",
        "file_collision": "existing-output",
        "runtime_artifact": ".sikula/state/team-invites",
    }

    payload = _blocked_payload(
        _args("tasks/team-invites.md", output=outputs[case_name], json_output=True),
        _cfg(project),
        capsys,
    )

    assert payload["errors"][0]["code"] == expected_code
    assert payload["errors"][0].get("path") == expected_path
    assert payload["selected_plan_id"] == expected_plan_id
    assert payload["paths"]["output_dir"] == expected_path
    assert str(tmp_path) not in json.dumps(payload)


@pytest.mark.parametrize(
    ("output", "expected_code"),
    [
        pytest.param("../outside", "delivery_prepare.output_traversal", id="posix_parent_traversal"),
        pytest.param("..\\outside", "delivery_prepare.output_traversal", id="windows_parent_traversal"),
        pytest.param("/tmp/outside", "delivery_prepare.output_absolute", id="posix_absolute"),
        pytest.param("C:\\outside", "delivery_prepare.output_absolute", id="windows_absolute"),
        pytest.param(
            ".sikula/state/team-invites",
            "delivery_prepare.output_runtime_artifact",
            id="state_runtime_artifact",
        ),
        pytest.param(
            ".sikula/worktrees/team-invites",
            "delivery_prepare.output_runtime_artifact",
            id="worktree_runtime_artifact",
        ),
        pytest.param(
            ".sikula/contract-reports/team-invites",
            "delivery_prepare.output_runtime_artifact",
            id="contract_report_runtime_artifact",
        ),
        pytest.param(".git/team-invites", "delivery_prepare.output_runtime_artifact", id="git_metadata_artifact"),
        pytest.param(
            ".sikula/delivery/foo..bar",
            "delivery_prepare.final_branch_invalid",
            id="invalid_generated_final_branch",
        ),
    ],
)
def test_cmd_delivery_prepare_rejects_unsafe_output_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output: str,
    expected_code: str,
) -> None:
    _write_task(tmp_path)
    calls: list[dict] = []
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args("tasks/team-invites.md", output=output, json_output=True),
        _cfg(tmp_path),
        capsys,
        context=_authoring_context(calls=calls),
    )

    assert payload["errors"][0]["code"] == expected_code
    assert calls == []
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


def test_cmd_delivery_prepare_rejects_output_symlink_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    delivery_root = tmp_path / ".sikula" / "delivery"
    target = tmp_path / "outside-target"
    delivery_root.mkdir(parents=True)
    target.mkdir()
    (delivery_root / "team-invites").symlink_to(target, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args("tasks/team-invites.md", output=".sikula/delivery/team-invites", json_output=True),
        _cfg(tmp_path),
        capsys,
        context=_authoring_context(),
    )

    payload_text = json.dumps(payload)
    assert payload["errors"][0] == {
        "code": "delivery_prepare.output_symlink",
        "message": "Output directory must not contain symlink components.",
        "path": ".sikula/delivery/team-invites",
        "severity": "error",
    }
    assert payload["existing_artifacts"] == []
    assert str(target) not in payload_text


def test_cmd_delivery_prepare_rejects_output_file_parent_before_authoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    sikula_dir = tmp_path / ".sikula"
    sikula_dir.mkdir()
    delivery_parent = sikula_dir / "delivery"
    delivery_parent.write_text("private parent body\n", encoding="utf-8")
    calls: list[dict] = []
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args("tasks/team-invites.md", output=".sikula/delivery/team-invites", json_output=True),
        _cfg(tmp_path),
        capsys,
        context=_authoring_context(calls=calls),
    )

    payload_text = json.dumps(payload)
    assert payload["errors"][0] == {
        "code": "delivery_prepare.output_not_directory",
        "message": "Output path component already exists and is not a directory.",
        "path": ".sikula/delivery",
        "severity": "error",
    }
    assert payload["existing_artifacts"] == []
    assert calls == []
    assert "private parent body" not in payload_text


def test_cmd_delivery_prepare_blocks_existing_artifacts_without_force_and_allows_with_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    output_dir = tmp_path / ".sikula" / "delivery" / "team-invites"
    units_dir = output_dir / "units"
    units_dir.mkdir(parents=True)
    plan_file = output_dir / "plan.yaml"
    unit_file = units_dir / "foundation.md"
    plan_file.write_text("existing plan\n", encoding="utf-8")
    unit_file.write_text("existing unit\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    blocked = _blocked_payload(
        _args("tasks/team-invites.md", output=".sikula/delivery/team-invites", json_output=True),
        _cfg(tmp_path),
        capsys,
    )

    assert blocked["errors"] == [
        {
            "code": "delivery_prepare.existing_artifacts",
            "message": "Existing delivery plan artifacts require --force to replace.",
            "path": ".sikula/delivery/team-invites",
            "severity": "error",
        }
    ]
    assert blocked["existing_artifacts"] == [
        {"kind": "plan", "path": ".sikula/delivery/team-invites/plan.yaml"},
        {"kind": "unit_task", "path": ".sikula/delivery/team-invites/units/foundation.md"},
    ]

    cmd_delivery_prepare(
        _args("tasks/team-invites.md", output=".sikula/delivery/team-invites", force=True, json_output=True),
        _cfg(tmp_path),
        context=_authoring_context(),
    )
    ready = json.loads(capsys.readouterr().out)

    assert ready["ready"] is True
    assert ready["prepared"] is True
    assert ready["force"] is True
    assert ready["overwrite_allowed"] is True
    assert ready["existing_artifacts"] == blocked["existing_artifacts"]
    assert ready["written_artifacts"] == [
        {"kind": "plan", "path": ".sikula/delivery/team-invites/plan.yaml"},
        {"kind": "unit_task", "path": ".sikula/delivery/team-invites/units/foundation.md"},
        {"kind": "unit_task", "path": ".sikula/delivery/team-invites/units/cli.md"},
    ]
    assert "existing plan" not in plan_file.read_text(encoding="utf-8")
    assert "existing unit" not in unit_file.read_text(encoding="utf-8")
    assert (units_dir / "cli.md").is_file()


def test_cmd_delivery_prepare_blocks_nested_unit_artifacts_without_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    unit_file = tmp_path / ".sikula" / "delivery" / "team-invites" / "units" / "nested" / "unit-b.md"
    unit_file.parent.mkdir(parents=True)
    unit_file.write_text("existing nested unit\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args("tasks/team-invites.md", output=".sikula/delivery/team-invites", json_output=True),
        _cfg(tmp_path),
        capsys,
    )

    assert payload["errors"][0]["code"] == "delivery_prepare.existing_artifacts"
    assert payload["existing_artifacts"] == [
        {"kind": "unit_task", "path": ".sikula/delivery/team-invites/units/nested/unit-b.md"}
    ]
    assert unit_file.read_text(encoding="utf-8") == "existing nested unit\n"


def test_cmd_delivery_prepare_blocks_units_path_file_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    output_dir = tmp_path / ".sikula" / "delivery" / "team-invites"
    output_dir.mkdir(parents=True)
    units_file = output_dir / "units"
    units_file.write_text("not a directory\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args("tasks/team-invites.md", output=".sikula/delivery/team-invites", json_output=True),
        _cfg(tmp_path),
        capsys,
    )

    assert payload["errors"][0]["code"] == "delivery_prepare.existing_artifacts"
    assert payload["existing_artifacts"] == [{"kind": "unit_task", "path": ".sikula/delivery/team-invites/units"}]
    assert units_file.read_text(encoding="utf-8") == "not a directory\n"


def test_cmd_delivery_prepare_blocks_unit_symlink_artifact_without_exposing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    output_dir = tmp_path / ".sikula" / "delivery" / "team-invites"
    units_dir = output_dir / "units"
    units_dir.mkdir(parents=True)
    target = tmp_path / "target-unit.md"
    target.write_text("linked unit body\n", encoding="utf-8")
    (units_dir / "linked-unit.md").symlink_to(target)
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args("tasks/team-invites.md", output=".sikula/delivery/team-invites", json_output=True),
        _cfg(tmp_path),
        capsys,
    )

    payload_text = json.dumps(payload)
    assert payload["errors"][0]["code"] == "delivery_prepare.existing_artifacts"
    assert payload["existing_artifacts"] == [
        {"kind": "unit_task", "path": ".sikula/delivery/team-invites/units/linked-unit.md"}
    ]
    assert str(target) not in payload_text
    assert "linked unit body" not in payload_text


@pytest.mark.parametrize(
    ("case_name", "expected_code"),
    [
        pytest.param("plan_symlink", "delivery_prepare.symlink_artifact", id="plan_symlink"),
        pytest.param("plan_directory", "delivery_prepare.target_not_file", id="plan_directory"),
        pytest.param("units_file", "delivery_prepare.units_dir_not_directory", id="units_file"),
        pytest.param("units_symlink", "delivery_prepare.units_dir_symlink", id="units_symlink"),
        pytest.param("unit_symlink", "delivery_prepare.symlink_artifact", id="unit_symlink"),
    ],
)
def test_cmd_delivery_prepare_blocks_non_replaceable_artifacts_with_force_before_authoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case_name: str,
    expected_code: str,
) -> None:
    _write_task(tmp_path)
    output_dir = tmp_path / ".sikula" / "delivery" / "team-invites"
    output_dir.mkdir(parents=True)
    target = tmp_path / "target-artifact"
    target.write_text("sensitive artifact body\n", encoding="utf-8")
    if case_name == "plan_symlink":
        (output_dir / "plan.yaml").symlink_to(target)
    elif case_name == "plan_directory":
        (output_dir / "plan.yaml").mkdir()
    elif case_name == "units_file":
        (output_dir / "units").write_text("not a directory\n", encoding="utf-8")
    elif case_name == "units_symlink":
        target_dir = tmp_path / "target-units"
        target_dir.mkdir()
        (output_dir / "units").symlink_to(target_dir, target_is_directory=True)
    else:
        units_dir = output_dir / "units"
        units_dir.mkdir()
        (units_dir / "linked-unit.md").symlink_to(target)
    calls: list[dict] = []
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args("tasks/team-invites.md", output=".sikula/delivery/team-invites", force=True, json_output=True),
        _cfg(tmp_path),
        capsys,
        context=_authoring_context(calls=calls),
    )

    payload_text = json.dumps(payload)
    assert payload["errors"][0]["code"] == expected_code
    assert calls == []
    assert str(target) not in payload_text
    assert "sensitive artifact body" not in payload_text


@pytest.mark.parametrize(
    ("agent_model", "agent_provider", "agent_timeout", "message"),
    [
        (["analyst=gpt-5.5"], None, None, "Unknown agent 'analyst'. Valid agents: delivery_preparer"),
        (
            ["delivery_preparer"],
            None,
            None,
            "Invalid --agent-model value 'delivery_preparer'. Expected format: AGENT=VALUE",
        ),
        (
            None,
            ["delivery_preparer"],
            None,
            "Invalid --agent-provider value 'delivery_preparer'. Expected format: AGENT=VALUE",
        ),
        (
            None,
            None,
            ["delivery_preparer=abc"],
            "Invalid --agent-timeout value 'abc' for agent 'delivery_preparer': expected int",
        ),
    ],
)
def test_cmd_delivery_prepare_rejects_invalid_agent_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    agent_model: list[str] | None,
    agent_provider: list[str] | None,
    agent_timeout: list[str] | None,
    message: str,
) -> None:
    _write_task(tmp_path)
    calls: list[dict] = []
    monkeypatch.chdir(tmp_path)

    payload = _blocked_payload(
        _args(
            "tasks/team-invites.md",
            json_output=True,
            agent_model=agent_model,
            agent_provider=agent_provider,
            agent_timeout=agent_timeout,
        ),
        _cfg(tmp_path),
        capsys,
        context=_authoring_context(calls=calls),
    )

    assert payload["errors"][0] == {
        "code": "delivery_prepare.agent_override_invalid",
        "message": message,
        "path": None,
        "severity": "error",
    }
    assert calls == []
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


def test_delivery_preparer_overrides_are_separate_from_task_preparer(capsys: pytest.CaptureFixture[str]) -> None:
    overrides = parse_agent_llm_overrides(
        ["delivery-preparer=gpt-5.5"],
        ["delivery_preparer=claude"],
        ["delivery-preparer=1200"],
        valid_agents=DELIVERY_PREPARATION_AGENT_NAMES,
    )

    assert overrides == {
        "delivery_preparer": {
            "agent_timeout": 1200,
            "model": "gpt-5.5",
            "provider": "claude",
        }
    }

    with pytest.raises(SystemExit) as exc:
        parse_agent_llm_overrides(["delivery_preparer=gpt-5.5"], None, None, valid_agents=PREPARATION_AGENT_NAMES)

    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert "Unknown agent 'delivery_preparer'" in out
    assert "Valid agents: task_preparer" in out

    with pytest.raises(SystemExit) as runtime_exc:
        parse_agent_llm_overrides(["delivery_preparer=gpt-5.5"], None, None)

    out = capsys.readouterr().out
    assert runtime_exc.value.code == 1
    assert "Unknown agent 'delivery_preparer'" in out
    assert "delivery_preparer" not in out.split("Valid agents:", 1)[1]


def test_render_delivery_prepare_includes_errors_and_warnings() -> None:
    result = DeliveryPrepareResult(
        status="blocked",
        ready=False,
        prepared=False,
        force=True,
        overwrite_allowed=True,
        selected_plan_id=None,
        unit_ids=[],
        paths={
            "task_file": None,
            "output_dir": None,
            "plan_file": None,
            "units_dir": None,
        },
        existing_artifacts=[DeliveryPrepareArtifact("plan", ".sikula/delivery/demo/plan.yaml")],
        unit_task_paths={"unit-a": ".sikula/delivery/demo/units/unit-a.md"},
        written_artifacts=[
            DeliveryPrepareArtifact("plan", ".sikula/delivery/demo/plan.yaml"),
            DeliveryPrepareArtifact("unit_task", ".sikula/delivery/demo/units/unit-a.md"),
        ],
        plan_validation={"status": "valid", "valid": True, "errors": [], "warnings": []},
        unit_readiness={
            "status": "ready",
            "units": [
                {
                    "unit_id": "unit-a",
                    "path": ".sikula/delivery/demo/units/unit-a.md",
                    "readiness_score": 100,
                    "status": "ready",
                    "ready_for_autonomous_delivery": True,
                    "blocking_gap_count": 0,
                    "warning_gap_count": 0,
                    "blocking_gap_ids": [],
                }
            ],
        },
        errors=[DeliveryPrepareIssue("error", "delivery_prepare.task_outside_project", "Task path must be inside.")],
        warnings=[
            DeliveryPrepareIssue(
                "warning",
                "delivery_prepare.preview_only",
                "No artifacts will be written.",
                ".sikula/delivery/demo",
            )
        ],
        message="Delivery prepare is blocked; fix the reported errors and retry.",
    )

    output = render_delivery_prepare(result)

    assert "Delivery prepare: <unknown>" in output
    assert "Overwrite allowed: yes" in output
    assert "Draft units: 0" in output
    assert "Existing artifacts:" in output
    assert "- plan: .sikula/delivery/demo/plan.yaml" in output
    assert "Written artifacts:" in output
    assert "- unit_task: .sikula/delivery/demo/units/unit-a.md" in output
    assert "Unit task paths:" in output
    assert "- unit-a: .sikula/delivery/demo/units/unit-a.md" in output
    assert "Plan validation:" in output
    assert "- status: valid" in output
    assert "- valid: yes" in output
    assert "Unit readiness:" in output
    assert "- unit-a: ready, score 100, blocking 0, warnings 0" in output
    assert "Errors:" in output
    assert "- delivery_prepare.task_outside_project: Task path must be inside." in output
    assert "Warnings:" in output
    assert "- delivery_prepare.preview_only [.sikula/delivery/demo]: No artifacts will be written." in output
