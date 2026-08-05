import unittest

from v5_review_feed import chunk_v5_review_feed


class V5ReviewFeedTests(unittest.TestCase):
    def test_oversized_feed_is_losslessly_chunked_with_retry_safe_ids(self):
        items = [{"familyEventId": f"family-{index}", "payload": "x" * 600} for index in range(12)]
        feed = {"schemaVersion": "v5-candidate-scan.v1", "scanTime": "2026-08-05T12:00:00Z",
                "predictionManifestSha256": "frozen-hash", "acquisitionEnabled": False, "items": items}
        chunks = chunk_v5_review_feed(feed, maximum_bytes=2200)
        self.assertGreater(len(chunks), 1)
        self.assertEqual([item for chunk in chunks for item in chunk["items"]], items)
        self.assertEqual({chunk["sourcePredictionManifestSha256"] for chunk in chunks}, {"frozen-hash"})
        self.assertEqual(len({chunk["predictionManifestSha256"] for chunk in chunks}), len(chunks))
        self.assertTrue(all(chunk["feedChunk"]["total"] == len(chunks) for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
