from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from tests import ci_shard

pytest_plugins = ["pytester"]


def test_shards_partition_all_cases_independently_of_collection_order():
    items = [
        SimpleNamespace(nodeid=f"tests/test_{file}.py::test_case[{case}]") for file in range(5) for case in range(7)
    ]
    partitions = []
    for index in range(1, 5):
        options = {"ci_shard_count": 4, "ci_shard_index": index}
        rejected = []
        config = SimpleNamespace(
            getoption=options.get,
            hook=SimpleNamespace(pytest_deselected=lambda items: rejected.extend(items)),
        )
        selected = list(reversed(items))
        ci_shard.pytest_configure(config)
        ci_shard.pytest_collection_modifyitems(config, selected)
        assert Counter(item.nodeid for item in selected + rejected) == Counter(item.nodeid for item in items)
        same = list(items)
        ci_shard.pytest_collection_modifyitems(config, same)
        assert same == selected
        partitions.append(selected)
    assert Counter(item.nodeid for part in partitions for item in part) == Counter(item.nodeid for item in items)
    assert max(map(len, partitions)) - min(map(len, partitions)) <= 1


def test_sharding_is_opt_in():
    config = SimpleNamespace(getoption=lambda name: None)
    items = [SimpleNamespace(nodeid="b"), SimpleNamespace(nodeid="a")]
    before = items[:]
    ci_shard.pytest_configure(config)
    ci_shard.pytest_collection_modifyitems(config, items)
    assert items == before
    assert ci_shard.pytest_report_header(config) is None


@pytest.mark.parametrize("count,index", [(None, 1), (4, None), (0, 1), (4, 0), (4, 5), (-1, 1)])
def test_invalid_shard_selection_fails_before_collection(count, index):
    config = SimpleNamespace(getoption={"ci_shard_count": count, "ci_shard_index": index}.get)
    with pytest.raises(pytest.UsageError):
        ci_shard.pytest_configure(config)


@pytest.mark.parametrize("workers", [0, 2])
def test_sharded_pytest_preserves_failures_skips_and_selection(pytester, workers, monkeypatch):
    # Run the actual plugin in independent processes, including xdist workers.
    # Copy it into the temporary project so no installed Sikula is needed.
    pytester.makepyfile(ci_shard=Path(ci_shard.__file__).read_text(encoding="utf-8"))
    pytester.makepyfile(
        test_cases="""
        import pytest

        @pytest.mark.parametrize("case", range(12))
        def test_case(case):
            if case == 4:
                pytest.skip("Platform condition")
            assert case != 7

        def test_unselected():
            raise AssertionError("Must be deselected by -k")
        """
    )
    cases = []
    failures = skips = 0
    for index in range(1, 5):
        monkeypatch.setenv("PYTHONHASHSEED", str(index))
        report = pytester.path / f"results-{index}.xml"
        result = pytester.runpytest_subprocess(
            "-p",
            "ci_shard",
            "-n",
            str(workers),
            "--dist",
            "worksteal",
            "--ci-shard-count=4",
            f"--ci-shard-index={index}",
            "-k",
            "not unselected",
            f"--junitxml={report}",
            "-q",
        )
        assert result.ret in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED)
        root = ElementTree.parse(report).getroot()
        current = root.findall(".//testcase")
        cases.extend(case.attrib["name"] for case in current)
        assert len(current) == 3
        failure_count = len(root.findall(".//failure"))
        failures += failure_count
        skips += len(root.findall(".//skipped"))
        assert result.ret == (pytest.ExitCode.TESTS_FAILED if failure_count else pytest.ExitCode.OK)
    assert Counter(cases) == Counter(f"test_case[{index}]" for index in range(12))
    assert failures == skips == 1
