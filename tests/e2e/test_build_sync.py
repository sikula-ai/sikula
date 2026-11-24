"""A real local sync command supplies generated output before fake-provider review."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from unittest.mock import patch

import pytest

from sikula import cmd_run
from tests.e2e.conftest import e2e_cfg, run_args


@pytest.mark.parametrize("isolated", [False, True])
def test_dependency_sync_is_reviewed_and_delivered(git_project, seq_fake_llm, monkeypatch, isolated):
    root = git_project
    monkeypatch.chdir(root)
    (root / "package.json").write_text('{"dependencies":{}}\n', encoding="utf-8")
    (root / "package-lock.json").write_text('{"dependencies":{}}\n', encoding="utf-8")
    (root / "sync.py").write_text(
        'from pathlib import Path\nPath("package-lock.json").write_bytes(Path("package.json").read_bytes())\n',
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(".sikula/state/\n.sikula/worktrees/\n__pycache__/\n", encoding="utf-8")
    task = root / ".sikula/tasks/dependency.md"
    task.parent.mkdir(parents=True)
    task.write_text("Add the requested dependency in the manifest. The pipeline regenerates its lockfile.\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "sync fixture"], cwd=root, check=True, capture_output=True)
    cfg = e2e_cfg(root)
    cfg["project"]["build_tool"] = "node"
    cfg["sandbox"]["allowed_write_paths"] = ["src/", "package.json", "package-lock.json"]
    cfg["run_build"] = True
    join = subprocess.list2cmdline if os.name == "nt" else shlex.join
    cfg["build"]["sync_command"] = join([sys.executable, "sync.py"])
    cfg["build"]["compile_command"] = join([sys.executable, "-m", "compileall", "-q", "src/"])
    manifest = '{"dependencies":{"synthetic-example":"1.0.0"}}\n'
    fake = seq_fake_llm(agent_responses=[{"package.json": manifest}])
    readonly = fake.run_readonly_agent
    reviewed = []

    def check_review(prompt, cwd):
        if (cwd / "package.json").read_text(encoding="utf-8") == manifest:
            assert (cwd / "package-lock.json").read_bytes() == (cwd / "package.json").read_bytes()
            assert "package-lock.json" in prompt
            reviewed.append(prompt)
        return readonly(prompt, cwd)

    fake.run_readonly_agent = check_review
    with patch("core.llm_client.create_llm_client", return_value=fake):
        with pytest.raises(SystemExit) as exit_info:
            cmd_run(run_args(task_file=str(task), no_isolate=not isolated), cfg)
    assert exit_info.value.code == 0
    states = [json.loads(p.read_text()) for p in (root / ".sikula/state").glob("*.json")]
    state = next(s for s in states if "task_id" in s)
    assert state["done"] and not state["failed"]
    assert reviewed and "package-lock.json" in state["files_changed"]
    assert state["build_sync_pending"] is None
    assert [r["phase"] for r in state["validation_cycle_records"]].index("sync") < [
        r["phase"] for r in state["validation_cycle_records"]
    ].index("build")
    if isolated:
        result = subprocess.run(
            ["git", "show", f"{state['result_commit']}:package-lock.json"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout == manifest
