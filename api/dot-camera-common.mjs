import { distKm } from "./alert-common.mjs";

const DELDOT_QUERY = "https://enterprise.firstmaptest.delaware.gov/arcgis/rest/services/Transportation/DE_TMC_Traffic_Feeds/MapServer/1/query";
const CALTRANS_DISTRICT_URL = district => `https://cwwp2.dot.ca.gov/data/d${district}/cctv/cctvStatusD${String(district).padStart(2, "0")}.json`;
const IOWA_QUERY = "https://services.arcgis.com/8lRhdTsQyJpO52F1/arcgis/rest/services/Traffic_Cameras_View/FeatureServer/0/query";
const OHGO_CAMERAS = "https://publicapi.ohgo.com/api/v1/cameras?page-all=true";
const WSDOT_CAMERAS = "https://www.wsdot.wa.gov/Traffic/api/HighwayCameras/HighwayCamerasREST.svc/GetCamerasAsJson";
const CATALOG_TTL_MS = 30 * 60 * 1000;
let deldotCache = null;
let deldotCacheAt = 0;
let caltransCache = null;
let caltransCacheAt = 0;
let iowaCache = null;
let iowaCacheAt = 0;
let ohioCache = null;
let ohioCacheAt = 0;
let wsdotCache = null;
let wsdotCacheAt = 0;

const FETCH_HEADERS = { "User-Agent": "Mozilla/5.0 rainbow-connector-review" };

function usableUrl(value) {
  return /^https:\/\//i.test(String(value || "")) ? String(value) : null;
}

export async function loadDelDotCameras() {
  if (deldotCache && Date.now() - deldotCacheAt < CATALOG_TTL_MS) return deldotCache;
  const query = new URLSearchParams({
    where: "1=1",
    outFields: "ID,TITLE,M3U8S,TIMESTAMP,CAMERA_URL",
    returnGeometry: "true",
    outSR: "4326",
    f: "json",
  });
  const response = await fetch(`${DELDOT_QUERY}?${query}`, {
    headers: FETCH_HEADERS,
  });
  if (!response.ok) throw new Error(`DelDOT camera list failed (${response.status})`);
  const payload = await response.json();
  deldotCache = (payload.features || []).map(feature => ({
    id: feature.attributes?.ID,
    name: feature.attributes?.TITLE,
    streamUrl: feature.attributes?.M3U8S,
    pageUrl: feature.attributes?.CAMERA_URL,
    updatedAt: feature.attributes?.TIMESTAMP || null,
    lat: Number(feature.geometry?.y),
    lon: Number(feature.geometry?.x),
  })).filter(camera => camera.id && camera.streamUrl
    && Number.isFinite(camera.lat) && Number.isFinite(camera.lon));
  deldotCacheAt = Date.now();
  return deldotCache;
}

export function parseCaltransDistrict(payload, district) {
  return (payload?.data || []).map(row => row?.cctv)
    .filter(camera => camera && String(camera.inService).toLowerCase() === "true").map(camera => {
    const location = camera.location || {};
    const image = camera.imageData || {};
    const staticImage = image.static || {};
    return {
      id: `ca-${String(district).padStart(2, "0")}-${camera.index}`,
      name: location.locationName || `Caltrans District ${district} camera ${camera.index}`,
      imageUrl: usableUrl(staticImage.currentImageURL),
      streamUrl: usableUrl(image.streamingVideoURL),
      pageUrl: "https://quickmap.dot.ca.gov/",
      updatedAt: camera.recordTimestamp
        ? `${camera.recordTimestamp.recordDate || ""}T${camera.recordTimestamp.recordTime || ""}`
        : null,
      lat: Number(location.latitude),
      lon: Number(location.longitude),
      routeDirection: location.direction || null,
    };
  }).filter(camera => (camera.imageUrl || camera.streamUrl)
    && Number.isFinite(camera.lat) && Number.isFinite(camera.lon));
}

export async function loadCaltransCameras() {
  if (caltransCache && Date.now() - caltransCacheAt < CATALOG_TTL_MS) return caltransCache;
  const results = await Promise.allSettled(Array.from({ length: 12 }, async (_, index) => {
    const district = index + 1;
    const response = await fetch(CALTRANS_DISTRICT_URL(district), { headers: FETCH_HEADERS });
    if (!response.ok) throw new Error(`Caltrans district ${district} failed (${response.status})`);
    return parseCaltransDistrict(await response.json(), district);
  }));
  const successful = results.filter(result => result.status === "fulfilled");
  if (!successful.length) throw new Error("Caltrans camera catalogs were unavailable");
  caltransCache = successful.flatMap(result => result.value);
  caltransCacheAt = Date.now();
  return caltransCache;
}

