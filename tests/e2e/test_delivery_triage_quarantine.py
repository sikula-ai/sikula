"""Exercise confirmed-triage quarantine with real validation and isolated delivery."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import shlex
import subprocess
import sys
from unittest.mock import patch

import pytest
import yaml

from core.llm_client import LLMConfig, _call_observed
from core.state import JsonStateStore
from sikula import main
from tests.delivery_fixtures import delivery_source
from tests.e2e.test_delivery_plan import (
    _git_commit_all,
    _write_delivery_plan,
    _write_delivery_unit,
    _write_handoff_smoke_config,
)
from tools.delivery_quarantine_tool import delivery_quarantine_supported, task_quarantine_summary


@pytest.mark.skipif(not delivery_quarantine_supported(), reason="reversible quarantine is unavailable")
def test_delivery_triage_quarantines_before_next_validation(git_project, fake_llm, monkeypatch, capsys):
    root = git_project
    original = "def subtract(a, b):\n    return a - b\n"
    (root / "tests_proj/test_architecture.py").write_text(
        "from pathlib import Path\n\n"
        "def test_subtraction_layer():\n"
        "    assert not Path('src/obsolete.py').exists(), 'Subtraction belongs in its own module'\n"
        "    from subtraction import subtract\n"
        "    assert subtract(5, 3) == 2\n",
        encoding="utf-8",
    )
    unit = _write_delivery_unit(
        root,
        "subtract.md",
        "# Subtraction\n\nImplement subtraction in src/subtraction.py, preserving addition. "
        "The existing architecture test must pass; leave no obsolete production files.\n",
    )
    plan_id = "triage-quarantine"
    plan = _write_delivery_plan(
        root,
        {
            "schema_version": 2,
            "plan_id": plan_id,
            "title": "Subtraction module",
            "source_task": delivery_source(root),
            "verification": {"mode": "final_gate"},
            "final_branch": f"sikula/delivery/{plan_id}",
            "units": [{"id": "subtract", "task_path": unit, "depends_on": [], "scope_paths": ["src/"]}],
        },
    )
    _write_handoff_smoke_config(root)
    config_path = root / ".sikula/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config.update(run_build=True, run_tests=True, run_review=True, run_security_review=True)
    python = shlex.quote(sys.executable)
    config["build"].update(
        compile_command=f"{python} -m compileall -q src/",
        test_command=f"{python} -m pytest -q -p no:cacheprovider tests_proj/",
    )
    config["sandbox"]["max_iterations"] = 2
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    _git_commit_all(root, "Add triage quarantine fixture")
    index_before = (root / ".git/index").read_bytes()
    fake = fake_llm()
    fake._config = LLMConfig(provider="test-provider", model="test-model")
    calls = []
    original_readonly = fake.run_readonly_agent

    def review(prompt: str, cwd: Path):
        if "Produce the implementation prompt." in prompt:
            return original_readonly(prompt, cwd)
        return json.dumps(
            {"sikula_disposition_schema_version": 1, "disposition": "approved", "summary": "Review completed."}
        )

    def write(prompt: str, cwd: Path):
        def invoke():
            calls.append(prompt)
            if len(calls) == 1:
                (cwd / "src/obsolete.py").write_text(original, encoding="utf-8")
                return ["src/obsolete.py"], "Implemented subtraction."
            assert (cwd / "src/obsolete.py").read_text() == original
            if len(calls) == 2:
                assert "quarantine_untracked" not in prompt
                return [], (
                    "TEST FAILURE TRIAGE:\nclassification: production_defect\n"
                    "contract_affected: subtraction module ownership\nchosen_fix: production_code\n"
                )
            assert len(calls) == 3, "No extra Fixer cycle should be necessary"
            assert "CONFIRMED TEST FAILURE TRIAGE" in prompt
            assert "quarantine_untracked" in prompt
            (cwd / "src/subtraction.py").write_text(original, encoding="utf-8")
            return ["src/subtraction.py"], json.dumps(
                {
                    "sikula_file_operation_schema_version": 1,
                    "operation": "quarantine_untracked",
                    "path": "src/obsolete.py",
                }
            )

        return _call_observed(
            fake._config, operation="run_agent", attempt=1, max_attempts=1, input_chars=len(prompt), fn=invoke
        )

    fake.run_agent = write
    fake.run_readonly_agent = review
    monkeypatch.chdir(root)
    with patch("core.llm_client.create_llm_client", return_value=fake):
        with patch("sys.argv", ["sikula", "delivery", "run-next", str(plan), "--json"]):
            main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["succeeded"], payload
    assert payload["unit_status"] == "done"
    child = JsonStateStore(root / ".sikula/state").load(payload["child_task_id"])
    assert child.done and not child.failed
    assert len(calls) == 3
    assert [record["triage_pass"] for record in child.fix_cycle_records] == ["test_only", "production_confirmed"]
    assert "file_quarantined" not in child.fix_cycle_records[0]
    assert child.fix_cycle_records[1]["file_quarantined"]["path"] == "src/obsolete.py"
    assert child.review_approved and child.security_approved
    assert child.test_status == "success"
    assert [record["status"] for record in child.validation_cycle_records if record["phase"] == "test"] == [
        "failed",
        "success",
    ]
    assert [record["status"] for record in child.validation_cycle_records if record["phase"] == "build"] == [
        "success",
        "success",
    ]
    assert task_quarantine_summary(root, child.task_id) == (1, len(original.encode("utf-8")))
    record = child.delivery_quarantine_records[0]
    assert record["status"] == "quarantined"
    retained = (
        root
        / ".git/sikula/quarantine"
        / sha256(child.task_id.encode("utf-8")).hexdigest()
        / record["quarantine_id"]
        / "entry"
    )
    assert retained.read_text() == original
    committed_paths = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", child.result_commit],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "src/subtraction.py" in committed_paths
    assert "src/obsolete.py" not in committed_paths
    assert (root / ".git/index").read_bytes() == index_before
    assert not (root / "src/subtraction.py").exists()
