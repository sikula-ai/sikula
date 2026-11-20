"""Small CLI acceptance cases distinguishing automatic recovery from operator reset."""

from __future__ import annotations

import json
import os
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


@pytest.mark.parametrize("fault", ["test_failure", "provider_failure", "review_protocol"])
def test_delivery_recovers_without_repeating_completed_work(git_project, fake_llm, monkeypatch, capsys, fault):
    root = git_project
    units = []
    for name, dependencies in (("foundation", []), ("consumer", ["foundation"])):
        task = _write_delivery_unit(
            root, f"{name}.md", f"# {name.title()}\n\nAdd {name} support while preserving calculator addition.\n"
        )
        units.append({"id": name, "task_path": task, "depends_on": dependencies, "scope_paths": ["src/"]})
    plan = _write_delivery_plan(
        root,
        {
            "schema_version": 2,
            "plan_id": "autonomy",
            "title": "Recovery acceptance",
            "source_task": delivery_source(root),
            "verification": {"mode": "final_gate"},
            "final_branch": "sikula/delivery/autonomy",
            "units": units,
        },
    )
    _write_handoff_smoke_config(root)
    config_path = root / ".sikula/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config.update(run_build=True, run_tests=True, run_review=True, run_security_review=True)
    python = subprocess.list2cmdline([sys.executable]) if os.name == "nt" else shlex.quote(sys.executable)
    config["build"].update(
        compile_command=f"{python} -m compileall -q src/",
        test_command=f"{python} -m pytest -q -p no:cacheprovider tests_proj/",
    )
    config["sandbox"]["max_iterations"] = 2
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    _git_commit_all(root, "Add recovery acceptance fixture")
    index_before = (root / ".git/index").read_bytes()
    head_before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root)
    progress_path = root / ".sikula/state/delivery/autonomy/progress.json"
    fake = fake_llm()
    fake._config = LLMConfig(provider="test-provider", model="test-model")
    original_readonly = fake.run_readonly_agent
    writes = []
    gates = []

    def review(prompt: str, cwd: Path):
        if "Produce the implementation prompt." in prompt:
            return original_readonly(prompt, cwd)
        if "Review whether the complete assembled candidate" in prompt:
            gates.append(prompt)
            assert (cwd / "src/foundation.py").exists()
            assert (cwd / "src/consumer.py").exists()
            if fault == "review_protocol" and len(gates) == 1:
                return "APPROVED"
            return json.dumps(
                {
                    "schema_version": 2,
                    "disposition": "approved",
                    "summary": "Integration verified.",
                    "findings": [],
                    "obligation_results": [],
                }
            )
        return json.dumps(
            {"sikula_disposition_schema_version": 1, "disposition": "approved", "summary": "Review completed."}
        )

    def write(prompt: str, cwd: Path):
        def invoke():
            writes.append(prompt)
            if len(writes) == 1:
                (cwd / "src/foundation.py").write_text("VALUE = 1\n", encoding="utf-8")
                return ["src/foundation.py"], "Foundation implemented."
            assert (cwd / "src/foundation.py").read_text() == "VALUE = 1\n"
            if fault == "provider_failure" and len(writes) == 2:
                raise RuntimeError("Synthetic provider failure")
            if fault == "test_failure":
                if len(writes) == 3:
                    return [], (
                        "TEST FAILURE TRIAGE:\nclassification: production_defect\n"
                        "contract_affected: preserve addition\nchosen_fix: production_code\n"
                    )
                if len(writes) == 4:
                    assert "CONFIRMED TEST FAILURE TRIAGE" in prompt
                    (cwd / "src/calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
                    return ["src/calculator.py"], "Addition restored."
                assert len(writes) == 2
                (cwd / "src/calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            (cwd / "src/consumer.py").write_text("from foundation import VALUE\n", encoding="utf-8")
            paths = ["src/consumer.py"]
            if fault == "test_failure":
                paths.append("src/calculator.py")
            return paths, "Consumer implemented."

        return _call_observed(
            fake._config, operation="run_agent", attempt=1, max_attempts=1, input_chars=len(prompt), fn=invoke
        )

    fake.run_agent = write
    fake.run_readonly_agent = review
    monkeypatch.chdir(root)

    def run(*options, succeeds=True):
        with patch("sys.argv", ["sikula", "delivery", "run", str(plan), *options, "--json"]):
            if succeeds:
                main()
            else:
                with pytest.raises(SystemExit) as exc:
                    main()
                assert exc.value.code == 1
        return json.loads(capsys.readouterr().out)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        if fault == "provider_failure":
            stopped = run(succeeds=False)
            assert not stopped["finalized"]
            before = json.loads(progress_path.read_text())
            foundation = before["units"][0]
            assert foundation["status"] == "done"
            assert before["units"][1]["status"] == "failed"
            calls_before = len(writes)
            blocked = run(succeeds=False)
            assert not blocked["finalized"] and len(writes) == calls_before
            completed = run("--reset-failed")
        else:
            completed = run()
    assert completed["finalized"], completed
    assert completed["verification"]["status"] == "passed"
    assert len(writes) == {"test_failure": 4, "provider_failure": 3, "review_protocol": 2}[fault]
    assert len(gates) == (2 if fault == "review_protocol" else 1)
    if fault == "review_protocol":
        assert "previous response was rejected" in gates[1]
    progress = json.loads(progress_path.read_text())
    assert len(progress["units"]) == 2
    assert all(unit["status"] == "done" for unit in progress["units"])
    expected_files = {
        "src/calculator.py": b"def add(a, b):\n    return a + b\n",
        "src/foundation.py": b"VALUE = 1\n",
        "src/consumer.py": b"from foundation import VALUE\n",
    }
    for path, expected in expected_files.items():
        content = subprocess.check_output(["git", "show", f"{progress['final_commit']}:{path}"], cwd=root)
        assert content.replace(b"\r\n", b"\n") == expected
    if fault == "provider_failure":
        assert progress["units"][0] == foundation
    child = JsonStateStore(root / ".sikula/state").load(progress["units"][1]["child_task_id"])
    assert child.done and not child.failed and child.review_approved and child.security_approved
    if fault == "test_failure":
        assert [item["triage_pass"] for item in child.fix_cycle_records] == ["test_only", "production_confirmed"]
        assert [item["status"] for item in child.validation_cycle_records if item["phase"] == "test"] == [
            "failed",
            "success",
        ]
    with patch(
        "core.llm_client.create_llm_client", side_effect=AssertionError("Finalized replay must not call providers")
    ):
        replay = run()
    assert replay["finalized"] and replay["units_attempted"] == 0
    assert json.loads(progress_path.read_text()) == progress
    assert (root / ".git/index").read_bytes() == index_before
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root) == head_before