export function parseIowaFeatures(payload) {
  return (payload?.features || []).map(feature => {
    const attributes = feature.attributes || {};
    return {
      id: `ia-${attributes.device_id}`,
      name: attributes.Desc_ || attributes.COMMON_ID || `Iowa DOT camera ${attributes.device_id}`,
      imageUrl: usableUrl(attributes.ImageURL),
      streamUrl: usableUrl(attributes.VideoURL),
      pageUrl: "https://data.iowadot.gov/datasets/IowaDOT::traffic-cameras-3/about",
      updatedAt: null,
      lat: Number(attributes.latitude ?? feature.geometry?.y),
      lon: Number(attributes.longitude ?? feature.geometry?.x),
    };
  }).filter(camera => camera.id !== "ia-undefined" && (camera.imageUrl || camera.streamUrl)
    && Number.isFinite(camera.lat) && Number.isFinite(camera.lon));
}

export async function loadIowaCameras() {
  if (iowaCache && Date.now() - iowaCacheAt < CATALOG_TTL_MS) return iowaCache;
  const query = new URLSearchParams({
    where: "1=1",
    outFields: "device_id,Desc_,ImageURL,VideoURL,latitude,longitude,ORG,COMMON_ID",
    returnGeometry: "true",
    outSR: "4326",
    f: "json",
  });
  const response = await fetch(`${IOWA_QUERY}?${query}`, { headers: FETCH_HEADERS });
  if (!response.ok) throw new Error(`Iowa DOT camera list failed (${response.status})`);
  iowaCache = parseIowaFeatures(await response.json());
  iowaCacheAt = Date.now();
  return iowaCache;
}

export function parseOhioCameras(payload) {
  return (payload?.results || []).flatMap(site => (site.cameraViews || []).map((view, index) => ({
    id: `oh-${site.id}-${index + 1}`,
    name: [site.description || site.location || `Ohio camera ${site.id}`, view.direction].filter(Boolean).join(" — "),
    imageUrl: usableUrl(view.largeUrl) || usableUrl(view.smallUrl),
    streamUrl: null,
    pageUrl: "https://ohgo.com/",
    updatedAt: payload.lastUpdated || null,
    lat: Number(site.latitude),
    lon: Number(site.longitude),
  }))).filter(camera => camera.imageUrl && Number.isFinite(camera.lat) && Number.isFinite(camera.lon));
}

export async function loadOhioCameras() {
  if (ohioCache && Date.now() - ohioCacheAt < CATALOG_TTL_MS) return ohioCache;
  const key = String(process.env.OHGO_API_KEY || "").trim();
  if (!key) throw new Error("OHGO_API_KEY is not configured");
  const response = await fetch(OHGO_CAMERAS, {
    headers: { ...FETCH_HEADERS, Authorization: `APIKEY ${key}` },
  });
  if (!response.ok) throw new Error(`OHGO camera list failed (${response.status})`);
  ohioCache = parseOhioCameras(await response.json());
  ohioCacheAt = Date.now();
  return ohioCache;
}

export function parseWsdotCameras(payload) {
  return (Array.isArray(payload) ? payload : []).filter(camera => camera.IsActive !== false).map(camera => ({
    id: `wa-${camera.CameraID}`,
    name: camera.Title || camera.Description || `WSDOT camera ${camera.CameraID}`,
    imageUrl: usableUrl(camera.ImageURL),
    streamUrl: null,
    pageUrl: usableUrl(camera.OwnerURL) || "https://wsdot.com/Travel/Real-time/Map/",
    updatedAt: null,
    lat: Number(camera.DisplayLatitude ?? camera.CameraLocation?.Latitude),
    lon: Number(camera.DisplayLongitude ?? camera.CameraLocation?.Longitude),
  })).filter(camera => camera.id !== "wa-undefined" && camera.imageUrl
    && Number.isFinite(camera.lat) && Number.isFinite(camera.lon));
}

export async function loadWsdotCameras() {
  if (wsdotCache && Date.now() - wsdotCacheAt < CATALOG_TTL_MS) return wsdotCache;
  const accessCode = String(process.env.WSDOT_ACCESS_CODE || "").trim();
  if (!accessCode) throw new Error("WSDOT_ACCESS_CODE is not configured");
  const response = await fetch(`${WSDOT_CAMERAS}?${new URLSearchParams({ AccessCode: accessCode })}`, {
    headers: FETCH_HEADERS,
  });
  if (!response.ok) throw new Error(`WSDOT camera list failed (${response.status})`);
  wsdotCache = parseWsdotCameras(await response.json());
  wsdotCacheAt = Date.now();
  return wsdotCache;
}

