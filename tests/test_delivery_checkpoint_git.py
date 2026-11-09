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


def test_shared_authority_reads_are_operation_local_and_copy_results(candidate):
    from core.delivery_checkpoints import _checkpoint_file_fingerprints
    from core.delivery_evidence_resolution import EvidenceResolver

    root, commit, contents = candidate
    expected = checkpoint_review_rule_fingerprints(root, commit, contents)
    with patch("core.delivery_checkpoints.subprocess.run", wraps=subprocess.run) as run:
        with EvidenceResolver() as resolver:
            first = _checkpoint_file_fingerprints(root, commit, contents, resolver=resolver)
            first.clear()
            (root / "one.md").write_bytes(b"checkout does not replace a committed blob")
            assert _checkpoint_file_fingerprints(root, commit, contents, resolver=resolver) == expected
            assert resolver.bytes_read > 0
        assert run.call_count == 4  # Initial context/metadata/blobs, then fresh repository context.
        with EvidenceResolver() as resolver:
            assert _checkpoint_file_fingerprints(root, commit, contents, resolver=resolver) == expected
        assert run.call_count == 7
    assert not resolver._git_keys and resolver.cache_bytes == 0


def test_shared_authority_separates_commits_projects_and_policies(candidate):
    from core.delivery_checkpoints import _checkpoint_file_fingerprints as read
    from core.delivery_evidence_resolution import EvidenceResolver

    root, commit, _ = candidate
    with EvidenceResolver() as resolver:
        raw = read(root, commit, ["space name.md"], resolver=resolver)
        normalized = read(root, commit, ["space name.md"], normalize_newlines=True, resolver=resolver)
        contract = read(root, commit, ["space name.md"], normalize_contract=True, resolver=resolver)
        assert len({raw["space name.md"], normalized["space name.md"], contract["space name.md"]}) == 3
        assert (
            read(root.parent, commit, ["app/space name.md"], resolver=resolver)["app/space name.md"]
            == raw["space name.md"]
        )
        with pytest.raises(ValueError):
            read(root.parent, commit, ["space name.md"], resolver=resolver)
        assert read(root, commit, ["missing"], allow_missing=True, resolver=resolver) == {"missing": "absent"}
        with pytest.raises(ValueError):
            read(root, commit, ["missing"], resolver=resolver)
        before = read(root, "HEAD", ["one.md"], resolver=resolver)
        (root / "one.md").write_bytes(b"new candidate")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "new candidate")
        changed = _git(root, "rev-parse", "HEAD")
        assert read(root, changed, ["one.md"], resolver=resolver) != before
        assert read(root, "HEAD", ["one.md"], resolver=resolver) != before
        assert read(root, commit, ["one.md"], resolver=resolver) == before


def test_shared_authority_does_not_hide_repository_loss(candidate, tmp_path):
    from core.delivery_checkpoints import _checkpoint_file_fingerprints as read
    from core.delivery_evidence_resolution import EvidenceResolver

    root, commit, _ = candidate
    with EvidenceResolver() as resolver:
        read(root, commit, ["one.md"], resolver=resolver)
        # Same project path now belongs to a different repository with no such object.
        _git(root, "init")
        with pytest.raises(ValueError):
            read(root, commit, ["one.md"], resolver=resolver)
    with EvidenceResolver() as resolver, pytest.raises(ValueError):
        read(root, commit, ["one.md"], resolver=resolver)


@pytest.mark.parametrize("field", ["artifacts", "reads", "bytes_read", "materialized_bytes"])
def test_shared_authority_limits_are_sticky(candidate, field):
    from dataclasses import replace
    from core.delivery_checkpoints import _checkpoint_file_fingerprints as read
    from core.delivery_evidence_resolution import EvidenceResolver, EvidenceResolutionLimit, EvidenceResolutionLimits

    root, commit, _ = candidate
    resolver = EvidenceResolver(replace(EvidenceResolutionLimits(), **{field: 0}))
    with pytest.raises(EvidenceResolutionLimit):
        read(root, commit, ["one.md"], resolver=resolver)
    with pytest.raises(EvidenceResolutionLimit):
        read(root, commit, ["one.md"], resolver=resolver)


