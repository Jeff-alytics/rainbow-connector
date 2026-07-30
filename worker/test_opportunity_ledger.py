import math
import unittest
from datetime import datetime, timezone

import numpy as np

from detector_core import offset, solar_position
from opportunity_ledger import (
    SCHEMA_VERSION, _representatives, build_opportunity_ledger, camera_evidence_scope,
    link_lineage, rain_components, swath_contains,
)
from rain_footprint import build_sidecar


class OpportunityLedgerTests(unittest.TestCase):
    def test_connected_components_ignore_rate_tier_boundaries(self):
        sidecar = {
            "observedAt": "2026-07-30T00:30:00Z",
            "runs": [[0, 0, 1, 1], [0, 2, 3, 3], [1, 3, 4, 2], [5, 9, 9, 1]],
        }
        components = rain_components(sidecar)
        self.assertEqual(len(components), 2)
        self.assertEqual(sorted(item["cellCount"] for item in components), [1, 6])
        self.assertEqual(max(item["maximumTier"] for item in components), 3)

    def test_component_ids_are_deterministic(self):
        sidecar = {"observedAt": "2026-07-30T00:30:00Z", "runs": [[1, 2, 3, 1], [2, 3, 4, 2]]}
        self.assertEqual(rain_components(sidecar), rain_components(sidecar))

    def test_lineage_records_splits_without_changing_stable_event(self):
        parent = {"componentId": "old", "eventId": "storm", "runs": [[0, 0, 4, 1]], "bounds": {"rowMin": 0, "rowMax": 0, "columnMin": 0, "columnMax": 4}}
        children = [
            {"componentId": "left", "eventId": "new-left", "runs": [[0, 0, 1, 1]], "bounds": {"rowMin": 0, "rowMax": 0, "columnMin": 0, "columnMax": 1}},
            {"componentId": "right", "eventId": "new-right", "runs": [[0, 3, 4, 1]], "bounds": {"rowMin": 0, "rowMax": 0, "columnMin": 3, "columnMax": 4}},
        ]
        current = {"rainEvents": children, "opportunities": [
            {"rainComponentId": "left", "eventId": "new-left"},
            {"rainComponentId": "right", "eventId": "new-right"},
        ]}
        linked = link_lineage({"rainEvents": [parent]}, current, maximum_motion_cells=0)
        self.assertEqual({item["eventId"] for item in linked["rainEvents"]}, {"storm"})
        self.assertEqual([edge["kind"] for edge in linked["lineageEdges"]], ["split", "split"])
        self.assertEqual({edge["linkBasis"] for edge in linked["lineageEdges"]}, {"native_cell_overlap"})
        self.assertEqual([edge["overlapCells"] for edge in linked["lineageEdges"]], [2, 2])
        self.assertTrue(all(edge["isPrimary"] for edge in linked["lineageEdges"]))
        self.assertEqual({item["eventId"] for item in linked["opportunities"]}, {"storm"})

    def test_lineage_marks_motion_fallback_separately_from_overlap(self):
        parent = {"componentId": "old", "eventId": "storm", "runs": [[0, 0, 1, 1]], "bounds": {"rowMin": 0, "rowMax": 0, "columnMin": 0, "columnMax": 1}}
        child = {"componentId": "new", "eventId": "new-storm", "runs": [[0, 3, 4, 1]], "bounds": {"rowMin": 0, "rowMax": 0, "columnMin": 3, "columnMax": 4}}
        current = {"rainEvents": [child], "opportunities": [{"rainComponentId": "new", "eventId": "new-storm"}]}
        linked = link_lineage({"rainEvents": [parent]}, current, maximum_motion_cells=2)
        edge = linked["lineageEdges"][0]
        self.assertEqual(edge["linkBasis"], "nearest_motion_fallback")
        self.assertEqual(edge["overlapCells"], 0)
        self.assertEqual(edge["boundingBoxGapCells"], 2)

    def test_bounding_box_containment_cannot_join_distant_cells(self):
        old = {"componentId": "crescent", "eventId": "old-storm",
               "runs": [[0, 0, 100, 1], [100, 0, 100, 1]],
               "bounds": {"rowMin": 0, "rowMax": 100, "columnMin": 0, "columnMax": 100},
               "cellCount": 102}
        fresh = {"componentId": "fresh", "eventId": "fresh-storm", "runs": [[50, 50, 50, 1]],
                 "bounds": {"rowMin": 50, "rowMax": 50, "columnMin": 50, "columnMax": 50},
                 "cellCount": 1}
        current = {"rainEvents": [fresh], "opportunities": [{"rainComponentId": "fresh", "eventId": "fresh-storm"}]}
        linked = link_lineage({"rainEvents": [old]}, current, maximum_motion_cells=12)
        self.assertEqual(linked["rainEvents"][0]["eventId"], "fresh-storm")
        self.assertEqual(linked["lineageEdges"], [])

    def test_equal_overlap_prefers_dominant_parent_not_event_id(self):
        small = {"componentId": "small", "eventId": "a-speck", "runs": [[0, 0, 0, 1]],
                 "bounds": {"rowMin": 0, "rowMax": 0, "columnMin": 0, "columnMax": 0}, "cellCount": 1}
        large = {"componentId": "large", "eventId": "z-system", "runs": [[0, 0, 99, 1]],
                 "bounds": {"rowMin": 0, "rowMax": 0, "columnMin": 0, "columnMax": 99}, "cellCount": 100}
        merged = {"componentId": "merged", "eventId": "new", "runs": [[0, 0, 0, 1]],
                  "bounds": {"rowMin": 0, "rowMax": 0, "columnMin": 0, "columnMax": 0}, "cellCount": 1}
        current = {"rainEvents": [merged], "opportunities": [{"rainComponentId": "merged", "eventId": "new"}]}
        linked = link_lineage({"rainEvents": [small, large]}, current)
        self.assertEqual(linked["rainEvents"][0]["eventId"], "z-system")
        self.assertEqual(linked["rainEvents"][0]["lineageScanCount"], 2)

    def test_merge_identity_follows_dominant_overlap(self):
        small = {"componentId": "small", "eventId": "a-small", "runs": [[0, 0, 0, 1]], "bounds": {"rowMin": 0, "rowMax": 0, "columnMin": 0, "columnMax": 0}}
        large = {"componentId": "large", "eventId": "z-large", "runs": [[0, 1, 4, 1]], "bounds": {"rowMin": 0, "rowMax": 0, "columnMin": 1, "columnMax": 4}}
        merged = {"componentId": "merged", "eventId": "new", "runs": [[0, 0, 4, 1]], "bounds": {"rowMin": 0, "rowMax": 0, "columnMin": 0, "columnMax": 4}}
        current = {"rainEvents": [merged], "opportunities": [{"rainComponentId": "merged", "eventId": "new"}]}
        linked = link_lineage({"rainEvents": [small, large]}, current)
        self.assertEqual(linked["rainEvents"][0]["eventId"], "z-large")
        primary = [edge for edge in linked["lineageEdges"] if edge["isPrimary"]]
        self.assertEqual(len(primary), 1)
        self.assertEqual(primary[0]["fromComponentId"], "large")

    def test_swath_retains_physical_observer_independent_of_representatives(self):
        lats = np.array([41.02, 41.01, 41.00])
        lons = np.array([-94.02, -94.01, -94.00])
        rates = np.zeros((3, 3)); rates[1, 1] = 1.5
        observed = datetime(2026, 7, 30, 0, 30, tzinfo=timezone.utc)
        sidecar = build_sidecar(lats, lons, rates, observed, "synthetic-iowa")
        ledger = build_opportunity_ledger(sidecar)
        self.assertEqual(ledger["schemaVersion"], SCHEMA_VERSION)
        self.assertEqual(ledger["stats"]["rainEvents"], 1)
        opportunity = ledger["opportunities"][0]
        _, sun_bearing = solar_position(observed, 41.01, -94.01)
        expected_lat, expected_lon = offset(41.01, -94.01, 15, sun_bearing)
        inside, distance = swath_contains(opportunity["observerSwath"], expected_lat, expected_lon)
        self.assertTrue(inside, distance)
        self.assertGreater(opportunity["observerSwath"]["cellCount"], 0)
        self.assertGreater(len(opportunity["observerSwath"]["representativeCandidates"]), 0)

    def test_camera_distance_is_asymmetric_by_evidence_direction(self):
        self.assertEqual(camera_evidence_scope(39.9, "no_rainbow"), "candidate")
        self.assertEqual(camera_evidence_scope(60.8, "rainbow"), "event")
        self.assertEqual(camera_evidence_scope(60.8, "no_rainbow"), "ignored")

    def test_representative_spatial_index_preserves_minimum_spacing(self):
        cells = {(row, column) for row in range(40) for column in range(40)}
        points = _representatives(cells, spacing_km=15)
        for index, left in enumerate(points):
            for right in points[index + 1:]:
                distance = math.hypot(
                    (left["lat"] - right["lat"]) * 111,
                    (left["lon"] - right["lon"]) * 111 * math.cos(math.radians(left["lat"])),
                )
                self.assertGreaterEqual(distance, 14.95)

    def test_ledger_records_no_camera_or_publication_inputs(self):
        lats = np.array([41.01, 41.00]); lons = np.array([-94.01, -94.00])
        rates = np.array([[0.0, 1.0], [0.0, 0.0]])
        sidecar = build_sidecar(lats, lons, rates, datetime(2026, 7, 30, 0, 30, tzinfo=timezone.utc), "source")
        ledger = build_opportunity_ledger(sidecar)
        text = str(ledger).lower()
        self.assertNotIn("camera", text)
        self.assertNotIn("email", text)
        self.assertNotIn("subscriber", text)

    def test_midday_rain_event_is_retained_without_false_bow_opportunity(self):
        lats = np.array([41.01, 41.00]); lons = np.array([-94.01, -94.00])
        rates = np.array([[0.0, 1.0], [0.0, 0.0]])
        sidecar = build_sidecar(lats, lons, rates, datetime(2026, 7, 30, 18, 30, tzinfo=timezone.utc), "source")
        ledger = build_opportunity_ledger(sidecar)
        self.assertEqual(ledger["stats"]["rainEvents"], 1)
        self.assertEqual(ledger["stats"]["opportunities"], 0)


if __name__ == "__main__":
    unittest.main()
