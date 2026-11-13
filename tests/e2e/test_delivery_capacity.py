"""One small default-CI case; opt-in larger graphs use tests.delivery_capacity."""

from tests.delivery_capacity import run_case


def test_capacity_delivery_repairs_late_regression_and_replays_without_calls(tmp_path):
    report = run_case(tmp_path / "fixture", depth=1, scenario="repair")
    assert report["completed"], report["operations"]
    assert report["interrupted"]
    assert report["implementations"] == 4
    assert report["semantic_gaps"]
    assert {call["stage"] for call in report["calls"]} >= {"semantic", "security", "repair_author"}
    assert report["evidence_totals"]["bytes_read"] > 0
    assert report["operations"][-1]["calls"] == 0
    # Replay makes no provider calls, but still checks current host authority.
    replay_work = report["operations"][-1]["host_work"]
    assert replay_work["git_subprocess_run_calls"] > 0
    assert replay_work["checkpoint_handoff_checks"] > 0
    assert report["host_work_totals"]["checkpoint_handoff_checks"] == sum(
        operation["host_work"]["checkpoint_handoff_checks"] for operation in report["operations"]
    )
    assert report["host_work_totals"]["git_subprocess_run_calls"] >= sum(
        operation["host_work"]["git_subprocess_run_calls"] for operation in report["operations"]
    )