def test_git_and_sidecar_reads_share_lru_and_artifact_allowance(candidate):
    from dataclasses import replace
    from core.delivery_checkpoints import _checkpoint_file_fingerprints as read
    from core.delivery_evidence_resolution import EvidenceResolver, EvidenceResolutionLimits, EvidenceResolutionLimit
    from core.delivery_repair_storage import write_repair_state

    root, commit, _ = candidate
    state = root / "state.json"
    write_repair_state(root, state, {"value": "x" * 4000})
    limits = replace(EvidenceResolutionLimits(), cache_bytes=5000)
    with patch("core.delivery_checkpoints.subprocess.run", wraps=subprocess.run) as run:
        with EvidenceResolver(limits) as resolver:
            expected = read(root, commit, ["one.md"], resolver=resolver)
            resolver.read(root, state)
            assert read(root, commit, ["one.md"], resolver=resolver) == expected
            assert resolver.cache_bytes <= limits.cache_bytes
        assert sum("--batch" in c.args[0] for c in run.call_args_list) == 2
    with pytest.raises(EvidenceResolutionLimit):
        with EvidenceResolver(replace(limits, artifacts=2)) as resolver:
            read(root, commit, ["one.md"], resolver=resolver)
            resolver.read(root, state)
    with pytest.raises(EvidenceResolutionLimit):
        with EvidenceResolver(replace(limits, artifacts=1)) as resolver:
            resolver.read(root, state)
            read(root, commit, ["one.md"], resolver=resolver)


def test_shared_authority_keeps_link_policy_and_packet_limit(candidate, monkeypatch):
    from core.delivery_checkpoints import _checkpoint_file_fingerprints as read
    from core.delivery_evidence_resolution import EvidenceResolver

    root, _, _ = candidate
    try:
        (root / "alias").symlink_to("one.md")
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "linked rule")
    commit = _git(root, "rev-parse", "HEAD")
    with EvidenceResolver() as resolver:
        assert read(root, commit, ["alias"], resolver=resolver)["alias"].startswith("sha256:")
        with pytest.raises(ValueError, match="regular"):
            read(root, commit, ["alias"], follow_links=False, resolver=resolver)
        read(root, commit, ["one.md"], resolver=resolver)
        monkeypatch.setattr("core.delivery_verification.MAX_DELIVERY_VERIFICATION_PACKET_BYTES", 1)
        with pytest.raises(ValueError, match="exceeds"):
            read(root, commit, ["one.md"], resolver=resolver)


def test_ambiguous_repository_context_uses_uncached_reads(candidate):
    from core.delivery_checkpoints import _checkpoint_file_fingerprints as read
    from core.delivery_evidence_resolution import EvidenceResolver

    root, commit, _ = candidate
    original = subprocess.run

    def run(args, **kwargs):
        result = original(args, **kwargs)
        if "--absolute-git-dir" in args:
            result.stdout = b"app/\n/project/with\nnewline/.git\n"
        return result

    with patch("core.delivery_checkpoints.subprocess.run", side_effect=run) as git:
        with EvidenceResolver() as resolver:
            assert read(root, commit, ["one.md"], resolver=resolver) == read(
                root, commit, ["one.md"], resolver=resolver
            )
            assert not resolver._git_keys
        assert sum("--batch" in call.args[0] for call in git.call_args_list) == 2


