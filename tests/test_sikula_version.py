"""Public runtime metadata contract, independent of project and provider setup."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import Mock

import pytest

import sikula
from core import version as runtime_version


# Standalone acceptance contract supplied by the consumer, with no Protocol dependency.
_CONSTANTS = {
    "schema_version": "sikula.cli.v1",
    "command": "sikula version",
    "status": "done",
    "next_action": "none",
}
_VERSION_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?![\s\S])"
)


def _assert_contract(payload):
    assert isinstance(payload, dict)
    assert set(payload) == {*_CONSTANTS, "sikula_version"}
    assert all(payload[key] == value for key, value in _CONSTANTS.items())
    value = payload["sikula_version"]
    assert isinstance(value, str) and 5 <= len(value) <= 64
    assert re.search(_VERSION_PATTERN, value) is not None


@pytest.fixture
def version_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["sikula", "version", "--json"])

    def forbidden(*args, **kwargs):
        pytest.fail("Version reporting must not load project configuration or initialize runtime services")

    monkeypatch.setattr(sikula, "_load_runtime_config", forbidden)
    monkeypatch.setattr(sikula, "cmd_init", forbidden)
    monkeypatch.setattr(sikula, "cmd_run", forbidden)
    monkeypatch.setattr("core.llm_client.create_llm_client", forbidden)
    monkeypatch.setattr("core.state.JsonStateStore", forbidden)
    return tmp_path


@pytest.mark.parametrize(
    "version",
    ["0.0.0", "0.3.0", "0.3.0-dev+main.82867fd", "1.0.0-alpha.1+build.001", "1.0.0+" + "a" * 58],
)
def test_version_json_contract(version_cli, monkeypatch, capsys, version):
    getter = Mock(return_value=version)
    monkeypatch.setattr(sikula, "_sikula_version", getter)

    sikula.main()

    output = capsys.readouterr()
    assert output.err == ""
    assert len(output.out.splitlines()) == 1
    payload = json.loads(output.out)  # Rejects extra JSON objects or trailing human text.
    _assert_contract(payload)
    assert payload["sikula_version"] == version
    getter.assert_called_once_with()
    assert list(version_cli.iterdir()) == []


@pytest.mark.parametrize(
    "version",
    [
        None,
        123,
        {},
        "",
        "dev",
        "unknown",
        "1.0",
        "1.0.0-.",
        "1.0.0-a..b",
        "1.0.0-01",
        "01.0.0",
        "1.0.0\n",
        "1.0.0\r\n",
        "1.0.0+" + "a" * 59,
        "1.0.0+privátni",
        "/private/project/token=SECRET",
    ],
)
def test_version_json_rejects_invalid_metadata(version_cli, monkeypatch, capsys, version):
    monkeypatch.setattr(sikula, "_sikula_version", lambda: version)
    with pytest.raises(SystemExit) as error:
        sikula.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Cannot report Sikula version: runtime version is unavailable or invalid.\n"
    assert list(version_cli.iterdir()) == []


@pytest.mark.parametrize("failure", [runtime_version.PackageNotFoundError, OSError, RuntimeError])
def test_version_json_handles_unavailable_metadata_without_fallback(version_cli, monkeypatch, capsys, failure):
    monkeypatch.setattr(runtime_version, "_pkg_version", Mock(side_effect=failure("/private/path SECRET")))
    with pytest.raises(SystemExit) as error:
        sikula.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Cannot report Sikula version: runtime version is unavailable or invalid.\n"


@pytest.mark.parametrize("config", ["absent", "malformed", "explicit"])
def test_version_json_does_not_read_or_modify_project(version_cli, monkeypatch, capsys, config):
    if config != "absent":
        project = version_cli / ".sikula"
        project.mkdir()
        (project / "config.yaml").write_text("project: [invalid yaml\n", encoding="utf-8")
        (project / "state").mkdir()
        (project / "state/task.json").write_text('{"private": "untouched"}', encoding="utf-8")
    if config == "explicit":
        monkeypatch.setattr(sys, "argv", ["sikula", "--config", "missing-config.yaml", "version", "--json"])
    before = {
        p.relative_to(version_cli): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in version_cli.rglob("*")
        if p.is_file()
    }
    monkeypatch.setattr(sikula, "_sikula_version", lambda: "0.3.0-dev+main.82867fd")

    sikula.main()

    output = capsys.readouterr()
    _assert_contract(json.loads(output.out))
    assert output.err == ""
    after = {
        p.relative_to(version_cli): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in version_cli.rglob("*")
        if p.is_file()
    }
    assert after == before


@pytest.mark.parametrize("field", [*_CONSTANTS, "sikula_version"])
def test_acceptance_contract_rejects_missing_fields(field):
    payload = {**_CONSTANTS, "sikula_version": "0.3.0"}
    payload.pop(field)
    with pytest.raises(AssertionError):
        _assert_contract(payload)


@pytest.mark.parametrize("extra", [{"path": "/private/checkout"}, {"credential": "SECRET"}, {"capabilities": []}])
def test_acceptance_contract_rejects_additional_fields(extra):
    with pytest.raises(AssertionError):
        _assert_contract({**_CONSTANTS, "sikula_version": "0.3.0", **extra})


@pytest.mark.parametrize("version", ["0.3.0", "0.3.0-dev+main.82867fd", "dev"])
def test_existing_version_flag_keeps_text_and_immediate_exit(version_cli, monkeypatch, capsys, version):
    monkeypatch.setattr(sys, "argv", ["sikula", "--version", "--unknown-option"])
    monkeypatch.setattr(sikula, "_sikula_version", lambda: version)
    # Compare the previous argparse action, including Python-specific program naming.
    previous = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter)
    previous.add_argument("--version", action="version", version=f"%(prog)s {version}")
    with pytest.raises(SystemExit) as baseline:
        previous.parse_args()
    expected = capsys.readouterr()
    with pytest.raises(SystemExit) as error:
        sikula.main()
    assert error.value.code == baseline.value.code == 0
    output = capsys.readouterr()
    assert output.out == expected.out
    assert output.err == ""


def test_version_json_uses_shared_runtime_metadata(version_cli, monkeypatch, capsys):
    metadata = Mock(return_value="0.3.0")
    monkeypatch.setattr(runtime_version, "_pkg_version", metadata)
    monkeypatch.setattr(runtime_version, "dev_version_suffix", lambda: "-dev+main.82867fd")
    sikula.main()
    payload = json.loads(capsys.readouterr().out)
    _assert_contract(payload)
    assert payload["sikula_version"] == "0.3.0-dev+main.82867fd"
    metadata.assert_called_once_with("sikula")


def test_version_without_json_prints_runtime_version(version_cli, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sikula", "version"])
    monkeypatch.setattr(sikula, "_sikula_version", lambda: "0.3.0")
    sikula.main()
    assert capsys.readouterr().out == "sikula 0.3.0\n"


@pytest.mark.parametrize("malformed_config", [False, True])
@pytest.mark.parametrize("version", ["1.0.0-alpha.1+build.001", None])
def test_version_subprocess_from_unrelated_directory(tmp_path, malformed_config, version):
    if malformed_config:
        (tmp_path / ".sikula").mkdir()
        (tmp_path / ".sikula/config.yaml").write_text("project: [invalid\n", encoding="utf-8")
    source = Path(__file__).resolve().parents[1]
    # Controlled metadata, but real CLI parsing, imports and process exit behavior.
    script = (
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        f"import sikula; sikula._sikula_version = lambda: {version!r}; sikula.main()"
    )
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    env = {key: value for key, value in os.environ.items() if not any(part in key for part in ("TOKEN", "API_KEY"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script, str(source), "version", "--json"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if version is None:
        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr == "Cannot report Sikula version: runtime version is unavailable or invalid.\n"
    else:
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        _assert_contract(json.loads(result.stdout))
        assert json.loads(result.stdout)["sikula_version"] == version
    assert {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
