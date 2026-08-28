"""Tests for validation artifact snapshot/restore helpers."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.validation_artifacts as validation_artifacts_module
from core.validation_artifacts import (
    DeliveryScopeSnapshotError,
    FileSnapshot,
    ValidationArtifact,
    delivery_scope_git_baseline,
    deserialize_delivery_scope_snapshot,
    detect_validation_artifacts,
    restore_validation_artifacts,
    serialize_delivery_scope_snapshot,
    snapshot_delivery_scope_files,
    snapshot_validation_dirty_files,
)


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "src").mkdir()
    (path / "src" / "main.py").write_text("# placeholder\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def _delivery_scope_git_repo(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    if not request.node.name.startswith("test_delivery_scope"):
        return
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)


def _artifact(path: str, before: str = "tracked", after: str = "tracked") -> ValidationArtifact:
    return ValidationArtifact(path=path, before_status=before, after_status=after)


def _make_symlink(path: Path, target: str = "missing-target") -> None:
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")


def test_snapshot_returns_empty_when_git_command_fails(tmp_path: Path, monkeypatch):
    from core import validation_artifacts

    def fail_git(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="fatal: not a repository")

    monkeypatch.setattr(validation_artifacts.subprocess, "run", fail_git)

    assert snapshot_validation_dirty_files(tmp_path) == {}


def test_delivery_scope_snapshot_includes_ignored_files_and_does_not_follow_symlinks(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / ".env").write_text("PRIVATE=value\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text("assert True\n", encoding="utf-8")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "private.json").write_text("private\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-scope"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    _make_symlink(tmp_path / "external", str(outside))

    snapshot = snapshot_delivery_scope_files(
        tmp_path,
        ignored_roots=["state"],
        include_content=lambda path: path.startswith("tests/"),
    )

    assert ".env" in snapshot
    assert snapshot[".env"].digest is not None
    assert snapshot[".env"].content is None
    assert snapshot["tests/test_sample.py"].content == b"assert True\n"
    assert snapshot["external"].symlink_target == str(outside)
    assert "external/secret.txt" not in snapshot
    assert "state/private.json" not in snapshot


def test_delivery_scope_snapshot_includes_exactly_ignored_file_in_untracked_directory(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("src/allowed/ignored.env\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "ignore exact file"], cwd=tmp_path, check=True, capture_output=True)
    allowed = tmp_path / "src" / "allowed"
    allowed.mkdir(parents=True)
    (allowed / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    (allowed / "ignored.env").write_text("PRIVATE=value\n", encoding="utf-8")

    snapshot = snapshot_delivery_scope_files(tmp_path)

    assert "src/allowed/tracked.py" in snapshot
    assert "src/allowed/ignored.env" in snapshot


def test_delivery_scope_snapshot_rejects_symlink_when_policy_callback_denies_it(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-policy"
    outside.mkdir()
    _make_symlink(tmp_path / "external", str(outside))

    with pytest.raises(DeliveryScopeSnapshotError, match="escapes the active write scope"):
        snapshot_delivery_scope_files(
            tmp_path,
            validate_symlink=lambda path, target: path != "external" or target == "allowed",
        )


def test_delivery_scope_snapshot_rejects_symlink_target_change_during_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    link = tmp_path / "changing"
    _make_symlink(link, "first")
    original_readlink = os.readlink
    link_reads = 0

    def changing_readlink(path, *args, **kwargs):
        nonlocal link_reads
        if Path(path).name == link.name:
            link_reads += 1
            return "first" if link_reads == 1 else "second"
        return original_readlink(path, *args, **kwargs)

    monkeypatch.setattr(validation_artifacts_module.os, "readlink", changing_readlink)

    with pytest.raises(DeliveryScopeSnapshotError, match="project path changed"):
        snapshot_delivery_scope_files(tmp_path)

    assert link_reads == 2


def test_delivery_scope_snapshot_round_trip_preserves_change_detection(tmp_path: Path) -> None:
    source = tmp_path / "ignored.env"
    source.write_text("before\n", encoding="utf-8")
    before = snapshot_delivery_scope_files(tmp_path)
    persisted = serialize_delivery_scope_snapshot(before)

    restored = deserialize_delivery_scope_snapshot(persisted)
    source.write_text("after\n", encoding="utf-8")
    after = snapshot_delivery_scope_files(tmp_path)

    assert restored == before
    assert [artifact.path for artifact in detect_validation_artifacts(restored, after)] == ["ignored.env"]


def test_delivery_scope_snapshot_keeps_committed_changes_visible_against_baseline(tmp_path: Path) -> None:
    baseline = delivery_scope_git_baseline(tmp_path)
    before = snapshot_delivery_scope_files(tmp_path, git_baseline=baseline)
    escaped = tmp_path / "docs" / "escaped.md"
    escaped.parent.mkdir()
    escaped.write_text("committed outside scope\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/escaped.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "provider commit"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "rm", "docs/escaped.md"], cwd=tmp_path, check=True, capture_output=True)
    allowed = tmp_path / "src" / "allowed.py"
    allowed.parent.mkdir(exist_ok=True)
    allowed.write_text("dirty in scope\n", encoding="utf-8")

    after = snapshot_delivery_scope_files(tmp_path, git_baseline=baseline)

    assert [artifact.path for artifact in detect_validation_artifacts(before, after)] == [
        "docs/escaped.md",
        "src/allowed.py",
    ]


def test_delivery_scope_snapshot_keeps_reverted_commits_visible_against_baseline(tmp_path: Path) -> None:
    baseline = delivery_scope_git_baseline(tmp_path)
    before = snapshot_delivery_scope_files(tmp_path, git_baseline=baseline)
    escaped = tmp_path / "docs" / "escaped.md"
    escaped.parent.mkdir()
    escaped.write_text("committed outside scope\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/escaped.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "provider commit"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "rm", "docs/escaped.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "provider revert"], cwd=tmp_path, check=True, capture_output=True)

    after = snapshot_delivery_scope_files(tmp_path, git_baseline=baseline)

    assert [artifact.path for artifact in detect_validation_artifacts(before, after)] == ["docs/escaped.md"]


def test_delivery_scope_snapshot_ignores_replace_refs_when_enumerating_commits(tmp_path: Path) -> None:
    baseline = delivery_scope_git_baseline(tmp_path)
    before = snapshot_delivery_scope_files(tmp_path, git_baseline=baseline)
    escaped = tmp_path / "docs" / "escaped.md"
    escaped.parent.mkdir()
    escaped.write_text("committed outside scope\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/escaped.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "provider commit"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "rm", "docs/escaped.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "provider revert"], cwd=tmp_path, check=True, capture_output=True)
    head = delivery_scope_git_baseline(tmp_path)
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    replacement = subprocess.run(
        ["git", "commit-tree", tree, "-p", baseline],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        input="replacement history\n",
    ).stdout.strip()
    subprocess.run(["git", "replace", head, replacement], cwd=tmp_path, check=True, capture_output=True)

    after = snapshot_delivery_scope_files(tmp_path, git_baseline=baseline)

    assert [artifact.path for artifact in detect_validation_artifacts(before, after)] == ["docs/escaped.md"]


def test_delivery_scope_snapshot_rejects_head_outside_baseline_history(tmp_path: Path) -> None:
    baseline = delivery_scope_git_baseline(tmp_path)
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    unrelated = subprocess.run(
        ["git", "commit-tree", tree],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        input="unrelated history\n",
    ).stdout.strip()
    subprocess.run(["git", "reset", "--hard", unrelated], cwd=tmp_path, check=True, capture_output=True)

    with pytest.raises(DeliveryScopeSnapshotError, match="no longer descends"):
        snapshot_delivery_scope_files(tmp_path, git_baseline=baseline)


def test_delivery_scope_snapshot_rejects_provider_commit_reset_to_baseline(tmp_path: Path) -> None:
    binding = validation_artifacts_module.delivery_scope_git_binding(tmp_path)
    snapshot_delivery_scope_files(
        tmp_path,
        git_baseline=binding.baseline,
        git_dir=binding.git_dir,
        git_common_dir=binding.common_dir,
        git_ignore_fingerprint=binding.ignore_fingerprint,
        git_ref_fingerprint=binding.ref_fingerprint,
    )
    escaped = tmp_path / "docs" / "discarded.md"
    escaped.parent.mkdir()
    escaped.write_text("discarded provider commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/discarded.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "discarded provider commit"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "reset", "--hard", binding.baseline], cwd=tmp_path, check=True, capture_output=True)

    with pytest.raises(DeliveryScopeSnapshotError, match="Git references changed"):
        snapshot_delivery_scope_files(
            tmp_path,
            git_baseline=binding.baseline,
            git_dir=binding.git_dir,
            git_common_dir=binding.common_dir,
            git_ignore_fingerprint=binding.ignore_fingerprint,
            git_ref_fingerprint=binding.ref_fingerprint,
        )


def test_delivery_scope_snapshot_rejects_retargeted_worktree_git_pointer(tmp_path: Path) -> None:
    linked = tmp_path.parent / f"{tmp_path.name}-linked"
    subprocess.run(
        ["git", "worktree", "add", "-b", "linked", str(linked)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    binding = validation_artifacts_module.delivery_scope_git_binding(linked)
    before = snapshot_delivery_scope_files(
        linked,
        git_baseline=binding.baseline,
        git_dir=binding.git_dir,
    )
    git_pointer = linked / ".git"
    git_pointer.chmod(git_pointer.stat().st_mode | stat.S_IWUSR)
    git_pointer.write_text(f"gitdir: {tmp_path / '.git'}\n", encoding="utf-8")

    with pytest.raises(DeliveryScopeSnapshotError, match="binding"):
        snapshot_delivery_scope_files(
            linked,
            git_baseline=binding.baseline,
            git_dir=binding.git_dir,
        )

    assert before == {}


def test_delivery_scope_snapshot_rejects_retargeted_core_worktree(tmp_path: Path) -> None:
    binding = validation_artifacts_module.delivery_scope_git_binding(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-configured-worktree"
    outside.mkdir()
    subprocess.run(
        ["git", "config", "core.worktree", str(outside)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    with pytest.raises(DeliveryScopeSnapshotError, match="worktree"):
        snapshot_delivery_scope_files(
            tmp_path,
            git_baseline=binding.baseline,
            git_dir=binding.git_dir,
        )


def test_delivery_scope_snapshot_rejects_mutated_info_exclude(tmp_path: Path) -> None:
    binding = validation_artifacts_module.delivery_scope_git_binding(tmp_path)
    before = snapshot_delivery_scope_files(
        tmp_path,
        git_baseline=binding.baseline,
        git_dir=binding.git_dir,
        git_ignore_fingerprint=binding.ignore_fingerprint,
    )
    exclude = tmp_path / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text(encoding="utf-8") + "\ntarget/\n", encoding="utf-8")
    escaped = tmp_path / "target" / "escaped"
    escaped.parent.mkdir()
    escaped.write_text("hidden by mutated exclude\n", encoding="utf-8")

    with pytest.raises(DeliveryScopeSnapshotError, match="ignore metadata changed"):
        snapshot_delivery_scope_files(
            tmp_path,
            git_baseline=binding.baseline,
            git_dir=binding.git_dir,
            git_ignore_fingerprint=binding.ignore_fingerprint,
        )

    assert before == {}


def test_delivery_scope_snapshot_rejects_mutated_worktree_gitignore(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("cache/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "track ignores"], cwd=tmp_path, check=True, capture_output=True)
    binding = validation_artifacts_module.delivery_scope_git_binding(tmp_path)
    snapshot_delivery_scope_files(
        tmp_path,
        git_baseline=binding.baseline,
        git_dir=binding.git_dir,
        git_ignore_fingerprint=binding.ignore_fingerprint,
    )
    gitignore.write_text("cache/\ntarget/\n", encoding="utf-8")

    with pytest.raises(DeliveryScopeSnapshotError, match="ignore metadata changed"):
        snapshot_delivery_scope_files(
            tmp_path,
            git_baseline=binding.baseline,
            git_dir=binding.git_dir,
            git_ignore_fingerprint=binding.ignore_fingerprint,
        )


def test_delivery_scope_snapshot_neutralizes_repository_excludes_file(tmp_path: Path) -> None:
    excludes = tmp_path.parent / f"{tmp_path.name}-global-excludes"
    excludes.write_text("target/\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "core.excludesFile", str(excludes)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    escaped = tmp_path / "target" / "escaped"
    escaped.parent.mkdir()
    escaped.write_text("must remain visible\n", encoding="utf-8")

    snapshot = snapshot_delivery_scope_files(tmp_path)

    assert "target/escaped" in snapshot


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_delivery_scope_snapshot_ignores_provider_mutable_index_flags(tmp_path: Path, flag: str) -> None:
    escaped = tmp_path / "docs" / "escaped.md"
    escaped.parent.mkdir()
    escaped.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/escaped.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "track escaped"], cwd=tmp_path, check=True, capture_output=True)
    baseline = delivery_scope_git_baseline(tmp_path)
    before = snapshot_delivery_scope_files(tmp_path, git_baseline=baseline)
    subprocess.run(["git", "update-index", flag, "docs/escaped.md"], cwd=tmp_path, check=True, capture_output=True)
    escaped.write_text("after\n", encoding="utf-8")

    after = snapshot_delivery_scope_files(tmp_path, git_baseline=baseline)

    assert [artifact.path for artifact in detect_validation_artifacts(before, after)] == ["docs/escaped.md"]


def test_delivery_scope_snapshot_bounds_large_and_binary_retained_content(tmp_path: Path) -> None:
    limit = validation_artifacts_module._MAX_DELIVERY_SCOPE_RETAINED_CONTENT_BYTES
    large = tmp_path / "large.rs"
    binary = tmp_path / "binary.rs"
    large.write_bytes(b"a" * (limit + 1))
    binary.write_bytes(b"pub fn value() {}\x00binary")

    before = snapshot_delivery_scope_files(tmp_path, include_content=lambda _path: True)
    serialized = serialize_delivery_scope_snapshot(before)

    assert before["large.rs"].content is None
    assert before["large.rs"].digest is not None
    assert before["binary.rs"].content is None
    assert before["binary.rs"].digest is not None
    assert all('"content":null' in value for value in serialized.values() if value is not None)

    large.write_bytes(b"a" * limit + b"b")
    after = snapshot_delivery_scope_files(tmp_path, include_content=lambda _path: True)
    assert [artifact.path for artifact in detect_validation_artifacts(before, after)] == ["large.rs"]


def test_delivery_scope_snapshot_retains_selected_clean_file_under_active_root(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "lib.rs").write_bytes(b"#[cfg(test)]\nmod tests {}\n")
    (source / "main.rs").write_bytes(b"fn main() {}\n")
    subprocess.run(["git", "add", "src"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "track rust sources"], cwd=tmp_path, check=True, capture_output=True)

    snapshot = snapshot_delivery_scope_files(
        tmp_path,
        include_content=lambda path: path == "src/lib.rs",
        symlink_roots=["src"],
    )

    assert snapshot["src/lib.rs"].content == b"#[cfg(test)]\nmod tests {}\n"
    assert "src/main.rs" not in snapshot


def test_delivery_scope_snapshot_rejects_malformed_persisted_value() -> None:
    with pytest.raises(DeliveryScopeSnapshotError):
        deserialize_delivery_scope_snapshot({".env": "not-json"})


def test_delivery_scope_snapshot_rejects_unavailable_root(tmp_path: Path) -> None:
    with pytest.raises(DeliveryScopeSnapshotError):
        snapshot_delivery_scope_files(tmp_path / "missing")


def test_delivery_scope_snapshot_fails_closed_when_git_query_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validation_artifacts_module, "_git_paths_z", lambda *_args, **_kwargs: None)

    with pytest.raises(DeliveryScopeSnapshotError, match="could not (?:bind Git ignore metadata|query Git changes)"):
        snapshot_delivery_scope_files(tmp_path)


def test_delivery_scope_git_paths_use_reversible_filesystem_decoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_path = b"src/non-utf8-\xff.py"
    monkeypatch.setattr(
        validation_artifacts_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=raw_path + b"\0", stderr=b""),
    )

    if os.name == "nt":
        with pytest.raises(DeliveryScopeSnapshotError, match="decode a Git path safely"):
            validation_artifacts_module._git_paths_z(tmp_path, ["ls-files", "-z"])
        return

    paths = validation_artifacts_module._git_paths_z(tmp_path, ["ls-files", "-z"])
    assert paths == [os.fsdecode(raw_path)]
    assert os.fsencode(paths[0]) == raw_path


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX byte filenames")
def test_delivery_scope_snapshot_audits_non_utf8_git_path(tmp_path: Path) -> None:
    raw_name = b"non-utf8-\xff.py"
    raw_path = os.path.join(os.fsencode(tmp_path), raw_name)
    try:
        descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as exc:
        pytest.skip(f"byte filenames are unavailable: {exc}")
    try:
        os.write(descriptor, b"changed\n")
    finally:
        os.close(descriptor)

    snapshot = snapshot_delivery_scope_files(tmp_path)

    decoded_name = os.fsdecode(raw_name)
    assert decoded_name in snapshot
    assert snapshot[decoded_name].digest is not None


@pytest.mark.skipif(os.name == "nt", reason="POSIX treats backslashes as filename characters")
def test_delivery_scope_snapshot_preserves_posix_backslash_filename(tmp_path: Path) -> None:
    physical_name = r"scripts\payload"
    (tmp_path / physical_name).write_text("outside lexical scripts directory\n", encoding="utf-8")

    snapshot = snapshot_delivery_scope_files(tmp_path)

    assert physical_name in snapshot
    assert "scripts/payload" not in snapshot


def test_delivery_scope_snapshot_wraps_enumeration_and_entry_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validation_artifacts_module.os, "scandir", lambda path: (_ for _ in ()).throw(OSError()))
    with pytest.raises(DeliveryScopeSnapshotError):
        snapshot_delivery_scope_files(tmp_path)

    class BrokenEntry:
        name = "broken"

        def stat(self, *, follow_symlinks: bool):
            raise OSError

    (tmp_path / "broken").write_text("candidate\n", encoding="utf-8")
    monkeypatch.setattr(validation_artifacts_module.os, "scandir", lambda path: [BrokenEntry()])
    with pytest.raises(DeliveryScopeSnapshotError):
        snapshot_delivery_scope_files(tmp_path)


def test_delivery_scope_snapshot_records_special_file(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    fifo = tmp_path / "events.pipe"
    try:
        os.mkfifo(fifo)
    except OSError as exc:
        pytest.skip(f"FIFO creation is unavailable: {exc}")

    snapshot = snapshot_delivery_scope_files(tmp_path, symlink_roots=["."])

    assert snapshot["events.pipe"].digest == f"special:{stat.S_IFIFO}"


def test_delivery_scope_snapshot_hashes_only_git_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracked = tmp_path / "tracked.py"
    tracked.write_bytes(b"clean\n")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "track candidate"], cwd=tmp_path, check=True, capture_output=True)
    original_snapshot = validation_artifacts_module._snapshot_delivery_scope_regular_file
    hashed: list[str] = []

    def record_hash(path, **kwargs):
        hashed.append(str(path))
        return original_snapshot(path, **kwargs)

    monkeypatch.setattr(validation_artifacts_module, "_snapshot_delivery_scope_regular_file", record_hash)

    clean = snapshot_delivery_scope_files(tmp_path)
    tracked.write_bytes(b"changed\n")
    dirty = snapshot_delivery_scope_files(tmp_path)

    assert "tracked.py" not in clean
    assert "tracked.py" in dirty
    assert sum(Path(path).name == tracked.name for path in hashed) == 1


def test_delivery_scope_snapshot_prunes_ephemeral_ignored_roots_before_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/\nignored-output/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "ignore outputs"], cwd=tmp_path, check=True, capture_output=True)
    dependency = tmp_path / "node_modules" / "package" / "index.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("dependency\n", encoding="utf-8")
    persistent = tmp_path / "ignored-output" / "escaped.txt"
    persistent.parent.mkdir()
    persistent.write_text("persistent\n", encoding="utf-8")
    original_open = validation_artifacts_module._open_delivery_scope_directory
    opened: list[str] = []

    def record_open(path, **kwargs):
        opened.append(str(path))
        return original_open(path, **kwargs)

    monkeypatch.setattr(validation_artifacts_module, "_open_delivery_scope_directory", record_open)

    snapshot = snapshot_delivery_scope_files(
        tmp_path,
        exclude_ephemeral=lambda path: "node_modules" in Path(path).parts,
    )

    assert not any(path == "node_modules" for path in opened)
    assert "node_modules/package/index.js" not in snapshot
    assert "ignored-output/escaped.txt" in snapshot


@pytest.mark.parametrize("force_path_fallback", [False, True])
def test_delivery_scope_snapshot_checks_symlinks_but_not_regular_files_in_ephemeral_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_path_fallback: bool,
) -> None:
    if force_path_fallback:
        monkeypatch.setattr(
            validation_artifacts_module, "_delivery_scope_descriptor_traversal_supported", lambda: False
        )
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "ignore build output"], cwd=tmp_path, check=True, capture_output=True)
    build = tmp_path / "build"
    build.mkdir()
    (build / "cache.bin").write_bytes(b"disposable\n")
    outside = tmp_path.parent / f"{tmp_path.name}-ephemeral-outside"
    outside.mkdir()
    try:
        (build / "escape").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    snapshot = snapshot_delivery_scope_files(
        tmp_path,
        validate_symlink=lambda _path, _target: True,
        symlink_roots=["."],
        exclude_ephemeral=lambda path: "build" in Path(path).parts,
    )

    assert "build/escape" in snapshot
    assert "build/cache.bin" not in snapshot

    with pytest.raises(DeliveryScopeSnapshotError, match="escapes the active write scope"):
        snapshot_delivery_scope_files(
            tmp_path,
            validate_symlink=lambda _path, _target: False,
            symlink_roots=["."],
            exclude_ephemeral=lambda path: "build" in Path(path).parts,
        )


def test_delivery_scope_link_like_recognizes_windows_reparse_directory() -> None:
    entry_stat = SimpleNamespace(
        st_mode=stat.S_IFDIR,
        st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024),
    )

    assert validation_artifacts_module._delivery_scope_link_like(entry_stat) is True


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_delivery_scope_snapshot_rejects_windows_junction_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-junction-outside"
    outside.mkdir()
    junction = tmp_path / "build" / "escape"
    junction.parent.mkdir()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True,
        capture_output=True,
    )

    with pytest.raises(DeliveryScopeSnapshotError, match="escapes the active write scope"):
        snapshot_delivery_scope_files(
            tmp_path,
            validate_symlink=lambda _path, _target: False,
            symlink_roots=["."],
            exclude_ephemeral=lambda path: "build" in Path(path).parts,
        )


def test_delivery_scope_snapshot_keeps_tracked_changes_under_ephemeral_named_root(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    tracked = tmp_path / "node_modules" / "tracked.js"
    tracked.parent.mkdir()
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-f", "node_modules/tracked.js"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "track generated source"], cwd=tmp_path, check=True, capture_output=True)
    tracked.write_text("after\n", encoding="utf-8")

    snapshot = snapshot_delivery_scope_files(
        tmp_path,
        exclude_ephemeral=lambda path: "node_modules" in Path(path).parts,
    )

    assert "node_modules/tracked.js" in snapshot


def test_delivery_scope_snapshot_propagates_typed_regular_file_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "file.txt").write_text("content\n", encoding="utf-8")
    monkeypatch.setattr(
        validation_artifacts_module,
        "_snapshot_delivery_scope_regular_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(DeliveryScopeSnapshotError("changed")),
    )

    with pytest.raises(DeliveryScopeSnapshotError):
        snapshot_delivery_scope_files(tmp_path)


def test_delivery_scope_regular_file_snapshot_rejects_type_race_and_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "file.txt"
    source.write_text("content\n", encoding="utf-8")
    monkeypatch.setattr(
        validation_artifacts_module.os,
        "fstat",
        lambda descriptor: SimpleNamespace(st_mode=stat.S_IFDIR),
    )
    with pytest.raises(DeliveryScopeSnapshotError):
        validation_artifacts_module._snapshot_delivery_scope_regular_file(
            source,
            mode=0o644,
            retain_content=False,
        )

    source_stat = source.stat()
    monkeypatch.setattr(
        validation_artifacts_module.os,
        "fstat",
        lambda descriptor: SimpleNamespace(
            st_mode=stat.S_IFREG,
            st_dev=source_stat.st_dev,
            st_ino=source_stat.st_ino + 1,
        ),
    )
    with pytest.raises(DeliveryScopeSnapshotError):
        validation_artifacts_module._snapshot_delivery_scope_regular_file(
            source,
            mode=0o644,
            retain_content=False,
            expected_identity=(source_stat.st_dev, source_stat.st_ino),
        )

    with pytest.raises(DeliveryScopeSnapshotError):
        validation_artifacts_module._snapshot_delivery_scope_regular_file(
            tmp_path / "missing",
            mode=0o644,
            retain_content=False,
        )


def test_delivery_scope_snapshot_rejects_directory_replaced_by_symlink_before_recursion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.scandir not in os.supports_fd or os.open not in os.supports_dir_fd:
        pytest.skip("descriptor-relative directory traversal is unavailable")
    scoped = tmp_path / "scoped"
    scoped.mkdir()
    (scoped / "inside.txt").write_text("inside\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n", encoding="utf-8")
    original_open_directory = validation_artifacts_module._open_delivery_scope_directory
    replaced = False

    def replace_before_open(path, *, expected_identity, dir_fd=None):
        nonlocal replaced
        if path == "scoped" and dir_fd is not None and not replaced:
            replaced = True
            scoped.rename(tmp_path / "scoped-original")
            scoped.symlink_to(outside, target_is_directory=True)
        return original_open_directory(path, expected_identity=expected_identity, dir_fd=dir_fd)

    monkeypatch.setattr(validation_artifacts_module, "_open_delivery_scope_directory", replace_before_open)

    with pytest.raises(DeliveryScopeSnapshotError):
        snapshot_delivery_scope_files(tmp_path)

    assert replaced is True


def test_delivery_scope_descriptor_traversal_wraps_enumeration_and_entry_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.scandir not in os.supports_fd or os.open not in os.supports_dir_fd:
        pytest.skip("descriptor-relative directory traversal is unavailable")

    def fail_scandir(_descriptor: int):
        raise OSError("unavailable")

    with monkeypatch.context() as context:
        context.setattr(validation_artifacts_module.os, "scandir", fail_scandir)
        context.setattr(validation_artifacts_module.os, "supports_fd", {*os.supports_fd, fail_scandir})
        with pytest.raises(DeliveryScopeSnapshotError, match="enumerate the project tree"):
            snapshot_delivery_scope_files(tmp_path)

    class BrokenEntry:
        name = "broken"

        def stat(self, *, follow_symlinks: bool):
            raise OSError("unavailable")

    def broken_scandir(_descriptor: int):
        return [BrokenEntry()]

    (tmp_path / "broken").write_text("candidate\n", encoding="utf-8")
    with monkeypatch.context() as context:
        context.setattr(validation_artifacts_module.os, "scandir", broken_scandir)
        context.setattr(validation_artifacts_module.os, "supports_fd", {*os.supports_fd, broken_scandir})
        with pytest.raises(DeliveryScopeSnapshotError, match="inspect a project path"):
            snapshot_delivery_scope_files(tmp_path)


@pytest.mark.parametrize("changed_on_call", [2, 3])
def test_delivery_scope_path_fallback_rejects_directory_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_on_call: int,
) -> None:
    root = tmp_path.resolve()
    original_stat = Path.stat
    root_stat_calls = 0

    def changing_stat(path: Path, *, follow_symlinks: bool = True):
        nonlocal root_stat_calls
        value = original_stat(path, follow_symlinks=follow_symlinks)
        if path == root and not follow_symlinks:
            root_stat_calls += 1
            if root_stat_calls == changed_on_call:
                return SimpleNamespace(
                    st_mode=value.st_mode,
                    st_dev=value.st_dev,
                    st_ino=value.st_ino + 1,
                )
        return value

    monkeypatch.setattr(validation_artifacts_module.os, "supports_fd", set())
    monkeypatch.setattr(validation_artifacts_module.Path, "stat", changing_stat)

    with pytest.raises(DeliveryScopeSnapshotError, match="directory changed"):
        snapshot_delivery_scope_files(root)


def test_delivery_scope_path_fallback_uses_path_stat_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "file.txt").write_text("content\n", encoding="utf-8")
    original_scandir = os.scandir

    class MismatchedEntry:
        def __init__(self, entry) -> None:
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def stat(self, *, follow_symlinks: bool):
            value = self._entry.stat(follow_symlinks=follow_symlinks)
            return SimpleNamespace(
                st_mode=value.st_mode,
                st_dev=value.st_dev,
                st_ino=value.st_ino + 1,
            )

    def mismatched_scandir(path):
        return [MismatchedEntry(entry) for entry in original_scandir(path)]

    monkeypatch.setattr(validation_artifacts_module.os, "supports_fd", set())
    monkeypatch.setattr(validation_artifacts_module.os, "scandir", mismatched_scandir)

    snapshot = snapshot_delivery_scope_files(tmp_path)

    assert "nested/file.txt" in snapshot


def test_delivery_scope_path_fallback_rejects_file_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "file.txt"
    source.write_text("before\n", encoding="utf-8")
    original_snapshot = validation_artifacts_module._snapshot_delivery_scope_regular_file

    def replace_after_read(path, **kwargs):
        result = original_snapshot(path, **kwargs)
        if Path(path) == source:
            source.rename(tmp_path / "file-original.txt")
            source.write_text("after\n", encoding="utf-8")
        return result

    monkeypatch.setattr(validation_artifacts_module.os, "supports_fd", set())
    monkeypatch.setattr(validation_artifacts_module, "_snapshot_delivery_scope_regular_file", replace_after_read)

    with pytest.raises(DeliveryScopeSnapshotError, match="project path changed"):
        snapshot_delivery_scope_files(tmp_path)


def test_open_delivery_scope_directory_rejects_identity_mismatch(tmp_path: Path) -> None:
    if not validation_artifacts_module._delivery_scope_descriptor_traversal_supported():
        pytest.skip("descriptor-relative directory traversal is unavailable")
    value = tmp_path.stat()

    with pytest.raises(DeliveryScopeSnapshotError, match="directory changed"):
        validation_artifacts_module._open_delivery_scope_directory(
            tmp_path,
            expected_identity=(value.st_dev, value.st_ino + 1),
        )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "[]",
        '{"exists":"yes","status":"filesystem"}',
        '{"exists":true,"status":1}',
    ],
)
def test_delivery_scope_snapshot_rejects_malformed_persisted_fields(value: str | None) -> None:
    with pytest.raises(DeliveryScopeSnapshotError):
        deserialize_delivery_scope_snapshot({"file": value})


def test_snapshot_skips_ignored_roots_and_records_deleted_files(tmp_path: Path):
    _init_repo(tmp_path)
    source = tmp_path / "src" / "main.py"
    source.unlink()
    ignored = tmp_path / "reports" / "runtime.txt"
    ignored.parent.mkdir()
    ignored.write_text("generated\n")

    snapshot = snapshot_validation_dirty_files(tmp_path, ignored_roots=["reports"])

    assert set(snapshot) == {"src/main.py"}
    assert snapshot["src/main.py"] == FileSnapshot(
        status="tracked",
        exists=False,
        content=None,
        mode=None,
    )


def test_snapshot_records_directory_like_dirty_path(tmp_path: Path, monkeypatch):
    from core import validation_artifacts

    dirty_dir = tmp_path / "generated"
    dirty_dir.mkdir()
    monkeypatch.setattr(validation_artifacts, "_dirty_paths", lambda cwd: ({"generated"}, {"generated"}))

    snapshot = snapshot_validation_dirty_files(tmp_path)

    entry = snapshot["generated"]
    assert entry.status == "untracked"
    assert entry.exists
    assert entry.content is None
    assert entry.mode is not None


def test_snapshot_records_broken_symlink_dirty_path(tmp_path: Path):
    _init_repo(tmp_path)
    link = tmp_path / "generated-link"
    _make_symlink(link)

    snapshot = snapshot_validation_dirty_files(tmp_path)

    assert snapshot["generated-link"] == FileSnapshot(
        status="untracked",
        exists=True,
        content=None,
        mode=None,
        symlink_target="missing-target",
    )


def test_restore_rejects_paths_outside_project_root(tmp_path: Path):
    errors = restore_validation_artifacts(
        tmp_path,
        before={},
        artifacts=[_artifact("../outside.txt", before="clean", after="untracked")],
    )

    assert errors == ["../outside.txt: path resolves outside project root"]


def test_restore_deletes_new_untracked_file(tmp_path: Path):
    artifact = tmp_path / "generated.txt"
    artifact.write_text("generated\n")

    errors = restore_validation_artifacts(
        tmp_path,
        before={},
        artifacts=[_artifact("generated.txt", before="clean", after="untracked")],
    )

    assert errors == []
    assert not artifact.exists()


def test_restore_deletes_new_broken_symlink_artifact(tmp_path: Path):
    artifact = tmp_path / "generated-link"
    _make_symlink(artifact)

    errors = restore_validation_artifacts(
        tmp_path,
        before={},
        artifacts=[_artifact("generated-link", before="clean", after="untracked")],
    )

    assert errors == []
    assert not artifact.exists()
    assert not artifact.is_symlink()


def test_restore_reports_directory_for_new_untracked_artifact(tmp_path: Path):
    artifact = tmp_path / "generated"
    artifact.mkdir()

    errors = restore_validation_artifacts(
        tmp_path,
        before={},
        artifacts=[_artifact("generated", before="clean", after="untracked")],
    )

    assert errors == ["generated: artifact path is a directory"]


def test_restore_clean_tracked_file_from_head(tmp_path: Path):
    _init_repo(tmp_path)
    source = tmp_path / "src" / "main.py"
    source.write_text("# generated during validation\n")

    errors = restore_validation_artifacts(
        tmp_path,
        before={},
        artifacts=[_artifact("src/main.py", before="clean", after="tracked")],
    )

    assert errors == []
    assert source.read_text() == "# placeholder\n"


def test_restore_records_head_restore_error(tmp_path: Path):
    errors = restore_validation_artifacts(
        tmp_path,
        before={},
        artifacts=[_artifact("src/main.py", before="clean", after="tracked")],
    )

    assert len(errors) == 1
    assert errors[0].startswith("src/main.py:")


def test_restore_deletes_file_recreated_after_task_deleted_it(tmp_path: Path):
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir()
    source.write_text("# generated during validation\n")

    errors = restore_validation_artifacts(
        tmp_path,
        before={"src/main.py": FileSnapshot(status="tracked", exists=False, content=None, mode=None)},
        artifacts=[_artifact("src/main.py")],
    )

    assert errors == []
    assert not source.exists()


def test_restore_deletes_broken_symlink_recreated_after_task_deleted_it(tmp_path: Path):
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir()
    _make_symlink(source)

    errors = restore_validation_artifacts(
        tmp_path,
        before={"src/main.py": FileSnapshot(status="tracked", exists=False, content=None, mode=None)},
        artifacts=[_artifact("src/main.py")],
    )

    assert errors == []
    assert not source.exists()
    assert not source.is_symlink()


def test_restore_reports_directory_recreated_after_task_deleted_it(tmp_path: Path):
    source = tmp_path / "src" / "main.py"
    source.mkdir(parents=True)

    errors = restore_validation_artifacts(
        tmp_path,
        before={"src/main.py": FileSnapshot(status="tracked", exists=False, content=None, mode=None)},
        artifacts=[_artifact("src/main.py")],
    )

    assert errors == ["src/main.py: artifact path is a directory"]


def test_restore_recreates_dirty_symlink_replaced_by_file_without_touching_target(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-target.txt"
    outside.write_text("outside original\n")
    source = tmp_path / "generated-link"
    _make_symlink(source, str(outside))
    source.unlink()
    source.write_text("regular file artifact\n")

    errors = restore_validation_artifacts(
        tmp_path,
        before={
            "generated-link": FileSnapshot(
                status="untracked",
                exists=True,
                content=None,
                mode=None,
                symlink_target=str(outside),
            )
        },
        artifacts=[_artifact("generated-link", before="untracked", after="untracked")],
    )

    assert errors == []
    assert source.is_symlink()
    assert source.samefile(outside)
    assert outside.read_text() == "outside original\n"


def test_restore_regular_file_replaced_by_symlink_does_not_write_through_target(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-target.txt"
    outside.write_text("outside original\n")
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir()
    _make_symlink(source, str(outside))

    errors = restore_validation_artifacts(
        tmp_path,
        before={
            "src/main.py": FileSnapshot(
                status="tracked",
                exists=True,
                content=b"# task change\n",
                mode=0o644,
            )
        },
        artifacts=[_artifact("src/main.py")],
    )

    assert errors == []
    assert not source.is_symlink()
    assert source.read_text() == "# task change\n"
    assert outside.read_text() == "outside original\n"


def test_restore_deletes_existing_untracked_file_with_unreadable_snapshot(tmp_path: Path):
    artifact = tmp_path / "generated.txt"
    artifact.write_text("generated\n")

    errors = restore_validation_artifacts(
        tmp_path,
        before={"generated.txt": FileSnapshot(status="untracked", exists=True, content=None, mode=None)},
        artifacts=[_artifact("generated.txt", before="untracked", after="untracked")],
    )

    assert errors == []
    assert not artifact.exists()


def test_restore_deletes_existing_untracked_broken_symlink_with_unreadable_snapshot(tmp_path: Path):
    artifact = tmp_path / "generated-link"
    _make_symlink(artifact)

    errors = restore_validation_artifacts(
        tmp_path,
        before={"generated-link": FileSnapshot(status="untracked", exists=True, content=None, mode=None)},
        artifacts=[_artifact("generated-link", before="untracked", after="untracked")],
    )

    assert errors == []
    assert not artifact.exists()
    assert not artifact.is_symlink()


def test_restore_reports_existing_untracked_directory_with_unreadable_snapshot(tmp_path: Path):
    artifact = tmp_path / "generated"
    artifact.mkdir()

    errors = restore_validation_artifacts(
        tmp_path,
        before={"generated": FileSnapshot(status="untracked", exists=True, content=None, mode=None)},
        artifacts=[_artifact("generated", before="untracked", after="untracked")],
    )

    assert errors == ["generated: artifact path is a directory"]


def test_restore_records_error_when_existing_tracked_snapshot_cannot_restore_from_head(tmp_path: Path):
    errors = restore_validation_artifacts(
        tmp_path,
        before={"src/main.py": FileSnapshot(status="tracked", exists=True, content=None, mode=None)},
        artifacts=[_artifact("src/main.py")],
    )

    assert len(errors) == 1
    assert errors[0].startswith("src/main.py:")


def test_restore_records_os_error_when_content_path_is_directory(tmp_path: Path):
    source = tmp_path / "src" / "main.py"
    source.mkdir(parents=True)

    errors = restore_validation_artifacts(
        tmp_path,
        before={"src/main.py": FileSnapshot(status="tracked", exists=True, content=b"# task change\n", mode=0o644)},
        artifacts=[_artifact("src/main.py")],
    )

    assert len(errors) == 1
    assert errors[0].startswith("src/main.py:")
