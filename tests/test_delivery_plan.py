from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
import yaml

from core.delivery_plan import DeliveryPlanIssue, check_delivery_plan_file, render_delivery_plan_check
from sikula import main
from sikula_cli.delivery import cmd_delivery_check


def test_delivery_cli_module_imports() -> None:
    import sikula_cli.delivery as delivery_cli

    assert callable(delivery_cli.register_parser)


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)


def _write_unit(root: Path, name: str, text: str = "# Unit\n") -> str:
    path = root / ".sikula" / "delivery" / "demo" / "units" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path.relative_to(root).as_posix()


def _write_plan(root: Path, data: dict) -> Path:
    path = root / ".sikula" / "delivery" / "demo" / "plan.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _base_plan(root: Path) -> dict:
    unit_1 = _write_unit(root, "01-domain.md")
    unit_2 = _write_unit(root, "02-api.md")
    return {
        "schema_version": 1,
        "plan_id": "checkout-redesign",
        "title": "Checkout redesign",
        "planning_mode": "fixed_window",
        "final_branch": "sikula/delivery/checkout-redesign",
        "streams": [{"id": "backend", "label": "Backend"}],
        "units": [
            {
                "id": "01-domain",
                "title": "Add checkout domain model",
                "stream": "backend",
                "platform": "shared",
                "task_path": unit_1,
                "depends_on": [],
            },
            {
                "id": "02-api",
                "title": "Add checkout API",
                "stream": "backend",
                "platform": "shared",
                "task_path": unit_2,
                "depends_on": ["01-domain"],
            },
        ],
    }


def _codes(result) -> set[str]:
    return {issue.code for issue in result.errors}


def test_delivery_plan_check_accepts_valid_single_repo_plan(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, _base_plan(tmp_path))

    result = check_delivery_plan_file(plan_path)

    assert result.valid is True
    assert result.plan is not None
    assert result.plan.plan_id == "checkout-redesign"
    assert result.plan.repositories[0].id == "main"
    assert result.plan.repositories[0].implicit is True
    assert result.plan.units[1].depends_on == ["01-domain"]
    rendered = render_delivery_plan_check(result)
    assert "Status: valid" in rendered
    assert "Units: 2" in rendered


def test_delivery_plan_check_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["schema_version"] = 2
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "schema_version.unsupported" in _codes(result)


