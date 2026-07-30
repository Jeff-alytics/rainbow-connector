const RAD = Math.PI / 180;

export const SOURCE_MAX_AGE_MINUTES = Object.freeze({
  radar: 6,
  goesCloud: 20,
  goesIrradiance: 30,
  sunlightModel: 30,
  surfaceObservation: 60,
});

export function distanceKm(a, b) {
  const dLat = (Number(b.lat) - Number(a.lat)) * RAD;
  const dLon = (Number(b.lon) - Number(a.lon)) * RAD;
  const lat1 = Number(a.lat) * RAD;
  const lat2 = Number(b.lat) * RAD;
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371 * Math.asin(Math.sqrt(h));
}

export function sourceHealth(observedAt, maxAgeMinutes, now = Date.now()) {
  const observedMs = new Date(observedAt || 0).getTime();
  const ageMinutes = Number.isFinite(observedMs) ? (now - observedMs) / 60000 : Infinity;
  return {
    observedAt: observedAt || null,
    ageMinutes: Number.isFinite(ageMinutes) ? Number(ageMinutes.toFixed(1)) : null,
    maxAgeMinutes,
    fresh: Number.isFinite(ageMinutes) && ageMinutes >= -2 && ageMinutes <= maxAgeMinutes,
  };
}

export function evaluateRequiredSources(sources, now = Date.now()) {
  const health = {};
  for (const [name, limit] of Object.entries(SOURCE_MAX_AGE_MINUTES)) {
    const source = sources?.[name];
    health[name] = sourceHealth(source?.observedAt, source?.maxAgeMinutes ?? limit, now);
    health[name].required = source?.required ?? name !== "surfaceObservation";
    health[name].provider = source?.provider || null;
  }
  const blocking = Object.entries(health)
    .filter(([, value]) => value.required && !value.fresh)
    .map(([name]) => name);
  return { healthy: blocking.length === 0, blocking, sources: health };
}

export function strictGoCandidates(artifact) {
  const seen = new Set();
  return [...(artifact?.candidates || []), ...(artifact?.possibleCandidates || [])]
    .filter(candidate => candidate?.verdict === "go")
    .filter(candidate => Number.isFinite(candidate.lat) && Number.isFinite(candidate.lon))
    .filter(candidate => {
      const key = candidate.id || `${candidate.lat},${candidate.lon}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

function exceptionalEvidence(candidate) {
  const evidence = candidate.evidence || {};
  return candidate.sunlightConfidence === "strong"
    && Number(evidence.score) >= 85
    && Number(evidence.rainIntensity) >= 0.6
    && Number(evidence.observerRainIntensity) <= 0.1;
}

function strongGeometryFallback(candidate) {
  return candidate?.evidence?.selectionReason === "strong-radar-geometry-sunlight-uncertain";
}

export function applyCandidatePersistence(current, previous, options = {}) {
  const radiusKm = options.radiusKm ?? 35;
  const maxGapMinutes = options.maxGapMinutes ?? 12;
  const nowMs = new Date(current.generatedAt || 0).getTime();
  const previousMs = new Date(previous?.generatedAt || 0).getTime();
  const usablePrevious = Number.isFinite(nowMs) && Number.isFinite(previousMs)
    && nowMs >= previousMs
    && nowMs - previousMs <= maxGapMinutes * 60000;
  const oldCandidates = usablePrevious
    ? [
        ...(previous?.candidates || []),
        ...(previous?.possibleCandidates || []),
        ...(previous?.persistenceCandidates || []),
      ]
    : [];

  const annotate = candidate => {
    const match = oldCandidates.find(old => distanceKm(candidate, old) <= radiusKm);
    const priorCount = Number(match?.persistence?.scanCount || (match ? 1 : 0));
    const scanCount = match ? priorCount + 1 : 1;
    return {
      ...candidate,
      persistence: {
        scanCount,
        confirmed: scanCount >= 2,
        matchedPrevious: Boolean(match),
        radiusKm,
      },
    };
  };

  const annotatedGo = (current.candidates || []).map(annotate);
  const annotatedPossible = (current.possibleCandidates || []).map(annotate);
  const confirmed = [];
  const demoted = [];
  for (const candidate of annotatedGo) {
    if (candidate.persistence.confirmed || exceptionalEvidence(candidate)) confirmed.push(candidate);
    else demoted.push({ ...candidate, verdict: "go", confidence: "new", persistencePending: true });
  }

  const visiblePossible = [];
  const persistenceCandidates = [];
  for (const candidate of annotatedPossible) {
    if (strongGeometryFallback(candidate) && !candidate.persistence.confirmed) {
      persistenceCandidates.push(candidate);
    } else {
      visiblePossible.push(candidate);
    }
  }

  return {
    ...current,
    candidates: confirmed,
    possibleCandidates: [
      ...demoted,
      ...visiblePossible,
    ],
    persistenceCandidates,
    persistenceDiagnostics: {
      previousArtifactUsable: usablePrevious,
      inputGoCandidates: annotatedGo.length,
      confirmedGoCandidates: confirmed.length,
      pendingCandidates: demoted.length,
      pendingStrongGeometryCandidates: persistenceCandidates.length,
      radiusKm,
      maxGapMinutes,
    },
  };
}
