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
