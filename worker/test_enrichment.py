import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from enrichment import batch_open_meteo, enrich_shortlist
from pipeline import notify_subscribers, publish_artifact


class FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, get_body=None, post_body=None):
        self.get_body = get_body
        self.post_body = post_body
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return FakeResponse(self.get_body)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return FakeResponse(self.post_body or {"ok": True})


class SequenceSession(FakeSession):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.responses.pop(0)


class EnrichmentTests(unittest.TestCase):
    def test_open_meteo_uses_instantaneous_dni(self):
        session = FakeSession(get_body={
            "current": {
                "time": "2026-07-26T12:00",
                "cloud_cover": 35,
                "direct_normal_irradiance_instant": 275,
            }
        })
        result = batch_open_meteo([{"lat": 35.0, "lon": -86.0}], session=session)
        self.assertEqual(result[0]["dniWm2"], 275)
        params = session.get_calls[0][1]["params"]
        self.assertEqual(params["current"], "cloud_cover,direct_normal_irradiance_instant")

    def test_open_meteo_failure_preserves_candidates_and_continues_batches(self):
        session = SequenceSession([
            FakeResponse({"reason": "quota"}, status_code=429),
            FakeResponse({"current": {
                "time": "2026-07-26T12:05",
                "cloud_cover": 20,
                "direct_normal_irradiance_instant": 260,
            }}),
        ])
        candidates = [
            {"lat": 35.0, "lon": -86.0},
            {"lat": 36.0, "lon": -87.0},
        ]
        result = batch_open_meteo(candidates, session=session, batch_size=1)
        self.assertEqual(len(result), 2)
        self.assertIsNone(result[0]["dniWm2"])
        self.assertEqual(result[0]["sunlightError"], "Open-Meteo batch failed (429)")
        self.assertEqual(result[1]["dniWm2"], 260)
        self.assertEqual(len(session.get_calls), 2)

    @patch("enrichment.sample_goes")
    def test_open_meteo_quota_failure_does_not_abort_artifact(self, sample):
        sample.return_value = (
            {
                "sunlightDecision": "unknown",
                "confidence": "unknown",
                "positiveSources": [],
                "negativeSources": [],
            },
            {},
        )
        radar = {
            "sourceHealth": {"radar": {"provider": "NOAA MRMS", "observedAt": "2026-07-28T23:00:00Z"}},
            "diagnostics": {},
            "shortlist": [{
                "lat": 39.3256,
                "lon": -76.7651,
                "rainLat": 39.295,
                "rainLon": -76.595,
                "rainDistanceKm": 15,
                "rainRateMmHr": 3.1,
                "observerRainRateMmHr": 0.0,
                "sunElevationDeg": 14.38,
                "antiSolarBearingDeg": 103.1,
                "radarScore": 94.4,
            }],
        }
        artifact = enrich_shortlist(
            radar,
            Path("unused"),
            session=SequenceSession([FakeResponse({"reason": "quota"}, status_code=429)]),
        )
        self.assertEqual(artifact["candidates"], [])
        self.assertEqual(len(artifact["possibleCandidates"]), 1)
        self.assertEqual(artifact["diagnostics"]["dniErrors"], 1)


    @patch("enrichment.sample_goes")
    def test_strict_candidate_requires_dni_and_goes_pair(self, sample):
        sample.return_value = (
            {
                "sunlightDecision": "go",
                "confidence": "strong",
                "positiveSources": ["goesDsrf", "goesAcmc"],
                "negativeSources": [],
            },
            {
                "goesDsrf": {"observedAt": "2026-07-26T11:58:00Z", "positive": True},
                "goesAcmc": {"observedAt": "2026-07-26T11:59:00Z", "positive": True},
            },
        )
        session = FakeSession(get_body={
            "current": {
                "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
                "cloud_cover": 40,
                "direct_normal_irradiance_instant": 280,
            }
        })
        radar = {
            "sourceHealth": {"radar": {"provider": "NOAA MRMS", "observedAt": "2026-07-26T12:00:00Z"}},
            "diagnostics": {},
            "shortlist": [{
                "lat": 35.1,
                "lon": -85.9,
                "rainLat": 35.0,
                "rainLon": -86.0,
                "rainDistanceKm": 15,
                "rainRateMmHr": 1.0,
                "observerRainRateMmHr": 0.0,
                "sunElevationDeg": 13.0,
                "antiSolarBearingDeg": 270.0,
                "radarScore": 88.0,
            }],
        }
        artifact = enrich_shortlist(radar, Path("unused"), session=session)
        self.assertEqual(len(artifact["candidates"]), 1)
        self.assertEqual(artifact["candidates"][0]["verdict"], "go")
        self.assertEqual(artifact["candidates"][0]["evidence"]["directNormalIrradianceWm2"], 280)
        self.assertEqual(artifact["sourceHealth"]["goesCloud"]["observedAt"], "2026-07-26T11:59:00Z")
        self.assertEqual(artifact["sourceHealth"]["goesIrradiance"]["observedAt"], "2026-07-26T11:58:00Z")
        self.assertEqual(artifact["sourceHealth"]["goesIrradiance"]["maxAgeMinutes"], 30)

    @patch("enrichment.sample_goes")
    def test_strong_geometry_can_become_possible_when_sunlight_is_blocked(self, sample):
        sample.return_value = (
            {
                "sunlightDecision": "blocked",
                "confidence": "strong-block",
                "positiveSources": [],
                "negativeSources": ["goesDsrf", "goesAcmc"],
            },
            {
                "goesDsrf": {"observedAt": "2026-07-28T23:00:00Z", "negative": True},
                "goesAcmc": {"observedAt": "2026-07-28T23:01:00Z", "negative": True},
            },
        )
        session = FakeSession(get_body={
            "current": {
                "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
                "cloud_cover": 100,
                "direct_normal_irradiance_instant": 0,
            }
        })
        radar = {
            "sourceHealth": {"radar": {"provider": "NOAA MRMS", "observedAt": "2026-07-28T23:00:00Z"}},
            "diagnostics": {},
            "shortlist": [{
                "lat": 39.3256,
                "lon": -76.7651,
                "rainLat": 39.295,
                "rainLon": -76.595,
                "rainDistanceKm": 15,
                "rainRateMmHr": 3.1,
                "observerRainRateMmHr": 0.0,
                "sunElevationDeg": 14.38,
                "antiSolarBearingDeg": 103.1,
                "radarScore": 94.4,
            }],
        }
        artifact = enrich_shortlist(radar, Path("unused"), session=session)
        self.assertEqual(len(artifact["candidates"]), 0)
        self.assertEqual(len(artifact["possibleCandidates"]), 1)
        possible = artifact["possibleCandidates"][0]
        self.assertEqual(possible["verdict"], "watch")
        self.assertEqual(
            possible["evidence"]["selectionReason"],
            "strong-radar-geometry-sunlight-uncertain",
        )
        self.assertEqual(artifact["diagnostics"]["strongGeometryFallbackChecked"], 1)

    @patch("enrichment.sample_goes")
    def test_weaker_geometry_does_not_bypass_sunlight(self, sample):
        session = FakeSession(get_body={
            "current": {
                "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
                "cloud_cover": 100,
                "direct_normal_irradiance_instant": 0,
            }
        })
        radar = {
            "sourceHealth": {"radar": {"provider": "NOAA MRMS", "observedAt": "2026-07-28T23:00:00Z"}},
            "diagnostics": {},
            "shortlist": [{
                "lat": 39.3,
                "lon": -76.6,
                "rainLat": 39.3,
                "rainLon": -76.5,
                "rainDistanceKm": 15,
                "rainRateMmHr": 3.1,
                "observerRainRateMmHr": 0.0,
                "sunElevationDeg": 14.0,
                "antiSolarBearingDeg": 103.0,
                "radarScore": 89.9,
            }],
        }
        decisions = []
        artifact = enrich_shortlist(radar, Path("unused"), session=session, decision_records=decisions)
        self.assertEqual(artifact["possibleCandidates"], [])
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["disposition"], "rejected")
        self.assertIn("geometry_fallback_radar_below_min", decisions[0]["decisionReasons"])
        sample.assert_not_called()

    def test_publisher_uses_bearer_secret(self):
        session = FakeSession(post_body={"ok": True, "storage": "redis"})
        result = publish_artifact(
            {"schemaVersion": 3, "candidates": []},
            url="https://example.test/api/satellite-candidates",
            secret="secret-value",
            session=session,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(session.post_calls[0][1]["headers"]["Authorization"], "Bearer secret-value")

    def test_notifier_sends_no_unconfirmed_artifact(self):
        session = FakeSession(post_body={"ok": True, "sent": 2})
        result = notify_subscribers(
            url="https://example.test/api/notify-alerts",
            secret="notify-secret",
            session=session,
        )
        self.assertEqual(result["sent"], 2)
        call = session.post_calls[0]
        self.assertEqual(call[1]["json"], {})
        self.assertEqual(call[1]["headers"]["Authorization"], "Bearer notify-secret")

    def test_notifier_is_optional_until_configured(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(notify_subscribers()["skipped"])


if __name__ == "__main__":
    unittest.main()
