"""Contracts for camera-independent historical pool construction."""

from __future__ import annotations

import sys
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "worker", ROOT / "api"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from build_historical_candidate_pool import completed_frames, conus_eligible_frame_times


class CameraIndependentPoolTests(unittest.TestCase):
    def test_frame_selection_does_not_require_a_camera_catalog(self):
        start = datetime(2026, 7, 3, tzinfo=timezone.utc)
        frames = conus_eligible_frame_times(start, start.replace(day=4))
        self.assertTrue(frames)
        self.assertTrue(all(frame.minute % 10 == 0 for frame in frames))
        self.assertLess(len(frames), 24 * 6)

    def test_deterministic_shard_partition_is_complete_and_disjoint(self):
        start = datetime(2026, 7, 3, tzinfo=timezone.utc)
        frames = conus_eligible_frame_times(start, start.replace(day=4))
        shards = [set(frames[index::4]) for index in range(4)]
        self.assertEqual(set().union(*shards), set(frames))
        self.assertEqual(sum(len(shard) for shard in shards), len(frames))

    def test_resume_retries_transient_errors_but_not_terminal_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pool.jsonl"
            rows = [
                {"observedAt": "2026-07-03T00:00:00Z", "status": "ok"},
                {"observedAt": "2026-07-03T00:10:00Z", "status": "missing_mrms_object"},
                {"observedAt": "2026-07-03T00:20:00Z", "status": "error: timeout"},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            self.assertEqual(completed_frames(path), {
                "2026-07-03T00:00:00Z", "2026-07-03T00:10:00Z",
            })


if __name__ == "__main__":
    unittest.main()
