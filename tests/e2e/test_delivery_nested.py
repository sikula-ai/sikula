"""Nested delivery through the actual coordinator with isolated fake providers."""

import json
from pathlib import Path
from unittest.mock import patch

import yaml

from sikula import main
from tests.e2e.test_delivery_plan import _write_handoff_smoke_config, _git_commit_all
from tests.test_delivery_nested_checkpoints import _prepared


def test_run_schedules_nested_gates_and_resumes_at_unit_bound(git_project, seq_fake_llm, monkeypatch, capsys):
    path, _, _, _, _, _ = _prepared(git_project)
    _write_handoff_smoke_config(git_project)
    config_path = git_project / ".sikula/config.yaml"
    cfg = yaml.safe_load(config_path.read_text())
    cfg["sandbox"]["allowed_write_paths"] = ["agents/"]
    cfg.update(run_build=True, run_tests=True)
    cfg["build"].update(
        compile_command="python3 -m compileall -q agents/", test_command="python3 -m pytest tests_proj/"
    )
    config_path.write_text(yaml.safe_dump(cfg))
    # Contracts' declared test command must match the configured invocation.
    # Changing them invalidates compact receipts intentionally: this e2e exercises
    # hierarchical scheduling and conservative fallback through real unit execution.
    for contract in path.parent.joinpath("units").glob("*.md"):
        contract.write_text(
            contract.read_text()
            .replace("`pytest`", "`python3 -m pytest tests_proj/`")
            .replace("`ruff check .`", "`python3 -m compileall -q agents/`")
        )
    _git_commit_all(git_project, "prepared nested delivery")
    fake = seq_fake_llm(
        agent_responses=[{f"agents/{key}.py": "value = 1\n"} for key in ("foundation", "consumer", "shipping")]
    )
    original = fake.run_readonly_agent
    gates = []

    def readonly(prompt: str, cwd: Path) -> str:
        if (
            "Review whether the complete assembled candidate" in prompt
            or "Review cross-unit security and privacy behavior" in prompt
        ):
            # The real prompt's declared protocol supplies the exact required scope.
            template = next(line for line in reversed(prompt.splitlines()) if line.startswith('{"schema_version":2'))
            control = json.loads(template)
            context = json.loads(
                prompt.split("Delivery plan context:\n", 1)[1].split("\n\nDeterministic validation summary:", 1)[0]
            )
            node = context.get("verification_node", {}).get("id", "root")
            expected = {"storage": "foundation", "integration": "consumer", "root": "shipping"}
            assert (cwd / "agents" / (expected[node] + ".py")).exists()
            gates.append(node)
            return json.dumps(control)
        return original(prompt, cwd)

    fake.run_readonly_agent = readonly
    monkeypatch.chdir(git_project)

    def invoke(*args):
        with patch("sys.argv", ["sikula", "delivery", "run", str(path), *args, "--json"]):
            main()
        return json.loads(capsys.readouterr().out)

    with patch("core.llm_client.create_llm_client", return_value=fake):
        paused = invoke("--max-units", "1")
        assert paused["succeeded"], paused
        assert "storage" in gates and "integration" not in gates
        done = invoke()
        assert done["finalized"], done
        assert gates.index("storage") < gates.index("integration") < gates.index("root")
        before = list(gates)
        again = invoke()
        assert again["finalized"] and again["units_attempted"] == 0
        assert gates == before
