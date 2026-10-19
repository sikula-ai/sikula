"""Opt-in CI sharding; every parametrized test belongs to exactly one runner."""

from __future__ import annotations

import pytest


def pytest_addoption(parser):
    group = parser.getgroup("CI sharding")
    group.addoption("--ci-shard-count", type=int, help="Total number of CI runners")
    group.addoption("--ci-shard-index", type=int, help="This runner's index, starting at 1")


def pytest_configure(config):
    count = config.getoption("ci_shard_count")
    index = config.getoption("ci_shard_index")
    if count is None and index is None:
        return
    if count is None or index is None or not 1 <= index <= count:
        raise pytest.UsageError("Specify both --ci-shard-count and --ci-shard-index with 1 <= index <= count")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    count = config.getoption("ci_shard_count")
    if count is None:
        return
    index = config.getoption("ci_shard_index") - 1
    # Interleave individual cases instead of assigning whole files: expensive
    # parametrized delivery scenarios are spread across runners. Sorting avoids
    # dependence on collection order and Python's per-process hash seed.
    selected, deselected = [], []
    for position, item in enumerate(sorted(items, key=lambda item: item.nodeid)):
        (selected if position % count == index else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)


def pytest_report_header(config):
    count = config.getoption("ci_shard_count")
    if count is not None:
        return f"CI shard {config.getoption('ci_shard_index')}/{count} (sorted test IDs, interleaved)"
