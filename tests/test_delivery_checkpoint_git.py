from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from core.delivery_checkpoints import checkpoint_review_rule_fingerprints
from tests.test_delivery_repair import _git


@pytest.fixture
def candidate(tmp_path: Path) -> tuple[Path, str, dict[str, bytes]]:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "core.autocrlf", "false")
    root = tmp_path / "app"
    root.mkdir()
    contents = {"one.md": b"LF\n", "copy.md": b"LF\n", "space name.md": b"CRLF\r\n", "[literal].md": b""}
    for name, content in contents.items():
        (root / name).write_bytes(content)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "authority files")
    return root, _git(tmp_path, "rev-parse", "HEAD"), contents


def test_authority_reads_batch_and_deduplicate_blobs_without_caching(candidate) -> None:
    root, commit, contents = candidate
    expected = {name: "sha256:" + sha256(content).hexdigest() for name, content in contents.items()}
    with patch("core.delivery_checkpoints.subprocess.run", wraps=subprocess.run) as run:
        assert checkpoint_review_rule_fingerprints(root, commit, contents) == expected
    assert run.call_count == 3  # Prefix, metadata, and a bounded batch of distinct blobs.
    batch = next(call for call in run.call_args_list if "--batch" in call.args[0])
    assert len(batch.kwargs["input"].splitlines()) == 3
    (root / "one.md").write_bytes(b"Changed checkout\n")
    assert checkpoint_review_rule_fingerprints(root, commit, contents) == expected
    _git(root, "add", ".")
    _git(root, "commit", "-m", "changed candidate")
    changed = _git(root, "rev-parse", "HEAD")
    assert checkpoint_review_rule_fingerprints(root, changed, contents)["one.md"] != expected["one.md"]


def test_authority_blob_batches_respect_payload_bound(candidate, monkeypatch) -> None:
    root, _, _ = candidate
    monkeypatch.setattr("core.delivery_verification.MAX_DELIVERY_VERIFICATION_PACKET_BYTES", 64)
    contents = {f"rule-{index}.md": bytes([index]) * 48 for index in range(5)}
    for name, content in contents.items():
        (root / name).write_bytes(content)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "bounded batches")
    commit = _git(root, "rev-parse", "HEAD")
    with patch("core.delivery_checkpoints.subprocess.run", wraps=subprocess.run) as run:
        result = checkpoint_review_rule_fingerprints(root, commit, contents)
    assert result == {name: "sha256:" + sha256(content).hexdigest() for name, content in contents.items()}
    batches = [call for call in run.call_args_list if "--batch" in call.args[0]]
    assert len(batches) == 5
    assert all(len(call.kwargs["input"].splitlines()) == 1 for call in batches)


def test_authority_rejects_oversized_blob_before_reading_it(candidate, monkeypatch) -> None:
    root, commit, _ = candidate
    monkeypatch.setattr("core.delivery_verification.MAX_DELIVERY_VERIFICATION_PACKET_BYTES", 3)
    with patch("core.delivery_checkpoints.subprocess.run", wraps=subprocess.run) as run:
        with pytest.raises(ValueError, match="exceeds"):
            checkpoint_review_rule_fingerprints(root, commit, ["space name.md"])
    assert not any("cat-file" in call.args[0] for call in run.call_args_list)


@pytest.mark.parametrize("corruption", ["header", "truncated", "extra", "failed"])
def test_authority_rejects_invalid_batch_response(candidate, corruption: str) -> None:
    root, commit, contents = candidate
    original_run = subprocess.run

    def run(args, **kwargs):
        result = original_run(args, **kwargs)
        if "--batch" in args:
            if corruption == "header":
                result.stdout = b"invalid\n" + result.stdout
            elif corruption == "truncated":
                result.stdout = result.stdout[:-2]
            elif corruption == "extra":
                result.stdout += b"unexpected"
            else:
                result.returncode = 1
        return result

    with patch("core.delivery_checkpoints.subprocess.run", side_effect=run):
        with pytest.raises(ValueError):
            checkpoint_review_rule_fingerprints(root, commit, contents)
