"""Select geometry-first candidates for private human review only."""
from __future__ import annotations
import math

RULE_VERSION = "geometry-first-review-2026-07-v1"
MAX_PER_SCAN = 6
THRESHOLDS = {
    "minimumRadarScore": 50.0, "minimumTier2RainArcSpanDeg": 30.0,
    "maximumRainDistanceKm": 40.0, "maximumObserverRainRateMmHr": 0.05,
    "minimumSunElevationDeg": -0.833, "maximumSunElevationDeg": 30.0,
    "blockedSunlightState": "overcast_supported", "maximumCandidatesPerScan": MAX_PER_SCAN,
}
STATE_RANK = {"sunlit_supported": 3, "sunlight_plausible": 2, "unresolved": 1}


def finite(value):
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


def tier2_span(rain):
    tier = ((rain.get("antiSolarRainSpanByTier") or {}).get("tier2Plus") or {})
    return finite(tier.get("arcSpanDeg"))


def distance_km(a_lat, a_lon, b_lat, b_lon):
    dlat, dlon = math.radians(b_lat-a_lat), math.radians(b_lon-a_lon)
    value = math.sin(dlat/2)**2 + math.cos(math.radians(a_lat))*math.cos(math.radians(b_lat))*math.sin(dlon/2)**2
    return 2*6371*math.asin(math.sqrt(value))


def angle_difference(a, b):
    return abs((a-b+540) % 360-180)


def camera_matches(record, catalog, maximum_distance_km=40):
    features = record.get("features") or {}
    observer, geometry = features.get("observer") or {}, features.get("geometry") or {}
    lat, lon = finite(observer.get("lat")), finite(observer.get("lon"))
    elevation = finite(geometry.get("apparentSunElevationDeg"))
    if elevation is None:
        elevation = finite(geometry.get("sunElevationDeg"))
    anti = finite(geometry.get("antiSolarBearingDeg"))
    if None in (lat, lon, elevation, anti) or not -0.833 <= elevation < 42: return []
    ratio = math.cos(math.radians(42))/math.cos(math.radians(elevation))
    half_extent = math.degrees(math.acos(max(-1, min(1, ratio))))
    bow_width, matches = 2*half_extent, []
    for site in catalog or []:
        distance = distance_km(lat, lon, float(site["lat"]), float(site["lon"]))
        if distance > maximum_distance_km: continue
        for camera in site.get("cameras") or []:
            # For intervals narrower than 180°, overlap equals the amount by which
            # their half-widths exceed the circular center separation.
            bearing = finite(camera.get("bearing"))
            fov = finite(camera.get("mapWedgeAngle"))
            if bearing is None:
                continue
            if fov is None:
                fov = 45.0
            if fov <= 0:
                continue
            overlap = max(0.0, half_extent + fov/2 - angle_difference(bearing, anti))
            overlap = min(overlap, bow_width, fov)
            if overlap < 3: continue
            matches.append({"siteId": site.get("id"), "cameraId": camera.get("id"), "name": site.get("name"),
                            "direction": camera.get("direction"), "distanceKm": round(distance, 1),
                            "visibleBowFraction": round(overlap/bow_width, 3), "bowArcOverlapDeg": round(overlap, 1)})
    return sorted(matches, key=lambda item: (item["distanceKm"], -item["visibleBowFraction"]))[:3]


def eligibility(record):
    if record.get("disposition") != "rejected":
        return False, ["already_selected_by_operational_rule"]
    features = record.get("features") or {}
    geometry, rain = features.get("geometry") or {}, features.get("rain") or {}
    sunlight = features.get("sunlightV2") or {}
    score, elevation = finite(geometry.get("radarScore")), finite(geometry.get("sunElevationDeg"))
    distance, observer_rain = finite(rain.get("distanceKm")), finite(rain.get("observerRateMmHr"))
    span, state, failures = tier2_span(rain), sunlight.get("sunlightState"), []
    if score is None or score < THRESHOLDS["minimumRadarScore"]:
        failures.append("radar_score_below_review_min")
    if elevation is None or not THRESHOLDS["minimumSunElevationDeg"] <= elevation <= THRESHOLDS["maximumSunElevationDeg"]:
        failures.append("sun_outside_refracted_review_band")
    if distance is None or distance > THRESHOLDS["maximumRainDistanceKm"]:
        failures.append("rain_too_distant_for_review")
    if observer_rain is None or observer_rain > THRESHOLDS["maximumObserverRainRateMmHr"]:
        failures.append("observer_not_dry")
    if span is None or span < THRESHOLDS["minimumTier2RainArcSpanDeg"]:
        failures.append("tier2_rain_arc_too_narrow")
    if state == "overcast_supported":
        failures.append("v2_affirmative_overcast")
    elif state not in STATE_RANK:
        failures.append("v2_state_unavailable")
    return not failures, failures


def rank_key(record):
    features = record.get("features") or {}
    geometry, rain = features.get("geometry") or {}, features.get("rain") or {}
    state = (features.get("sunlightV2") or {}).get("sunlightState")
    return (STATE_RANK.get(state, 0), min(100.0, tier2_span(rain) or 0.0),
            finite(geometry.get("radarScore")) or 0.0, -(finite(rain.get("distanceKm")) or 40.0))


def select_research_candidates(records, maximum=MAX_PER_SCAN, camera_catalog=None):
    eligible = []
    for record in records:
        if not eligibility(record)[0]: continue
        matches = camera_matches(record, camera_catalog) if camera_catalog is not None else []
        if camera_catalog is not None and not matches: continue
        eligible.append((record, matches))
    # A nearby camera is more likely to produce interpretable evidence than a
    # distant camera with a marginally more centered field of view.
    eligible.sort(key=lambda item: (-(item[1][0]["distanceKm"] if item[1] else 999),
                                    (item[1][0]["visibleBowFraction"] if item[1] else 0),
                                    *rank_key(item[0])), reverse=True)
    selected = eligible[:max(0, min(int(maximum), MAX_PER_SCAN))]
    for rank, (record, matches) in enumerate(selected, 1):
        original_disposition = record.get("originalDisposition") or record.get("disposition")
        record["originalDisposition"] = original_disposition
        record["disposition"] = "selected_research_possible"
        record["decisionStage"] = "research_review_selection"
        record["decisionReasons"] = [*(record.get("decisionReasons") or []), "selected_geometry_first_review_v1"]
        record.setdefault("features", {})["researchReview"] = {
            "ruleVersion": RULE_VERSION, "rankWithinScan": rank, "reviewOnly": True,
            "publicClassificationChanged": False, "thresholdSnapshot": dict(THRESHOLDS),
            "originalDisposition": original_disposition,
            "cameraMatches": matches,
        }
    return {"ruleVersion": RULE_VERSION, "eligible": len(eligible), "selected": len(selected),
            "selectedCandidateIds": [record.get("candidateId") for record, _ in selected],
            "maximumPerScan": MAX_PER_SCAN}
