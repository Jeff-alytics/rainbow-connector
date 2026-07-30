import json
from pathlib import Path

from audit_b import DEFAULT_FIXTURE, analyze_event, run


def test_fixture_is_frozen_and_has_expected_positive_inventory():
    fixture = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["contentSha256"]
    assert len(fixture["events"]) == 19
    assert sum(event["productionScope"] == "conus_recall" for event in fixture["events"]) == 16
    assert sum(event["productionScope"] == "conus_recall" and event["evidenceClass"] == "camera_confirmed" for event in fixture["events"]) == 13
    assert len({event["id"] for event in fixture["events"]}) == len(fixture["events"])


def test_region_only_idaho_is_not_given_false_point_precision():
    fixture = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
    event = next(event for event in fixture["events"] if event["id"] == "social-idaho-20260728")
    row = analyze_event(event, {})
    assert row["pointScorable"] is False
    assert row["currentPolicyReplay"] == "not_scorable"


def test_report_runs_without_optional_historical_goes(tmp_path: Path):
    report = run(DEFAULT_FIXTURE, tmp_path / "missing-goes.json", tmp_path / "missing-history.json")
    assert report["summary"]["conusCameraConfirmedEvents"] == 13
    assert report["summary"]["pointScorableConusRecallEvents"] == 16
    assert report["historicalGoesPresent"] is False
    assert "false-positive rate" in report["limitations"][0]
