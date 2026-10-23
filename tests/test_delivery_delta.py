from contextlib import contextmanager
import io
import sys
import tracemalloc
from unittest.mock import Mock

import pytest

from core import delivery_delta as delta
from tests.test_delivery_repair import _git


DIFF = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--ignore-submodules=none", "--relative"]
RAW = b":100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" M"


@pytest.mark.parametrize("nested", [False, True])
def test_complete_large_git_metadata_and_literal_paths(tmp_path, nested):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "core.autocrlf", "true")
    _git(tmp_path, "commit", "--allow-empty", "-m", "base")
    origin = _git(tmp_path, "rev-parse", "HEAD")
    root = tmp_path / "app" if nested else tmp_path
    root.mkdir(exist_ok=True)
    names = [f"part-{i:04}.txt" for i in range(256)] + ["space ü.txt", "[literal].txt", "-option.txt"]
    for name in names:
        (root / name).write_bytes(b"public text\r\n")
    if nested:
        (tmp_path / "outside.txt").write_bytes(b"\0not project input")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "candidate")
    candidate = _git(tmp_path, "rev-parse", "HEAD")
    paths = []
    assert delta.inspect_delta_metadata(root, DIFF, [origin, candidate], is_private=lambda p: paths.append(p)) == len(
        names
    )
    assert set(paths) == set(names)


def _metadata(monkeypatch, raw, stats):
    @contextmanager
    def records(root, args, budget):
        yield iter(raw if "--raw" in args else stats)

    monkeypatch.setattr(delta, "_git_records", records)


@pytest.mark.parametrize(
    "failure",
    ["missing_path", "raw", "sha", "binary", "mismatch", "missing", "extra", "gitlink", "traversal", "private"],
)
def test_complete_metadata_rejects_unsafe_or_mismatched_tail(tmp_path, monkeypatch, failure):
    names = [f"path-{i}".encode() for i in range(256)]
    raw = [part for name in names for part in (RAW, name)]
    stats = [b"1\t0\t" + name for name in names]
    raw.extend([RAW, b"tail"])
    stats.append(b"1\t0\ttail")
    if failure == "missing_path":
        raw.pop()
    elif failure == "raw":
        raw[-2] = b"invalid"
    elif failure == "sha":
        raw[-2] = RAW.replace(b"b" * 40, b"b" * 64)
    elif failure == "binary":
        stats[-1] = b"-\t-\ttail"
    elif failure == "mismatch":
        stats[-1] = b"1\t0\tother"
    elif failure == "missing":
        stats.pop()
    elif failure == "extra":
        stats.append(b"1\t0\textra")
    elif failure == "gitlink":
        raw[-2] = RAW.replace(b"100644", b"160000", 1)
    elif failure == "traversal":
        raw[-1] = b"../outside"
    _metadata(monkeypatch, raw, stats)
    with pytest.raises(ValueError):
        delta.inspect_delta_metadata(
            tmp_path, DIFF, ["a" * 40, "b" * 40], is_private=lambda p: failure == "private" and p == "tail"
        )


@pytest.mark.parametrize("path", ["colon:name", "tab\tname", "newline\nname", "雪.txt"])
def test_metadata_preserves_literal_path_bytes(tmp_path, monkeypatch, path):
    encoded = path.encode()
    _metadata(monkeypatch, [RAW, encoded], [b"1\t0\t" + encoded])
    private = Mock(return_value=False)
    assert delta.inspect_delta_metadata(tmp_path, DIFF, ["a" * 40, "b" * 40], is_private=private) == 1
    private.assert_called_once_with(path)


@pytest.mark.parametrize("failure", ["record", "unterminated", "bytes", "deadline", "paths"])
def test_metadata_resource_limits_reject_incomplete_inspection(tmp_path, monkeypatch, failure):
    if failure == "paths":
        monkeypatch.setattr(delta, "MAX_METADATA_PATHS", 1)
        _metadata(monkeypatch, [RAW, b"one", RAW, b"two"], [b"1\t0\tone", b"1\t0\ttwo"])
        with pytest.raises(ValueError, match="budget"):
            delta.inspect_delta_metadata(tmp_path, DIFF, [], is_private=lambda p: False)
        return
    data = b"entry\0"
    budget = delta._ScanBudget()
    if failure == "record":
        monkeypatch.setattr(delta, "MAX_METADATA_RECORD_BYTES", 2)
    elif failure == "unterminated":
        data = b"entry"
    elif failure == "bytes":
        budget.remaining = 2
    else:
        budget.deadline = 0
    with pytest.raises(ValueError):
        list(delta._nul_records(io.BytesIO(data), budget))


def test_record_reader_handles_boundaries_without_buffering_the_stream(monkeypatch):
    monkeypatch.setattr(delta, "METADATA_CHUNK_BYTES", 3)
    assert list(delta._nul_records(io.BytesIO(b"one\0two\0\0three\0"), delta._ScanBudget())) == [
        b"one",
        b"two",
        b"",
        b"three",
    ]