def test_delivery_plan_check_rejects_plan_id_that_cannot_be_used_for_state_path(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["plan_id"] = "../bad"
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "plan_id.invalid" in _codes(result)


def test_delivery_plan_check_reports_duplicate_and_unknown_dependencies(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][1]["id"] = "01-domain"
    data["units"][1]["depends_on"] = ["missing-unit"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.duplicate_id" in _codes(result)
    assert "dependencies.unknown_unit" in _codes(result)


def test_delivery_plan_check_preserves_unit_index_for_dependency_errors_after_skipped_units(
    tmp_path: Path,
) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["id"] = ""
    data["units"][1]["depends_on"] = ["missing-unit"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    dependency_errors = [issue for issue in result.errors if issue.code == "dependencies.unknown_unit"]
    assert len(dependency_errors) == 1
    assert dependency_errors[0].path == "units[1].depends_on[0]"


def test_delivery_plan_check_reports_dependency_cycles(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["depends_on"] = ["02-api"]
    data["units"][1]["depends_on"] = ["01-domain"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "dependencies.cycle" in _codes(result)


def test_delivery_plan_check_rejects_missing_and_escaping_task_paths(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["task_path"] = ".sikula/delivery/demo/units/missing.md"
    data["units"][1]["task_path"] = "../outside.md"
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.task_path_missing" in _codes(result)
    assert "units.task_path_outside_project" in _codes(result)


def test_delivery_plan_check_reports_invalid_task_path_string(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["task_path"] = "bad\0"
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    invalid_path_errors = [issue for issue in result.errors if issue.code == "units.task_path_invalid"]
    assert len(invalid_path_errors) == 1
    assert invalid_path_errors[0].path == "units[0].task_path"
    assert "bad" not in invalid_path_errors[0].message


def test_delivery_plan_check_rejects_multi_repo_plan_before_multi_repo_support(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["repositories"] = [
        {"id": "android", "root": "."},
        {"id": "ios", "root": "../ios"},
    ]
    data["units"][1]["repo_id"] = "ios"
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "repositories.multiple_unsupported" in _codes(result)
    assert "units.repo_id_unknown" in _codes(result)


def test_delivery_plan_check_allows_explicit_single_repo(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["repositories"] = [{"id": "main", "root": "."}]
    data["units"][0]["repo_id"] = "main"
    data["units"][1]["repo_id"] = "main"
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is True
    assert result.plan is not None
    assert result.plan.repositories[0].implicit is False


def test_delivery_plan_check_rejects_unknown_stream_when_streams_are_declared(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][1]["stream"] = "web"
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.stream_unknown" in _codes(result)


def test_delivery_plan_check_warns_when_unit_stream_is_missing(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    del data["units"][1]["stream"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is True
    assert [warning.code for warning in result.warnings] == ["units.stream_missing"]
    rendered = render_delivery_plan_check(result)
    assert "Warnings:" in rendered
    assert "units.stream_missing" in rendered


def test_delivery_plan_check_resolves_relative_plan_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, _base_plan(tmp_path))
    monkeypatch.chdir(tmp_path)

    result = check_delivery_plan_file(plan_path.relative_to(tmp_path))

    assert result.valid is True
    assert result.plan_path == str(plan_path.resolve())


def test_delivery_plan_check_reports_missing_plan_file(tmp_path: Path) -> None:
    _git_init(tmp_path)

    result = check_delivery_plan_file(tmp_path / ".sikula" / "delivery" / "missing.yaml")

    assert result.valid is False
    assert "plan.missing" in _codes(result)


def test_delivery_plan_check_reports_plan_path_that_is_not_file(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_dir = tmp_path / ".sikula" / "delivery"
    plan_dir.mkdir(parents=True)

    result = check_delivery_plan_file(plan_dir)

    assert result.valid is False
    assert "plan.not_file" in _codes(result)


def test_delivery_plan_check_reports_invalid_yaml(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = tmp_path / ".sikula" / "delivery" / "demo" / "plan.yaml"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("schema_version: [unterminated\nsecret: OPENAI_API_KEY=sk-test\n", encoding="utf-8")

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "plan.parse_failed" in _codes(result)
    message = result.errors[0].message
    assert "ParserError" in message
    assert "line " in message
    assert "OPENAI_API_KEY" not in message
    assert "sk-test" not in message
    assert "unterminated" not in message


def test_delivery_plan_check_reports_non_utf8_plan_file(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = tmp_path / ".sikula" / "delivery" / "demo" / "plan.yaml"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_bytes(b"\xff\xfe\x00")

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "plan.read_failed" in _codes(result)


def test_delivery_plan_check_reports_non_mapping_yaml(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = tmp_path / ".sikula" / "delivery" / "demo" / "plan.yaml"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "plan.invalid_type" in _codes(result)


def test_delivery_plan_check_reports_missing_git_root(tmp_path: Path) -> None:
    plan_path = _write_plan(tmp_path, _base_plan(tmp_path))

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "project.git_root_missing" in _codes(result)


def test_delivery_plan_check_reports_plan_outside_supplied_project_root(tmp_path: Path) -> None:
    _git_init(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    plan_path = _write_plan(tmp_path, _base_plan(tmp_path))

    result = check_delivery_plan_file(plan_path, project_root=other)

    assert result.valid is False
    assert "plan.path_outside_project" in _codes(result)


def test_delivery_plan_check_reports_invalid_repository_shapes(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["repositories"] = {"id": "main"}
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "repositories.invalid_type" in _codes(result)


def test_delivery_plan_check_reports_empty_repositories(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["repositories"] = []
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "repositories.empty" in _codes(result)


def test_delivery_plan_check_reports_invalid_repository_entry(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["repositories"] = ["main"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "repositories.item_invalid" in _codes(result)


def test_delivery_plan_check_rejects_non_root_single_repository(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["repositories"] = [{"id": "main", "root": "packages/app"}]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "repositories.root_unsupported" in _codes(result)


def test_delivery_plan_check_reports_invalid_stream_shapes(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["streams"] = {"id": "backend"}
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "streams.invalid_type" in _codes(result)


def test_delivery_plan_check_reports_invalid_and_duplicate_stream_entries(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["streams"] = ["backend", 123, {"id": "backend"}, {"label": "No ID"}]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "streams.item_invalid" in _codes(result)
    assert "streams.duplicate_id" in _codes(result)
    assert "streams[3].id.missing" in _codes(result)


def test_delivery_plan_check_reports_invalid_units_container(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"] = {"id": "unit"}
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.invalid_type" in _codes(result)


def test_delivery_plan_check_reports_empty_units(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"] = []
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.empty" in _codes(result)


def test_delivery_plan_check_reports_invalid_unit_entry_and_fields(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"] = [
        "bad unit",
        {
            "id": "",
            "title": 123,
            "task_path": 42,
            "depends_on": "01-domain",
            "stream": "",
            "platform": 123,
            "phase": 123,
            "kind": 123,
            "repo_id": 123,
        },
    ]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.item_invalid" in _codes(result)
    assert "units[1].id.missing" in _codes(result)
    assert "units[1].title.invalid_type" in _codes(result)
    assert "units[1].task_path.missing" in _codes(result)
    assert "units[1].depends_on.invalid_type" in _codes(result)
    assert "units[1].stream.invalid_type" in _codes(result)
    assert "units[1].platform.invalid_type" in _codes(result)
    assert "units[1].phase.invalid_type" in _codes(result)
    assert "units[1].kind.invalid_type" in _codes(result)
    assert "units[1].repo_id.invalid_type" in _codes(result)


def test_delivery_plan_check_reports_invalid_dependency_entries(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][1]["depends_on"] = ["01-domain", ""]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units[1].depends_on.item_invalid" in _codes(result)


def test_delivery_plan_check_reports_absolute_task_path(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["task_path"] = str(tmp_path / ".sikula" / "delivery" / "demo" / "units" / "01-domain.md")
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "units.task_path_absolute" in _codes(result)


def test_delivery_plan_check_reports_self_dependency(tmp_path: Path) -> None:
    _git_init(tmp_path)
    data = _base_plan(tmp_path)
    data["units"][0]["depends_on"] = ["01-domain"]
    plan_path = _write_plan(tmp_path, data)

    result = check_delivery_plan_file(plan_path)

    assert result.valid is False
    assert "dependencies.self_reference" in _codes(result)


def test_delivery_plan_result_json_includes_issue_paths(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, {"schema_version": "1"})

    result = check_delivery_plan_file(plan_path)
    payload = result.to_dict()

    assert payload["errors"][0]["path"]


def test_delivery_plan_issue_json_omits_empty_path() -> None:
    assert DeliveryPlanIssue("error", "code", "message").to_dict() == {
        "severity": "error",
        "code": "code",
        "message": "message",
    }


def test_delivery_plan_check_json_output_does_not_embed_unit_task_contents(tmp_path: Path, capsys) -> None:
    _git_init(tmp_path)
    secret_text = "# Unit\n\nDo not echo this raw unit body.\n"
    data = _base_plan(tmp_path)
    data["units"][0]["task_path"] = _write_unit(tmp_path, "01-domain.md", secret_text)
    plan_path = _write_plan(tmp_path, data)

    cmd_delivery_check(argparse.Namespace(plan_file=str(plan_path), json=True), {})

    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert "Do not echo this raw unit body" not in json.dumps(payload)


def test_delivery_check_cli_exits_nonzero_for_invalid_plan(tmp_path: Path, capsys) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, {"schema_version": 1})

    with pytest.raises(SystemExit) as exc:
        cmd_delivery_check(argparse.Namespace(plan_file=str(plan_path), json=False), {})

    assert exc.value.code == 1
    assert "Status: invalid" in capsys.readouterr().out


def test_main_dispatches_delivery_check_without_loading_project_config(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text("schema_version: 1\n", encoding="utf-8")

    with patch("sys.argv", ["sikula", "delivery", "check", str(plan_path)]):
        with patch("sikula._load_runtime_config", return_value={}) as load_config:
            with patch("sikula.cmd_delivery_check") as delivery_check:
                main()

    load_config.assert_not_called()
    delivery_check.assert_called_once()