def test_identical_blob_batches_reuse_hashes_across_commits_and_path_order(candidate):
    from core.delivery_checkpoints import _checkpoint_file_fingerprints as read
    from core.delivery_evidence_resolution import EvidenceResolver

    root, commit, contents = candidate
    _git(root, "commit", "--allow-empty", "-m", "another candidate")
    later = _git(root, "rev-parse", "HEAD")
    expected = checkpoint_review_rule_fingerprints(root, commit, contents)
    with patch("core.delivery_checkpoints.subprocess.run", wraps=subprocess.run) as git:
        with EvidenceResolver() as resolver:
            assert read(root, commit, contents, resolver=resolver) == expected
            assert read(root, later, list(reversed(contents)), resolver=resolver) == expected
        assert sum("ls-tree" in c.args[0] for c in git.call_args_list) == 2
        assert sum("--absolute-git-dir" in c.args[0] for c in git.call_args_list) == 2
        assert sum("--batch" in c.args[0] for c in git.call_args_list) == 1
        with EvidenceResolver() as resolver:
            assert read(root, later, contents, resolver=resolver) == expected
        assert sum("--batch" in c.args[0] for c in git.call_args_list) == 2


@pytest.mark.parametrize("change", ["content", "missing", "directory", "link"])
def test_shared_blob_hashes_do_not_replace_new_candidate_path_checks(candidate, change):
    from core.delivery_checkpoints import _checkpoint_file_fingerprints as read
    from core.delivery_evidence_resolution import EvidenceResolver

    root, commit, _ = candidate
    with EvidenceResolver() as resolver:
        before = read(root, commit, ["one.md"], follow_links=False, resolver=resolver)
        path = root / "one.md"
        path.unlink()
        if change == "content":
            path.write_bytes(b"new authority")
        elif change == "directory":
            path.mkdir()
            (path / "nested.md").write_bytes(b"LF\n")
        elif change == "link":
            try:
                path.symlink_to("copy.md")
            except (OSError, NotImplementedError):
                pytest.skip("Symlinks unavailable")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "changed authority path")
        later = _git(root, "rev-parse", "HEAD")
        if change == "content":
            assert read(root, later, ["one.md"], follow_links=False, resolver=resolver) != before
        else:
            with pytest.raises(ValueError):
                read(root, later, ["one.md"], follow_links=False, resolver=resolver)
        if change == "missing":
            assert read(root, later, ["one.md"], allow_missing=True, resolver=resolver) == {"one.md": "absent"}


def test_shared_blob_hashes_reject_missing_object_in_new_candidate(candidate):
    from core.delivery_checkpoints import _checkpoint_file_fingerprints as read
    from core.delivery_evidence_resolution import EvidenceResolver

    root, commit, _ = candidate
    _git(root, "commit", "--allow-empty", "-m", "later candidate")
    later = _git(root, "rev-parse", "HEAD")
    oid = _git(root, "rev-parse", f"{commit}:app/one.md")
    with EvidenceResolver() as resolver:
        read(root, commit, ["one.md"], resolver=resolver)
        blob_path = root.parent / ".git/objects" / oid[:2] / oid[2:]
        blob_path.chmod(0o600)  # Git objects may be read-only on Windows.
        blob_path.unlink()
        with pytest.raises(ValueError):
            read(root, later, ["one.md"], resolver=resolver)


def test_shared_blob_batches_keep_project_policy_and_mutable_ref_boundaries(candidate, monkeypatch):
    from core.delivery_checkpoints import _checkpoint_file_fingerprints as read
    from core.delivery_evidence_resolution import EvidenceResolver

    root, commit, _ = candidate
    _git(root, "commit", "--allow-empty", "-m", "later candidate")
    later = _git(root, "rev-parse", "HEAD")
    with patch("core.delivery_checkpoints.subprocess.run", wraps=subprocess.run) as git:
        with EvidenceResolver() as resolver:
            raw = read(root, commit, ["space name.md"], resolver=resolver)
            normalized = read(root, later, ["space name.md"], normalize_newlines=True, resolver=resolver)
            assert normalized != raw
            assert read(root.parent, later, ["app/space name.md"], resolver=resolver) == {
                "app/space name.md": raw["space name.md"]
            }
            assert read(root, "HEAD", ["space name.md"], resolver=resolver) == raw
            assert read(root, "HEAD", ["space name.md"], resolver=resolver) == raw
            assert sum("--batch" in c.args[0] for c in git.call_args_list) == 5
            monkeypatch.setattr("core.delivery_verification.MAX_DELIVERY_VERIFICATION_PACKET_BYTES", 1)
            with pytest.raises(ValueError, match="exceeds"):
                read(root, later, ["space name.md"], resolver=resolver)