export function matchNearbyDotCameras(event, catalog, maxDistanceKm = 35, limit = 3) {
  const rep = event?.representative || {};
  if (!Number.isFinite(rep.lat) || !Number.isFinite(rep.lon)) return [];
  const seen = new Set();
  return (catalog || [])
    .map(camera => ({ camera, distanceKm: distKm(rep.lat, rep.lon, camera.lat, camera.lon) }))
    .filter(match => match.distanceKm <= maxDistanceKm)
    .sort((a, b) => a.distanceKm - b.distanceKm)
    .filter(match => {
      const key = String(match.camera.id);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, Math.max(1, Math.min(Number(limit) || 3, 5)));
}

export async function deldotJobs(events, options = {}) {
  const catalog = await loadDelDotCameras();
  const maxDistanceKm = Number(options.maxDistanceKm || 35);
  const camerasPerEvent = Number(options.camerasPerEvent || 3);
  return (events || []).flatMap(event => matchNearbyDotCameras(
    event, catalog, maxDistanceKm, camerasPerEvent,
  ).map(match => ({
    eventId: event.id,
    source: "DelDOT",
    cameraId: match.camera.id,
    cameraName: match.camera.name || match.camera.id,
    cameraPageUrl: match.camera.pageUrl || null,
    imageUrl: match.camera.imageUrl || null,
    streamUrl: match.camera.streamUrl,
    distanceKm: Number(match.distanceKm.toFixed(1)),
    bearingDifference: null,
    direction: "Unknown/current view may change",
  })));
}

function stateCode(event) {
  return String(event?.representative?.nearestZip?.state
    || event?.detections?.at?.(-1)?.nearestZip?.state || "").toUpperCase();
}

function catalogJobs(event, catalog, metadata, options = {}) {
  return matchNearbyDotCameras(
    event, catalog, Number(options.maxDistanceKm || 35), Number(options.camerasPerEvent || 3),
  ).map(match => ({
    eventId: event.id,
    source: metadata.source,
    state: metadata.state,
    cameraId: match.camera.id,
    cameraName: match.camera.name || match.camera.id,
    cameraPageUrl: match.camera.pageUrl || metadata.pageUrl || null,
    imageUrl: match.camera.imageUrl || null,
    streamUrl: match.camera.streamUrl || null,
    distanceKm: Number(match.distanceKm.toFixed(1)),
    bearingDifference: null,
    direction: metadata.direction,
  }));
}

export async function dotCameraJobs(events, options = {}) {
  const requestedStates = new Set((events || []).map(stateCode));
  const catalogs = new Map();
  await Promise.all([
    requestedStates.has("DE") && loadDelDotCameras().then(value => catalogs.set("DE", value)).catch(() => null),
    requestedStates.has("CA") && loadCaltransCameras().then(value => catalogs.set("CA", value)).catch(() => null),
    requestedStates.has("IA") && loadIowaCameras().then(value => catalogs.set("IA", value)).catch(() => null),
    requestedStates.has("OH") && loadOhioCameras().then(value => catalogs.set("OH", value)).catch(() => null),
    requestedStates.has("WA") && loadWsdotCameras().then(value => catalogs.set("WA", value)).catch(() => null),
  ].filter(Boolean));
  const metadata = {
    DE: { source: "DelDOT", state: "DE", pageUrl: "https://deldot.gov/map/index.shtml?tab=Cameras", direction: "Current view may change; bearing not published" },
    CA: { source: "Caltrans", state: "CA", pageUrl: "https://quickmap.dot.ca.gov/", direction: "Camera bearing not published; roadway direction is not camera direction" },
    IA: { source: "Iowa DOT", state: "IA", pageUrl: "https://data.iowadot.gov/datasets/IowaDOT::traffic-cameras-3/about", direction: "Current view may change; bearing not published" },
    OH: { source: "Ohio DOT", state: "OH", pageUrl: "https://ohgo.com/", direction: "Current view may be PTZ; usable bearing not published" },
    WA: { source: "WSDOT", state: "WA", pageUrl: "https://wsdot.com/Travel/Real-time/Map/", direction: "Published roadway direction is not treated as camera bearing" },
  };
  return (events || []).flatMap(event => {
    const state = stateCode(event);
    return catalogs.has(state) ? catalogJobs(event, catalogs.get(state), metadata[state], options) : [];
  });
}
