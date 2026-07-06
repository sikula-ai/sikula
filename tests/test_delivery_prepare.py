from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import sikula
from sikula_cli.agent_overrides import (
    DELIVERY_PREPARATION_AGENT_NAMES,
    PREPARATION_AGENT_NAMES,
    parse_agent_llm_overrides,
)
from sikula_cli.delivery import (
    DeliveryPrepareArtifact,
    DeliveryPrepareIssue,
    DeliveryPrepareResult,
    cmd_delivery_prepare,
    register_parser,
    render_delivery_prepare,
)


def _cfg(root: Path) -> dict:
    return {"project": {"root_path": str(root)}}


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


def _blocked_payload(args: argparse.Namespace, cfg: dict, capsys: pytest.CaptureFixture[str]) -> dict:
    with pytest.raises(SystemExit) as exc:
        cmd_delivery_prepare(args, cfg)

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


def test_cmd_delivery_prepare_ready_text_uses_default_output_and_writes_no_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path, "tasks/Team Invites.md", "# Team invites\n\nDo not echo this raw task body.\n")
    monkeypatch.chdir(tmp_path)

    cmd_delivery_prepare(_args("tasks/Team Invites.md"), _cfg(tmp_path))

    out = capsys.readouterr().out
    assert "Delivery prepare: tasks/Team Invites.md" in out
    assert "Status: ready" in out
    assert "Selected plan: team-invites" in out
    assert "Output: .sikula/delivery/team-invites" in out
    assert "Plan file: .sikula/delivery/team-invites/plan.yaml" in out
    assert "Units dir: .sikula/delivery/team-invites/units" in out
    assert "Overwrite allowed: no" in out
    assert "Delivery prepare command surface is ready; no delivery artifacts were written in this unit." in out
    assert "Do not echo this raw task body" not in out
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


def test_sikula_cmd_delivery_prepare_wrapper_delegates_to_cli(tmp_path: Path) -> None:
    args = _args("tasks/team-invites.md")
    cfg = _cfg(tmp_path)

    with patch("sikula.cli_delivery.cmd_delivery_prepare", return_value="prepared") as prepare:
        result = sikula.cmd_delivery_prepare(args, cfg)

    assert result == "prepared"
    prepare.assert_called_once_with(args, cfg)


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
    )

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload == {
        "errors": [],
        "existing_artifacts": [],
        "force": False,
        "message": "Delivery prepare command surface is ready; no delivery artifacts were written in this unit.",
        "overwrite_allowed": False,
        "paths": {
            "output_dir": ".sikula/delivery/team-invites",
            "plan_file": ".sikula/delivery/team-invites/plan.yaml",
            "task_file": "tasks/team-invites.md",
            "units_dir": ".sikula/delivery/team-invites/units",
        },
        "prepared": False,
        "ready": True,
        "selected_plan_id": "team-invites",
        "status": "ready",
        "unit_ids": [],
        "warnings": [],
    }
    assert str(tmp_path) not in out
    assert "PRIVATE TASK BODY" not in out
    assert not (tmp_path / ".sikula" / "delivery" / "team-invites").exists()


def test_cmd_delivery_prepare_resolves_relative_paths_from_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    workdir = tmp_path / "nested"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    cmd_delivery_prepare(
        _args("../tasks/team-invites.md", output="../.sikula/delivery/custom.plan", json_output=True),
        _cfg(tmp_path),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["selected_plan_id"] == "custom.plan"
    assert payload["paths"] == {
        "output_dir": ".sikula/delivery/custom.plan",
        "plan_file": ".sikula/delivery/custom.plan/plan.yaml",
        "task_file": "tasks/team-invites.md",
        "units_dir": ".sikula/delivery/custom.plan/units",
    }
    assert not (tmp_path / ".sikula" / "delivery" / "custom.plan").exists()


def test_cmd_delivery_prepare_uses_cwd_when_config_root_is_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    monkeypatch.chdir(tmp_path)

    cmd_delivery_prepare(_args("tasks/team-invites.md", json_output=True), {})

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

    cmd_delivery_prepare(_args("tasks/---.md", json_output=True), _cfg(tmp_path))

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
    ("case_name", "expected_code", "expected_path", "expected_plan_id"),
    [
        ("outside", "delivery_prepare.output_outside_project", None, "outside-delivery"),
        ("invalid_plan_id", "delivery_prepare.plan_id_invalid", ".sikula/delivery/-bad", None),
        ("invalid_plan_id_char", "delivery_prepare.plan_id_invalid", ".sikula/delivery/bad name", None),
        ("file_collision", "delivery_prepare.output_not_directory", "existing-output", "existing-output"),
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
    unit_file = units_dir / "unit-a.md"
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
        {"kind": "unit_task", "path": ".sikula/delivery/team-invites/units/unit-a.md"},
    ]

    cmd_delivery_prepare(
        _args("tasks/team-invites.md", output=".sikula/delivery/team-invites", force=True, json_output=True),
        _cfg(tmp_path),
    )
    ready = json.loads(capsys.readouterr().out)

    assert ready["ready"] is True
    assert ready["force"] is True
    assert ready["overwrite_allowed"] is True
    assert ready["existing_artifacts"] == blocked["existing_artifacts"]
    assert plan_file.read_text(encoding="utf-8") == "existing plan\n"
    assert unit_file.read_text(encoding="utf-8") == "existing unit\n"


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
    )

    assert payload["errors"][0] == {
        "code": "delivery_prepare.agent_override_invalid",
        "message": message,
        "severity": "error",
    }
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
    assert "Existing artifacts:" in output
    assert "- plan: .sikula/delivery/demo/plan.yaml" in output
    assert "Errors:" in output
    assert "- delivery_prepare.task_outside_project: Task path must be inside." in output
    assert "Warnings:" in output
    assert "- delivery_prepare.preview_only [.sikula/delivery/demo]: No artifacts will be written." in output
