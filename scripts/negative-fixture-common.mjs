function finite(value) {
  if (value == null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function timeMs(value) {
  const parsed = new Date(value || 0).getTime();
  return Number.isFinite(parsed) ? parsed : null;
}

export function frameSummary(event) {
  const target = timeMs(event?.representative?.detectedAt || event?.firstSeenAt);
  const times = [...new Set((event?.evidence?.frames || [])
    .map(frame => frame?.observedAt).filter(Boolean))].sort();
  const offsets = target == null ? [] : times.map(value => Math.abs(timeMs(value) - target) / 60000).filter(Number.isFinite);
  return {
    distinctFrames: times.length,
    observedAt: times,
    nearestFrameOffsetMinutes: offsets.length ? Math.round(Math.min(...offsets) * 10) / 10 : null,
    frameSpanMinutes: times.length > 1 ? Math.round((timeMs(times.at(-1)) - timeMs(times[0])) / 6000) / 10 : 0,
  };
}

export function negativeEvidence(event) {
  const camera = event?.evidence?.camera || {};
  const source = String(event?.evidence?.source || "");
  const frames = frameSummary(event);
  const distance = finite(camera.distanceKm);
  const bearing = finite(camera.bearingDifference);
  const skyPct = finite(camera.horizonSkyPct);
  const visibleBowFraction = finite(camera.visibleBowFraction);
  const viewQuality = String(camera.viewQuality || "").toLowerCase();
  const knownBearing = bearing != null;
  const trustedFaaView = source === "FAA WeatherCam";

  const direction = visibleBowFraction != null && visibleBowFraction < 0.5 ? 0
    : !knownBearing ? 0 : bearing <= 15 ? 1 : bearing <= 30 ? 0.75 : 0.25;
  const proximity = distance == null ? 0 : distance <= 25 ? 1 : distance <= 35 ? 0.8 : 0.25;
  const timing = frames.nearestFrameOffsetMinutes == null ? 0
    : frames.nearestFrameOffsetMinutes <= 5 ? 1
      : frames.nearestFrameOffsetMinutes <= 8 ? 0.7
        : frames.nearestFrameOffsetMinutes <= 15 ? 0.3 : 0.1;
  const coverage = frames.distinctFrames >= 5 ? 1 : frames.distinctFrames === 4 ? 0.9
    : frames.distinctFrames === 3 ? 0.75 : frames.distinctFrames === 2 ? 0.5
      : frames.distinctFrames === 1 ? 0.17 : 0;
  const sky = ["limited", "unreviewed"].includes(viewQuality) ? 0.2
    : skyPct != null ? Math.min(1, skyPct / 30)
      : (viewQuality === "usable" || trustedFaaView) ? 0.85 : 0.5;

  const weight = knownBearing
    ? Math.round(direction * proximity * timing * coverage * sky * 1000) / 1000
    : 0;
  const adequateSky = sky >= 0.67;
  const eligibilityReasons = [];
  if (!knownBearing) eligibilityReasons.push("camera_bearing_unknown");
  if (visibleBowFraction != null && visibleBowFraction < 0.5) eligibilityReasons.push("less_than_half_of_visible_bow_in_view");
  if (distance == null || distance > 35) eligibilityReasons.push("camera_too_distant_or_unknown");
  if (bearing != null && bearing > 30) eligibilityReasons.push("bow_direction_outside_strong_review_sector");
  if (frames.distinctFrames < 2) eligibilityReasons.push("single_or_missing_frame");
  if (frames.nearestFrameOffsetMinutes == null || frames.nearestFrameOffsetMinutes > 8) eligibilityReasons.push("frames_not_timely");
  if (!adequateSky) eligibilityReasons.push("sky_view_not_verified_adequate");
  if (["limited", "unreviewed"].includes(viewQuality)) eligibilityReasons.push("view_quality_not_calibration_grade");
  if (weight < 0.5) eligibilityReasons.push("negative_evidence_weight_below_0_5");

  return {
    methodVersion: "negative-evidence-v2-bow-coverage",
    weight,
    calibrationEligible: eligibilityReasons.length === 0,
    eligibilityReasons: [...new Set(eligibilityReasons)],
    components: { direction, proximity, timing, coverage, sky },
    inputs: {
      distanceKm: distance,
      bearingDifferenceDeg: bearing,
      visibleBowFraction,
      viewQuality: viewQuality || null,
      horizonSkyPct: skyPct,
      source: source || null,
      ...frames,
    },
  };
}

export function compactNegativeEvent(event) {
  const rep = event?.representative || {};
  const evidence = rep.evidence || {};
  const camera = event?.evidence?.camera || {};
  return {
    eventId: event.id,
    label: "no_rainbow",
    candidateClass: event.candidateClass || (event.candidateType === "live_possible" ? "POSSIBLE" : "GO"),
    candidateType: event.candidateType || null,
    firstSeenAt: event.firstSeenAt || null,
    lastSeenAt: event.lastSeenAt || null,
    reviewedAt: event?.review?.reviewedAt || null,
    scanCount: finite(event.scanCount),
    peakScore: finite(event.peakScore),
    observer: { lat: finite(rep.lat), lon: finite(rep.lon) },
    meteorology: {
      sunElevationDeg: finite(evidence.sunElevationDeg),
      directNormalIrradianceWm2: finite(evidence.directNormalIrradianceWm2),
      cloudCoverPct: finite(evidence.cloudCoverPct),
      rainIntensity: finite(evidence.rainIntensity),
      observerRainIntensity: finite(evidence.observerRainIntensity),
      rainPoint: evidence.rainPoint || null,
      goes: evidence.goes || null,
    },
    camera: {
      source: event?.evidence?.source || null,
      name: camera.name || null,
      state: camera.state || null,
      direction: camera.direction || null,
      distanceKm: finite(camera.distanceKm),
      bearingDifferenceDeg: finite(camera.bearingDifference),
      viewQuality: camera.viewQuality || null,
      horizonSkyPct: finite(camera.horizonSkyPct),
      visibleBowFraction: finite(camera.visibleBowFraction),
    },
    negativeEvidence: negativeEvidence(event),
  };
}
