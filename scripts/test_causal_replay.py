"""Protection tests for the research-only causal replay package."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from causal_replay import (CausalReplay, run_streams, streams_from_camera_free_artifact,
                           streams_from_pool_files, verify_prediction_package,
                           stream_pool_to_prediction_package, write_prediction_package)


def seed(lat=39.0, lon=-76.0, radar=96.0, support=None, sunlight=None):
    record = {
        "lat": lat, "lon": lon, "rainLat": lat, "rainLon": lon + 0.1,
        "rainDistanceKm": 8, "rainRateMmHr": 2.0, "observerRainRateMmHr": 0.0,
        "sunElevationDeg": 12.0, "radarScore": radar,
    }
    if support is not None:
        record["spatialSupport"] = support
    if sunlight is not None:
        record["sunlightV2"] = sunlight
    return record


class CausalLineageTests(unittest.TestCase):
    def test_future_scans_cannot_change_prior_predictions(self):
        replay = CausalReplay()
        records = [
            replay.process_scan("test", f"2026-08-03T00:{minute:02d}:00Z",
                                [seed(lat=39 + minute / 1000)])
            for minute in (0, 10, 20)
        ]
        frozen = deepcopy(records)
        replay.process_scan("test", "2026-08-03T00:30:00Z", [seed(lat=39.03, radar=100)])
        self.assertEqual(records, frozen)
        self.assertEqual(records[-1]["candidates"][0]["scanCount"], 3)
        self.assertEqual(records[-1]["candidates"][0]["causalPeakRadar"], 96)

    def test_expired_lineage_is_not_reused_but_remains_auditable(self):
        replay = CausalReplay({"maximumLineageGapMinutes": 30})
        first = replay.process_scan("test", "2026-08-03T00:00:00Z", [seed()])
        later = replay.process_scan("test", "2026-08-03T00:40:00Z", [seed()])
        self.assertNotEqual(
            first["candidates"][0]["lineageId"], later["candidates"][0]["lineageId"]
        )
        self.assertEqual(len(replay.lineages), 2)

    def test_persistence_saturates_at_five_scans(self):
        replay = CausalReplay()
        scores = []
        for minute in (0, 10, 20, 30, 40, 50):
            record = replay.process_scan("test", f"2026-08-03T00:{minute:02d}:00Z", [seed()])
            scores.append(record["candidates"][0]["models"]["boundedBlend"]["score"])
        self.assertEqual(scores[4], scores[5])
        self.assertTrue(record["candidates"][0]["causalFeatures"]["persistenceSaturated"])

    def test_missing_sunlight_is_neutral_and_confirmed_overcast_is_only_a_penalty(self):
        replay = CausalReplay()
        missing = replay.process_scan("missing", "2026-08-03T00:00:00Z", [seed()])
        unavailable = missing["candidates"][0]["causalFeatures"]
        self.assertEqual(unavailable["sunlightModifier"], 0)
        no_evidence = CausalReplay().process_scan("no-evidence", "2026-08-03T00:00:00Z", [
            seed(sunlight={"sunlightState": "overcast_supported", "evidencePresent": False})
        ])["candidates"][0]["causalFeatures"]
        self.assertEqual(no_evidence["sunlightModifier"], 0)
        evidenced_record = CausalReplay().process_scan("evidenced", "2026-08-03T00:00:00Z", [
            seed(sunlight={"sunlightState": "overcast_supported", "evidencePresent": True})
        ])["candidates"][0]
        evidenced = evidenced_record["causalFeatures"]
        self.assertEqual(evidenced["sunlightModifier"], -10)
        self.assertTrue(evidenced_record["models"]["boundedBlend"]["eligible"])

    def test_isolated_radar_support_blocks_validity_but_unknown_support_does_not(self):
        unknown = CausalReplay().process_scan("unknown", "2026-08-03T00:00:00Z", [seed()])
        self.assertTrue(unknown["candidates"][0]["validity"]["eligible"])
        isolated = CausalReplay().process_scan("isolated", "2026-08-03T00:00:00Z", [
            seed(support={"isolatedPixel": True, "wetNeighborCount": 0})
        ])
        self.assertFalse(isolated["candidates"][0]["validity"]["eligible"])
        self.assertIn("radar_spatial_support", isolated["candidates"][0]["validity"]["reasons"])
        supported = CausalReplay().process_scan("supported", "2026-08-03T00:00:00Z", [
            seed(support={"isolatedPixel": False, "adjacentWetCells": 5})
        ])
        self.assertEqual(supported["candidates"][0]["spatialSupportState"], "supported")
        self.assertTrue(supported["candidates"][0]["validity"]["eligible"])

    def test_review_policy_locks_primary_control_and_blind_first_assignments(self):
        replay = CausalReplay({
            "primaryReviewMaximumRank": 1,
            "lowerRankControlMinimumRank": 2,
            "lowerRankControlMaximumRank": 2,
            "lowerRankControlFraction": 1.0,
            "blindFirstFraction": 1.0,
        })
        first = [seed(lat=39.0, radar=99), seed(lat=40.0, radar=80)]
        replay.process_scan("test", "2026-08-03T00:00:00Z", first)
        second = replay.process_scan("test", "2026-08-03T00:10:00Z", first)
        lanes = {item["reviewPolicy"]["lane"] for item in second["candidates"]}
        self.assertEqual(lanes, {"primary", "lower_rank_control"})
        self.assertTrue(all(item["reviewPolicy"]["blindFirst"] for item in second["candidates"]))

    def test_budget_gate_waits_for_bounded_causal_evidence(self):
        replay = CausalReplay({
            "budgetGateMinimumScans": 2,
            "budgetGateMinimumPeakRadar": 95,
            "budgetGateMinimumCurrentRadar": 70,
        })
        seeds = [seed(lat=39.0, radar=99), seed(lat=40.0, radar=98)]
        first = replay.process_scan("test", "2026-08-03T00:00:00Z", seeds)
        self.assertFalse(any(item["models"]["budgetGate"]["eligible"] for item in first["candidates"]))
        second = replay.process_scan("test", "2026-08-03T00:10:00Z", seeds)
        self.assertTrue(all(item["models"]["budgetGate"]["eligible"] for item in second["candidates"]))


class InputAndIntegrityTests(unittest.TestCase):
    def test_streaming_pool_writer_is_hashed_and_chronological(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "shard-0.jsonl", root / "shard-1.jsonl"
            first.write_text(json.dumps({
                "observedAt": "2026-08-03T00:10:00Z", "status": "ok",
                "allSeeds": [seed(lat=39.01)],
            }) + chr(10))
            second.write_text(json.dumps({
                "observedAt": "2026-08-03T00:00:00Z", "status": "ok",
                "allSeeds": [seed(lat=39.0)],
            }) + chr(10))
            output = root / "predictions"
            manifest, summary = stream_pool_to_prediction_package(
                [first, second], output
            )
            self.assertTrue(manifest["streaming"])
            self.assertEqual(summary["scans"], 2)
            self.assertEqual(summary["lineages"], 1)
            self.assertTrue(verify_prediction_package(output)["ok"])
            lines = (output / "predictions-2026-08-03.jsonl").read_text().splitlines()
            self.assertEqual(
                json.loads(lines[0])["prediction"]["scanTime"], "2026-08-03T00:00:00Z"
            )

    def test_legacy_camera_filtered_pool_requires_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pool.jsonl"
            path.write_text(json.dumps({"observedAt": "2026-08-03T00:00:00Z",
                                        "status": "ok", "matched": [seed()]}) + chr(10))
            with self.assertRaisesRegex(ValueError, "does not retain allSeeds"):
                streams_from_pool_files([path])
            streams = streams_from_pool_files([path], allow_camera_filtered=True)
            self.assertEqual(streams[0]["inputScope"], "camera_filtered_legacy")

    def test_prediction_hash_chain_detects_tampering(self):
        streams = [{"streamId": "test", "inputScope": "camera_independent_test",
                    "scans": [{"scanTime": "2026-08-03T00:00:00Z", "seeds": [seed()]}]}]
        result = run_streams(streams)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            source.write_text("{}")
            output = Path(directory) / "out"
            write_prediction_package(result, output, [source])
            self.assertTrue(verify_prediction_package(output)["ok"])
            prediction = next(output.glob("predictions-*.jsonl"))
            prediction.write_text(prediction.read_text().replace('"radarScore":96.0', '"radarScore":95.0'))
            verification = verify_prediction_package(output)
            self.assertFalse(verification["ok"])
            self.assertTrue(any("content hash mismatch" in error for error in verification["errors"]))

    def test_prediction_hash_chain_reports_corrupt_json_cleanly(self):
        streams = [{"streamId": "test", "inputScope": "camera_independent_test",
                    "scans": [{"scanTime": "2026-08-03T00:00:00Z", "seeds": [seed()]}]}]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            source.write_text("{}")
            output = Path(directory) / "out"
            write_prediction_package(run_streams(streams), output, [source])
            prediction = next(output.glob("predictions-*.jsonl"))
            prediction.write_text("{not-json}\n")
            verification = verify_prediction_package(output)
            self.assertFalse(verification["ok"])
            self.assertTrue(any("invalid prediction record" in error for error in verification["errors"]))

    def test_prediction_package_detects_source_tampering(self):
        streams = [{"streamId": "test", "inputScope": "camera_independent_test",
                    "scans": [{"scanTime": "2026-08-03T00:00:00Z", "seeds": [seed()]}]}]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            source.write_text("{}")
            output = Path(directory) / "out"
            write_prediction_package(run_streams(streams), output, [source])
            source.write_text('{"changed":true}')
            verification = verify_prediction_package(output)
            self.assertFalse(verification["ok"])
            self.assertTrue(any("source hash mismatch" in error for error in verification["errors"]))

    def test_retained_frozen_fixture_has_causal_budget_crossings_before_windows(self):
        artifact_path = ROOT / "validation" / "frozen-case-pool-check" / "camera-free-seeds.json"
        payload = json.loads(artifact_path.read_text(encoding="utf-8-sig"))
        result = run_streams(streams_from_camera_free_artifact(payload))
        summaries = {item["streamId"]: item for item in result["streams"]}
        expected = {
            "baltimore-dundalk-20260728": ("2026-07-28T23:00:00Z", 25.0),
            "colorado-multicamera-20260730": ("2026-07-30T00:50:00Z", 30.0),
            "utah-ogden-bear-river-20260730": ("2026-07-30T01:20:00Z", 60.0),
        }
        for case_id, (first_at, lead) in expected.items():
            model = summaries[case_id]["models"]["budgetGate"]
            self.assertEqual(model["firstEligibleAt"], first_at)
            self.assertEqual(model["leadMinutesToWindowStart"], lead)


if __name__ == "__main__":
    unittest.main()
