"""Test smoke wiring with fake providers; no paid calls in automated tests."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import subprocess

import pytest

import sikula
from tests.test_delivery_hierarchical_preparation import TreeLLM
from tools import smoke_hierarchical_preparation as smoke


@pytest.fixture
def fixture(tmp_path):
    return smoke.create_fixture(tmp_path / "smoke", "codex", "fake", 30)


def test_fixture_is_isolated_and_does_not_stage_files(fixture):
    smoke.check_fixture(fixture)
    result = subprocess.run(["git", "ls-files"], cwd=fixture, capture_output=True, check=True)
    assert result.stdout == b""
    baseline = subprocess.run(
        [smoke.sys.executable, "-m", "unittest", "discover", "-s", "tests_proj", "-v"],
        cwd=fixture,
        capture_output=True,
    )
    assert baseline.returncode == 0, baseline.stderr
    with pytest.raises(FileExistsError):
        smoke.create_fixture(fixture, "codex", "fake", 30)


def test_smoke_exercises_cli_readiness_audit_and_exact_replay(fixture, monkeypatch):
    class FixtureLLM(TreeLLM):
        def generate(self, system, prompt):
            value = json.loads(super().generate(system, prompt))
            if "preparation_mode" in value:
                value["plan_id"] = smoke.PLAN_ID
            for unit in value.get("units", []):
                unit["scope_paths"] = ["warehouse/"]
                unit["task_markdown"] = unit["task_markdown"].replace(
                    "- `pytest`\n- `ruff check .`", "- `python -m unittest discover -s tests_proj -v`"
                )
            return json.dumps(value)

    llm = FixtureLLM(depth=1)
    monkeypatch.setattr("core.llm_client.create_llm_client", lambda _: llm)
    real_run = subprocess.run

    def cli(command, **kwargs):
        if len(command) < 2 or command[1] != str(smoke.REPOSITORY / "sikula.py"):
            return real_run(command, **kwargs)
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        with monkeypatch.context() as patch, redirect_stdout(stdout), redirect_stderr(stderr):
            patch.chdir(kwargs["cwd"])
            patch.setattr("sys.argv", command[1:])
            try:
                sikula.main()
            except SystemExit as exc:
                code = exc.code
        kwargs["stdout"].write(stdout.getvalue().encode("utf-8"))
        kwargs["stderr"].write(stderr.getvalue().encode("utf-8"))
        return subprocess.CompletedProcess(command, code)

    monkeypatch.setattr(smoke.subprocess, "run", cli)
    report = smoke.run_smoke(fixture)
    assert report["passed"], report
    assert report["reused_without_new_calls"] and report["artifacts_unchanged"]
    assert report["metrics"]["audited_exchanges"] == len(llm.calls)
    assert report["metrics"]["max_prompt_bytes"] > 0
    assert "Local contract" not in json.dumps(report)
    assert not report["implementation_executed"]


@pytest.mark.parametrize("failure", ["blocked", "flat", "missing_audit", "new_call", "changed_artifact"])
def test_smoke_does_not_report_false_success(fixture, monkeypatch, failure):
    attempts = []

    def invoke(root, label):
        attempts.append(label)
        if failure == "blocked":
            return 1, {"ready": False, "errors": [{"code": "delivery_prepare.hierarchy_authority_changed"}]}, 0.1
        (root / smoke.PLAN_DIR).mkdir(parents=True, exist_ok=True)
        smoke.write_json(root / smoke.PLAN_DIR / "plan.yaml", {"units": [{}], "checkpoints": [{}]})
        if failure != "flat":
            (root / smoke.CONTROL).parent.mkdir(parents=True, exist_ok=True)
            calls = [{"status": "accepted"}]
            if label == "replay" and failure == "new_call":
                calls.append({"status": "accepted"})
            smoke.write_json(root / smoke.CONTROL, {"calls": calls})
        if label == "replay" and failure == "changed_artifact":
            (root / smoke.PLAN_DIR / "unexpected.md").write_text("changed", encoding="utf-8")
        return 0, {"ready": True, "prepared": True}, 0.1

    monkeypatch.setattr(smoke, "invoke_prepare", invoke)
    if failure != "missing_audit":
        monkeypatch.setattr(
            smoke,
            "audit_metrics",
            lambda _: {
                "audited_exchanges": 3,
                "exchanges_by_stage": {"author": 1, "local_review": 1, "integration_review": 1},
            },
        )
    report = smoke.run_smoke(fixture)
    assert not report["passed"]
    if failure == "blocked":
        assert report["prepare_error_codes"] == ["delivery_prepare.hierarchy_authority_changed"]
    assert len(attempts) == (1 if failure in {"blocked", "flat", "missing_audit"} else 2)