def test_shared_blob_batches_remain_bounded_across_commits(candidate, monkeypatch):
    from core.delivery_checkpoints import _checkpoint_file_fingerprints as read
    from core.delivery_evidence_resolution import EvidenceResolver

    root, _, _ = candidate
    monkeypatch.setattr("core.delivery_verification.MAX_DELIVERY_VERIFICATION_PACKET_BYTES", 64)
    contents = {f"rule-{index}.md": bytes([index]) * 48 for index in range(5)}
    for name, content in contents.items():
        (root / name).write_bytes(content)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "bounded authority")
    commit = _git(root, "rev-parse", "HEAD")
    _git(root, "commit", "--allow-empty", "-m", "later candidate")
    later = _git(root, "rev-parse", "HEAD")
    with patch("core.delivery_checkpoints.subprocess.run", wraps=subprocess.run) as git:
        with EvidenceResolver() as resolver:
            expected = read(root, commit, contents, resolver=resolver)
            assert read(root, later, contents, resolver=resolver) == expected
        batches = [c for c in git.call_args_list if "--batch" in c.args[0]]
        assert len(batches) == 5
        assert all(len(c.kwargs["input"].splitlines()) == 1 for c in batches)


@pytest.mark.parametrize("corruption", ["header", "truncated", "extra", "failed"])
def test_invalid_blob_batches_are_not_cached(candidate, corruption):
    from core.delivery_checkpoints import _checkpoint_file_fingerprints as read
    from core.delivery_evidence_resolution import EvidenceResolver

    root, commit, contents = candidate
    original = subprocess.run
    corrupt = True

    def run(args, **kwargs):
        result = original(args, **kwargs)
        if corrupt and "--batch" in args:
            if corruption == "header":
                result.stdout = b"invalid\n" + result.stdout
            elif corruption == "truncated":
                result.stdout = result.stdout[:-2]
            elif corruption == "extra":
                result.stdout += b"extra"
            else:
                result.returncode = 1
        return result

    with patch("core.delivery_checkpoints.subprocess.run", side_effect=run) as git:
        with EvidenceResolver() as resolver:
            with pytest.raises(ValueError):
                read(root, commit, contents, resolver=resolver)
            corrupt = False
            assert read(root, commit, contents, resolver=resolver) == {
                name: "sha256:" + sha256(content).hexdigest() for name, content in contents.items()
            }
        assert sum("--batch" in c.args[0] for c in git.call_args_list) == 2


@pytest.mark.parametrize("cache_bytes", [0, 5000])
def test_shared_blob_eviction_charges_new_payload_reads(candidate, cache_bytes):
    from dataclasses import replace
    from core.delivery_checkpoints import _checkpoint_file_fingerprints as read
    from core.delivery_evidence_resolution import EvidenceResolver, EvidenceResolutionLimits
    from core.delivery_repair_storage import write_repair_state

    root, commit, contents = candidate
    _git(root, "commit", "--allow-empty", "-m", "later candidate")
    later = _git(root, "rev-parse", "HEAD")
    state = root / "state.json"
    write_repair_state(root, state, {"value": "x" * 4000})
    with patch("core.delivery_checkpoints.subprocess.run", wraps=subprocess.run) as git:
        with EvidenceResolver(replace(EvidenceResolutionLimits(), cache_bytes=cache_bytes)) as resolver:
            expected = read(root, commit, contents, resolver=resolver)
            resolver.read(root, state)
            bytes_before, memory_before = resolver.bytes_read, resolver.materialized_bytes
            assert read(root, later, contents, resolver=resolver) == expected
            assert resolver.bytes_read > bytes_before
            assert resolver.materialized_bytes > memory_before
            assert resolver.cache_bytes <= cache_bytes
        assert sum("--batch" in c.args[0] for c in git.call_args_list) == 2