def _scanner_allocated_bytes():
    # Coverage and other test instrumentation allocate in the same process.
    # Include the simulated stream's buffers as well as the scanner's allocations.
    # Sample while records are live, before the scanner releases retained paths.
    snapshot = tracemalloc.take_snapshot().filter_traces(
        [tracemalloc.Filter(True, delta.__file__), tracemalloc.Filter(True, __file__)]
    )
    return sum(trace.size for trace in snapshot.traces)


def test_many_records_have_bounded_memory():
    class Stream:
        left = 10000

        def read(self, size):
            assert size <= delta.METADATA_CHUNK_BYTES
            if not self.left:
                return b""
            self.left -= 1
            return b"x" * (size - 1) + b"\0"

    budget = delta._ScanBudget(remaining=64 * 1024 * 1024)
    samples = []
    tracemalloc.start()
    try:
        for count, _ in enumerate(delta._nul_records(Stream(), budget), 1):
            if count % 1000 == 0:
                samples.append(_scanner_allocated_bytes())
    finally:
        tracemalloc.stop()
    assert count == 10000
    assert max(samples) < 256 * 1024


def test_inspection_does_not_retain_the_path_inventory(tmp_path, monkeypatch):
    class Stream:
        def __init__(self, raw):
            self.records = (
                (RAW + b"\0" if raw else b"1\t0\t") + f"file-{index:05}-".encode() + b"x" * 256 + b"\0"
                for index in range(10000)
            )
            self.pending = b""

        def read(self, size):
            while len(self.pending) < size:
                entry = next(self.records, b"")
                if not entry:
                    break
                self.pending += entry
            chunk, self.pending = self.pending[:size], self.pending[size:]
            return chunk

    samples = []

    def sampled_records(raw, budget):
        for index, record in enumerate(delta._nul_records(Stream(raw), budget), 1):
            yield record
            if index % 1000 == 0:
                samples.append(_scanner_allocated_bytes())

    @contextmanager
    def records(root, args, budget):
        yield sampled_records("--raw" in args, budget)

    monkeypatch.setattr(delta, "_git_records", records)
    # Memory tracing can be slower than ordinary inspection on CI workers.
    monkeypatch.setattr(delta, "METADATA_TIMEOUT_SECONDS", 60)
    tracemalloc.start()
    try:
        count = delta.inspect_delta_metadata(tmp_path, DIFF, [], is_private=lambda p: False)
    finally:
        tracemalloc.stop()
    assert count == 10000
    assert max(samples) < 256 * 1024


@pytest.mark.parametrize("failure", [None, "exit", "parse", "interrupt", "timeout", "unconsumed"])
def test_git_process_is_reaped_and_stream_closed_on_every_exit(tmp_path, monkeypatch, failure):
    process = Mock()
    process.stdout = io.BytesIO(b"entry\0other\0")
    process.__enter__ = Mock(return_value=process)
    process.__exit__ = Mock(side_effect=lambda *args: process.stdout.close())
    process.poll.return_value = None
    process.wait.return_value = 1 if failure == "exit" else 0
    monkeypatch.setattr(delta.subprocess, "Popen", Mock(return_value=process))
    timer = Mock()
    monkeypatch.setattr(delta, "Timer", Mock(return_value=timer))
    budget = delta._ScanBudget()

    def read():
        with delta._git_records(tmp_path, DIFF, budget) as records:
            assert next(records) == b"entry"
            if failure == "parse":
                raise ValueError("Invalid metadata")
            if failure == "interrupt":
                raise KeyboardInterrupt()
            if failure == "timeout":
                budget.deadline = 0
            if failure != "unconsumed":
                list(records)

    if failure:
        with pytest.raises(KeyboardInterrupt if failure == "interrupt" else ValueError):
            read()
    else:
        read()
    assert process.stdout.closed
    process.kill.assert_called_once()
    assert process.wait.called
    timer.cancel.assert_called_once()
    timer.join.assert_called_once()


def test_blocked_git_pipe_is_killed_on_deadline(tmp_path, monkeypatch):
    popen = delta.subprocess.Popen
    children = []

    def blocked(*args, **kwargs):
        process = popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(delta.subprocess, "Popen", blocked)
    monkeypatch.setattr(delta, "METADATA_TIMEOUT_SECONDS", 0.2)
    with pytest.raises(ValueError, match="timed out"):
        with delta._git_records(tmp_path, DIFF, delta._ScanBudget()) as records:
            list(records)
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdout.closed


def test_metadata_byte_budget_is_shared_across_both_git_reads(tmp_path, monkeypatch):
    streams = [RAW + b"\0name\0", b"1\t0\tname\0"]
    monkeypatch.setattr(delta, "MAX_METADATA_BYTES", len(streams[0]) + len(streams[1]) - 1)

    @contextmanager
    def records(root, args, budget):
        yield delta._nul_records(io.BytesIO(streams.pop(0)), budget)

    monkeypatch.setattr(delta, "_git_records", records)
    with pytest.raises(ValueError, match="work budget"):
        delta.inspect_delta_metadata(tmp_path, DIFF, [], is_private=lambda p: False)
