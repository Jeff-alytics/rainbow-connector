#!/usr/bin/env node
import { inflateSync } from "node:zlib";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");

const RAD = Math.PI / 180;
const ANTISOLAR_KM = 10;
const DIRECT_GO_MIN = 200;
const DIRECT_WATCH_MIN = 120;
const OPTIMAL_SUN_MIN = 5;
const OPTIMAL_SUN_MAX = 22;
const WATCH_SUN_MIN = 0;
const WATCH_SUN_MAX = 30;
const CLOUD_CLEAR_MAX = 55;
const OBSERVER_SUNLIT_CLOUD_MAX = 60;
const RAIN_LIGHT_MIN = 80;
const RAIN_CLOUD_MAX = 75;
const NIGHT_BELOW = -0.833;
const REFRESH_MIN = 5;
const CANDIDATE_SCHEMA_VERSION = 3;

const CONUS = { latMin: 24.5, latMax: 49.4, lonMin: -124.8, lonMax: -66.9 };
const LANDCELL = 0.6;
const LAND_KM = 35;
const ANCHOR_POINTS = [
  ["New Orleans, LA", 29.9511, -90.0715],
  ["Baton Rouge, LA", 30.4515, -91.1871],
  ["Houston, TX", 29.7604, -95.3698],
  ["Dallas, TX", 32.7767, -96.7970],
  ["Austin, TX", 30.2672, -97.7431],
  ["San Antonio, TX", 29.4241, -98.4936],
  ["Miami, FL", 25.7617, -80.1918],
  ["Tampa, FL", 27.9506, -82.4572],
  ["Orlando, FL", 28.5383, -81.3792],
  ["Atlanta, GA", 33.7490, -84.3880],
  ["Nashville, TN", 36.1627, -86.7816],
  ["Memphis, TN", 35.1495, -90.0490],
  ["Birmingham, AL", 33.5186, -86.8104],
  ["Jackson, MS", 32.2988, -90.1848],
  ["Charlotte, NC", 35.2271, -80.8431],
  ["Raleigh, NC", 35.7796, -78.6382],
  ["Charleston, SC", 32.7765, -79.9311],
  ["Savannah, GA", 32.0809, -81.0912],
  ["St. Louis, MO", 38.6270, -90.1994],
  ["Kansas City, MO", 39.0997, -94.5786],
  ["Chicago, IL", 41.8781, -87.6298],
  ["Denver, CO", 39.7392, -104.9903],
  ["Phoenix, AZ", 33.4484, -112.0740],
  ["Las Vegas, NV", 36.1699, -115.1398],
  ["Los Angeles, CA", 34.0522, -118.2437],
  ["San Diego, CA", 32.7157, -117.1611],
  ["San Francisco, CA", 37.7749, -122.4194],
  ["Portland, OR", 45.5152, -122.6784],
  ["Seattle, WA", 47.6062, -122.3321],
  ["New York, NY", 40.7128, -74.0060],
  ["Philadelphia, PA", 39.9526, -75.1652],
  ["Washington, DC", 38.9072, -77.0369],
  ["Boston, MA", 42.3601, -71.0589],
];

const DEFAULTS = {
  output: path.join(ROOT, "candidates.json"),
  maxVerify: 250,
  maxGo: 50,
  gridKm: 12,
  clusterKm: 35,
  batchSize: 10,
  tileZoom: 6,
  dryRun: false,
};

function parseArgs() {
  const opts = { ...DEFAULTS };
  for (let i = 2; i < process.argv.length; i++) {
    const a = process.argv[i];
    const next = () => process.argv[++i];
    if (a === "--output") opts.output = path.resolve(ROOT, next());
    else if (a === "--max-verify") opts.maxVerify = Number(next());
    else if (a === "--max-go") opts.maxGo = Number(next());
    else if (a === "--grid-km") opts.gridKm = Number(next());
    else if (a === "--cluster-km") opts.clusterKm = Number(next());
    else if (a === "--batch-size") opts.batchSize = Number(next());
    else if (a === "--dry-run") opts.dryRun = true;
    else if (a === "--help") {
      console.log("Usage: node scripts/build-candidates.mjs [--output candidates.json] [--max-verify 250] [--dry-run]");
      process.exit(0);
    }
  }
  return opts;
}

function toJulian(date) { return date.valueOf() / 86400000 - 0.5 + 2440588; }
function toDays(date) { return toJulian(date) - 2451545; }
function rightAscension(l, b) {
  const e = RAD * 23.4397;
  return Math.atan2(Math.sin(l) * Math.cos(e) - Math.tan(b) * Math.sin(e), Math.cos(l));
}
function declination(l, b) {
  const e = RAD * 23.4397;
  return Math.asin(Math.sin(b) * Math.cos(e) + Math.cos(b) * Math.sin(e) * Math.sin(l));
}
function azimuth(H, phi, dec) {
  return Math.atan2(Math.sin(H), Math.cos(H) * Math.sin(phi) - Math.tan(dec) * Math.cos(phi));
}
function altitude(H, phi, dec) {
  return Math.asin(Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(H));
}
function siderealTime(d, lw) { return RAD * (280.16 + 360.9856235 * d) - lw; }
function solarMeanAnomaly(d) { return RAD * (357.5291 + 0.98560028 * d); }
function eclipticLongitude(M) {
  const C = RAD * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M));
  return M + C + RAD * 102.9372 + Math.PI;
}
function sunPosition(date, lat, lng) {
  const lw = RAD * -lng, phi = RAD * lat, d = toDays(date);
  const M = solarMeanAnomaly(d), L = eclipticLongitude(M);
  const dec = declination(L, 0), ra = rightAscension(L, 0);
  const H = siderealTime(d, lw) - ra;
  return { azimuth: azimuth(H, phi, dec), altitude: altitude(H, phi, dec) };
}
function solarReadout(date, lat, lng) {
  const p = sunPosition(date, lat, lng);
  const elev = p.altitude / RAD;
  const bearing = ((p.azimuth / RAD + 180) + 180) % 360;
  return { elev, bearing };
}
function bowPossible(elev) { return elev > NIGHT_BELOW && elev < 42; }
function optimalSun(elev) { return elev >= OPTIMAL_SUN_MIN && elev <= OPTIMAL_SUN_MAX; }
function watchSun(elev) { return elev >= WATCH_SUN_MIN && elev <= WATCH_SUN_MAX; }
function offset(lat, lon, km, bearingDeg) {
  const b = bearingDeg * RAD;
  return {
    lat: lat + (km / 111) * Math.cos(b),
    lon: lon + (km / (111 * Math.cos(lat * RAD))) * Math.sin(b),
  };
}
function distKm(la1, lo1, la2, lo2) {
  const R = 6371, dLa = (la2 - la1) * RAD, dLo = (lo2 - lo1) * RAD;
  const a = Math.sin(dLa / 2) ** 2 + Math.cos(la1 * RAD) * Math.cos(la2 * RAD) * Math.sin(dLo / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}
function compassLabel(deg) {
  const dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  return dirs[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16];
}

function lonLatToPixel(lat, lon, z) {
  const n = 2 ** z, xf = (lon + 180) / 360 * n, lr = lat * RAD;
  const yf = (1 - Math.log(Math.tan(lr) + 1 / Math.cos(lr)) / Math.PI) / 2 * n;
  return { xt: Math.floor(xf), yt: Math.floor(yf), px: Math.floor((xf % 1) * 256), py: Math.floor((yf % 1) * 256) };
}

function decodePng(buffer) {
  const sig = buffer.subarray(0, 8).toString("hex");
  if (sig !== "89504e470d0a1a0a") throw new Error("not a png");
  let p = 8, width = 0, height = 0, colorType = 0, bitDepth = 0;
  const idat = [];
  let palette = null, trns = null;
  while (p < buffer.length) {
    const len = buffer.readUInt32BE(p); p += 4;
    const type = buffer.subarray(p, p + 4).toString("ascii"); p += 4;
    const data = buffer.subarray(p, p + len); p += len + 4;
    if (type === "IHDR") {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      bitDepth = data[8];
      colorType = data[9];
    } else if (type === "PLTE") {
      palette = data;
    } else if (type === "tRNS") {
      trns = data;
    } else if (type === "IDAT") {
      idat.push(data);
    } else if (type === "IEND") {
      break;
    }
  }
  if (bitDepth !== 8) throw new Error(`unsupported png bit depth ${bitDepth}`);
  const channels = colorType === 6 ? 4 : colorType === 2 ? 3 : colorType === 3 ? 1 : 0;
  if (!channels) throw new Error(`unsupported png color type ${colorType}`);
  const raw = inflateSync(Buffer.concat(idat));
  const stride = width * channels;
  const rows = [];
  let o = 0;
  for (let y = 0; y < height; y++) {
    const filter = raw[o++];
    const row = Buffer.from(raw.subarray(o, o + stride));
    o += stride;
    const prev = rows[y - 1];
    for (let x = 0; x < stride; x++) {
      const left = x >= channels ? row[x - channels] : 0;
      const up = prev ? prev[x] : 0;
      const upLeft = prev && x >= channels ? prev[x - channels] : 0;
      if (filter === 1) row[x] = (row[x] + left) & 255;
      else if (filter === 2) row[x] = (row[x] + up) & 255;
      else if (filter === 3) row[x] = (row[x] + Math.floor((left + up) / 2)) & 255;
      else if (filter === 4) {
        const pr = left + up - upLeft;
        const pa = Math.abs(pr - left), pb = Math.abs(pr - up), pc = Math.abs(pr - upLeft);
        row[x] = (row[x] + (pa <= pb && pa <= pc ? left : pb <= pc ? up : upLeft)) & 255;
      } else if (filter !== 0) {
        throw new Error(`unsupported png filter ${filter}`);
      }
    }
    rows.push(row);
  }
  return { width, height, colorType, channels, rows, palette, trns };
}

function pngPixel(png, x, y) {
  if (x < 0 || y < 0 || x >= png.width || y >= png.height) return [0, 0, 0, 0];
  const i = (y * png.width + x) * png.channels;
  const row = png.rows[y];
  if (png.colorType === 6) return [row[i], row[i + 1], row[i + 2], row[i + 3]];
  if (png.colorType === 2) return [row[i], row[i + 1], row[i + 2], 255];
  const idx = row[i], pi = idx * 3;
  return [png.palette?.[pi] ?? 0, png.palette?.[pi + 1] ?? 0, png.palette?.[pi + 2] ?? 0, png.trns?.[idx] ?? 255];
}

function radarIntensityFromRgba([r, g, b, a]) {
  if (a < 50) return 0;
  if (Math.max(r, g, b) - Math.min(r, g, b) < 25) return 0;
  if (r > 180 && g < 120) return 1.0;
  if (r > 180) return 0.85;
  if (g > 140 && r < 160) return 0.6;
  if (b > 140) return 0.5;
  return 0;
}

async function fetchJSON(url, timeoutMs = 12000) {
  let lastErr = null;
  for (let i = 0; i < 3; i++) {
    const ctl = new AbortController();
    const timeout = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const r = await fetch(url, {
        headers: { "User-Agent": "RainbowConnectorCandidateBuilder/1.0" },
        signal: ctl.signal,
      });
      if (r.ok) return r.json();
      lastErr = new Error(`${r.status} ${url}`);
      if (![408, 429, 500, 502, 503, 504].includes(r.status)) throw lastErr;
    } catch (err) {
      lastErr = err;
    } finally {
      clearTimeout(timeout);
    }
    await new Promise(resolve => setTimeout(resolve, 800 * (i + 1)));
  }
  throw lastErr || new Error(`fetch failed ${url}`);
}

async function fetchRadarFrame() {
  const j = await fetchJSON("https://api.rainviewer.com/public/weather-maps.json");
  const past = j.radar?.past || [];
  if (!past.length) throw new Error("no RainViewer radar frames");
  const frame = past[past.length - 1];
  return { host: j.host, path: frame.path, time: frame.time, tiles: new Map() };
}

async function fetchRadarTile(rv, z, xt, yt) {
  const key = `${z}/${xt}/${yt}`;
  if (rv.tiles.has(key)) return rv.tiles.get(key);
  const url = `${rv.host}${rv.path}/256/${z}/${xt}/${yt}/4/1_1.png`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  const png = decodePng(Buffer.from(await r.arrayBuffer()));
  rv.tiles.set(key, png);
  return png;
}

async function prefetchConusRadar(rv, z) {
  const tl = lonLatToPixel(CONUS.latMax + 1, CONUS.lonMin - 1, z);
  const br = lonLatToPixel(CONUS.latMin - 1, CONUS.lonMax + 1, z);
  const tiles = [];
  for (let xt = tl.xt; xt <= br.xt; xt++) {
    for (let yt = tl.yt; yt <= br.yt; yt++) tiles.push({ xt, yt });
  }
  await pool(tiles, 12, t => fetchRadarTile(rv, z, t.xt, t.yt).catch(() => null));
  return tiles.length;
}

function radarAt(rv, z, lat, lon) {
  const p = lonLatToPixel(lat, lon, z);
  const png = rv.tiles.get(`${z}/${p.xt}/${p.yt}`);
  return png ? radarIntensityFromRgba(pngPixel(png, p.px, p.py)) : 0;
}

function radarFan(rv, z, lat, lon, bearing) {
  const bearings = [bearing - 28, bearing - 16, bearing - 8, bearing, bearing + 8, bearing + 16, bearing + 28];
  const dists = [5, 10, 15, 20, 28, 36, 48];
  let max = 0, best = null, wetSamples = 0;
  for (const d of dists) {
    for (const bb of bearings) {
      const o = offset(lat, lon, d, ((bb % 360) + 360) % 360);
      const intensity = radarAt(rv, z, o.lat, o.lon);
      if (intensity > 0) wetSamples++;
      if (intensity > max) {
        max = intensity;
        best = { lat: o.lat, lon: o.lon, distanceKm: d, bearing: ((bb % 360) + 360) % 360 };
      }
    }
  }
  return { intensity: max, best, wetSamples };
}

async function pool(items, limit, fn) {
  const out = new Array(items.length);
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (i < items.length) {
      const idx = i++;
      out[idx] = await fn(items[idx], idx);
    }
  }));
  return out;
}

function buildGridMesh(gridKm) {
  const pts = [];
  for (let lat = CONUS.latMin; lat <= CONUS.latMax; lat += gridKm / 111) {
    const dLon = gridKm / (111 * Math.cos(lat * RAD));
    for (let lon = CONUS.lonMin; lon <= CONUS.lonMax; lon += dLon) pts.push([lat, lon]);
  }
  return pts;
}

function cellIdx(lat, lon) { return [Math.round(lat / LANDCELL), Math.round(lon / LANDCELL)]; }
function buildLandCells(zipTable) {
  const cells = new Map();
  for (const z in zipTable) {
    const v = zipTable[z];
    if (!v[1]) continue;
    const [iy, ix] = cellIdx(v[2], v[3]);
    const k = `${iy},${ix}`;
    let a = cells.get(k);
    if (!a) cells.set(k, a = []);
    a.push([v[2], v[3], z]);
  }
  return cells;
}
function onLand(cells, lat, lon) {
  const [iy, ix] = cellIdx(lat, lon);
  for (let dy = -1; dy <= 1; dy++) {
    for (let dx = -1; dx <= 1; dx++) {
      const a = cells.get(`${iy + dy},${ix + dx}`);
      if (!a) continue;
      for (const [la, lo] of a) if (distKm(lat, lon, la, lo) <= LAND_KM) return true;
    }
  }
  return false;
}
function nearestZip(zipTable, cells, lat, lon) {
  const [iy, ix] = cellIdx(lat, lon);
  let best = null, bd = Infinity;
  for (let dy = -2; dy <= 2; dy++) {
    for (let dx = -2; dx <= 2; dx++) {
      const a = cells.get(`${iy + dy},${ix + dx}`);
      if (!a) continue;
      for (const [la, lo, zip] of a) {
        const d = distKm(lat, lon, la, lo);
        if (d < bd) {
          bd = d;
          const z = zipTable[zip];
          best = { zip, name: `${z[0]}, ${z[1]}`, lat: z[2], lon: z[3], distanceKm: d };
        }
      }
    }
  }
  return best;
}

function selectClustered(cands, max, clusterKm) {
  const selected = [];
  for (const c of cands) {
    if (selected.some(s => distKm(c.lat, c.lon, s.lat, s.lon) < clusterKm)) continue;
    selected.push(c);
    if (selected.length >= max) break;
  }
  return selected;
}

function rawCandidateAt(rv, z, lat, lon, now, anchorLabel = null) {
  const sun = solarReadout(now, lat, lon);
  if (!bowPossible(sun.elev)) return null;
  const antiBearing = (sun.bearing + 180) % 360;
  const fan = radarFan(rv, z, lat, lon, antiBearing);
  if (fan.intensity <= 0) return null;
  const localRain = radarAt(rv, z, lat, lon);
  const edgeBonus = Math.max(0, 1 - localRain) * 0.25;
  const sunBonus = Math.exp(-(((sun.elev - 13) / 9) ** 2)) * 0.45;
  const sampleBonus = Math.min(0.15, fan.wetSamples / 100);
  return {
    lat, lon,
    anchorLabel,
    geo: true,
    sunElev: sun.elev,
    sunBearing: sun.bearing,
    antiBearing,
    rainIntensity: fan.intensity,
    wetSamples: fan.wetSamples,
    rainPoint: fan.best,
    observerRain: localRain,
    score: fan.intensity + edgeBonus + sunBonus + sampleBonus + (anchorLabel ? 0.08 : 0),
  };
}

async function batchOpenMeteo(cands, batchSize) {
  const results = [];
  const safeBatchSize = Math.max(1, Math.min(batchSize || DEFAULTS.batchSize, 10));
  for (let i = 0; i < cands.length; i += safeBatchSize) {
    const batch = cands.slice(i, i + safeBatchSize);
    const lats = batch.map(c => c.lat.toFixed(4)).join(",");
    const lons = batch.map(c => c.lon.toFixed(4)).join(",");
    const url = "https://api.open-meteo.com/v1/forecast"
      + `?latitude=${lats}&longitude=${lons}`
      + "&current=cloud_cover,direct_normal_irradiance_instant"
      + "&forecast_days=1&timezone=GMT";
    let arr = null;
    try {
      const j = await fetchJSON(url);
      if (batch.length > 1 && !Array.isArray(j)) {
        throw new Error("Open-Meteo returned a non-array response for a batched request");
      }
      arr = Array.isArray(j) ? j : [j];
      if (arr.length !== batch.length) {
        throw new Error(`Open-Meteo returned ${arr.length} records for ${batch.length} points`);
      }
    } catch (err) {
      arr = [];
      for (let k = 0; k < batch.length; k++) {
        results.push({
          ...batch[k],
          cloudCover: null,
          directRadiation: null,
          openMeteoError: err?.message || "Open-Meteo unavailable",
        });
      }
      continue;
    }
    for (let k = 0; k < batch.length; k++) {
      const current = arr[k]?.current || {};
      results.push({
        ...batch[k],
        cloudCover: current.cloud_cover ?? null,
        directRadiation: current.direct_normal_irradiance_instant ?? null,
      });
    }
  }
  return results;
}

function goVerdict(c) {
  const f = gateFlags(c);
  return f.geo && f.optimalGeometry && f.rain && f.directGo && !f.uniformOvercast;
}

function watchVerdict(c) {
  const f = gateFlags(c);
  return !goVerdict(c) && f.geo && f.watchGeometry && f.rain && f.directWatch && !f.uniformOvercast;
}

function legacyGoVerdict(c) {
  const f = gateFlags(c);
  return f.geo && f.rain && f.openMeteoRadiationOk && f.openMeteoCloudOk && f.rainPointRadiationOk && f.rainPointCloudOk;
}

function gateFlags(c) {
  const openMeteoCloudOk = c.cloudCover != null ? c.cloudCover < OBSERVER_SUNLIT_CLOUD_MAX : null;
  const openMeteoRadiationOk = c.directRadiation != null ? c.directRadiation >= DIRECT_GO_MIN : null;
  const openMeteoSun = openMeteoRadiationOk === true;
  const rainPointCloudOk = c.rainCloudCover != null ? c.rainCloudCover < RAIN_CLOUD_MAX : null;
  const rainPointRadiationOk = (c.rainDirectRadiation ?? 0) >= RAIN_LIGHT_MIN;
  const observerSources = {
    openMeteoCloud: openMeteoCloudOk,
    openMeteoRadiation: openMeteoRadiationOk,
  };
  const sourceValues = Object.values(observerSources);
  // Cloud fraction is context, not proof that the solar disc is visible.
  // Require the direct-radiation signal for a nationwide GO.
  const directGo = openMeteoRadiationOk;
  const directWatch = c.directRadiation != null ? c.directRadiation >= DIRECT_WATCH_MIN : null;
  const observerSun = directGo;
  const mixedSun = observerSun === true && sourceValues.some(v => v === false);
  const uniformOvercast = c.cloudCover != null && c.cloudCover >= 90
    && (c.directRadiation == null || c.directRadiation < DIRECT_WATCH_MIN);
  return {
    geo: !!c.geo,
    optimalGeometry: optimalSun(c.sunElev),
    watchGeometry: watchSun(c.sunElev),
    rain: (c.rainIntensity ?? 0) > 0,
    openMeteoCloudOk,
    openMeteoRadiationOk,
    openMeteoSun,
    observerSun,
    directGo,
    directWatch,
    uniformOvercast,
    mixedSun,
    rainPointCloudOk,
    rainPointRadiationOk,
  };
}

function opportunityScore(c) {
  const sun = Math.exp(-(((c.sunElev - 13) / 8) ** 2));
  const direct = Math.max(0, Math.min(1, ((c.directRadiation ?? 0) - DIRECT_WATCH_MIN) / 280));
  const rain = Math.max(0, Math.min(1, c.rainIntensity ?? 0));
  const sector = Math.max(0, Math.min(1, (c.wetSamples ?? 0) / 12));
  const edge = Math.max(0, 1 - (c.observerRain ?? 0));
  return 100 * (0.32 * sun + 0.32 * direct + 0.2 * rain + 0.1 * sector + 0.06 * edge);
}

function legacyBlocks(c) {
  const f = gateFlags(c);
  const blocks = [];
  if (!f.geo) blocks.push("sun-geometry");
  if (!f.rain) blocks.push("rain-opposite-sun");
  if (f.openMeteoCloudOk === false) blocks.push("open-meteo-cloud");
  if (f.openMeteoRadiationOk === false) blocks.push("open-meteo-direct-radiation");
  if (f.rainPointCloudOk === false) blocks.push("rain-point-cloud");
  if (!f.rainPointRadiationOk) blocks.push("rain-point-direct-radiation");
  return blocks;
}

function rejectionDiagnostics(checked) {
  const counts = {
    checked: checked.length,
    lowSunAndRain: 0,
    observerSunBlocked: 0,
    mixedSourceGoCandidates: 0,
    openMeteoSunCandidates: 0,
    openMeteoCloudWouldHaveBlocked: 0,
    openMeteoRadiationWouldHaveBlocked: 0,
    rainPointCloudWouldHaveBlocked: 0,
    rainPointRadiationWouldHaveBlocked: 0,
    legacyGoCandidates: 0,
    sourceBlendGoCandidates: 0,
  };
  const nearMisses = [];

  for (const c of checked) {
    const f = gateFlags(c);
    const base = f.geo && f.rain;
    if (base) counts.lowSunAndRain++;
    if (base && !f.observerSun) counts.observerSunBlocked++;
    if (base && f.observerSun && f.mixedSun) counts.mixedSourceGoCandidates++;
    if (base && f.observerSun && f.openMeteoSun) counts.openMeteoSunCandidates++;
    if (base && f.observerSun && f.openMeteoCloudOk === false) counts.openMeteoCloudWouldHaveBlocked++;
    if (base && f.observerSun && f.openMeteoRadiationOk === false) counts.openMeteoRadiationWouldHaveBlocked++;
    if (base && f.observerSun && f.rainPointCloudOk === false) counts.rainPointCloudWouldHaveBlocked++;
    if (base && f.observerSun && !f.rainPointRadiationOk) counts.rainPointRadiationWouldHaveBlocked++;
    if (legacyGoVerdict(c)) counts.legacyGoCandidates++;
    if (goVerdict(c)) counts.sourceBlendGoCandidates++;

    if (base && !goVerdict(c) && nearMisses.length < 10) {
      nearMisses.push({
        lat: Number(c.lat.toFixed(4)),
        lon: Number(c.lon.toFixed(4)),
        sunElevationDeg: Number(c.sunElev.toFixed(1)),
        cloudCoverPct: c.cloudCover == null ? null : Math.round(c.cloudCover),
        directNormalIrradianceWm2: c.directRadiation == null ? null : Math.round(c.directRadiation),
        rainIntensity: Number(c.rainIntensity.toFixed(2)),
        score: Number(c.score.toFixed(3)),
        blocks: legacyBlocks(c),
      });
    }
  }

  return { counts, nearMisses };
}

async function verifyRainPointSun(cands, batchSize) {
  const withRainPoints = cands.filter(c => c.rainPoint && Number.isFinite(c.rainPoint.lat) && Number.isFinite(c.rainPoint.lon));
  const checked = await batchOpenMeteo(withRainPoints.map(c => ({
    ...c,
    lat: c.rainPoint.lat,
    lon: c.rainPoint.lon,
  })), batchSize);
  const byKey = new Map(checked.map(c => [`${c.rainPoint.lat.toFixed(4)},${c.rainPoint.lon.toFixed(4)}`, c]));
  return cands.map(c => {
    if (!c.rainPoint) return c;
    const hit = byKey.get(`${c.rainPoint.lat.toFixed(4)},${c.rainPoint.lon.toFixed(4)}`);
    return {
      ...c,
      rainCloudCover: hit?.cloudCover ?? null,
      rainDirectRadiation: hit?.directRadiation ?? null,
    };
  });
}

export async function buildCandidates(options = {}) {
  const opts = { ...DEFAULTS, ...options };
  const startedAt = new Date();
  const zipTable = JSON.parse(await readFile(path.join(ROOT, "zip-centroids.json"), "utf8"));
  const landCells = buildLandCells(zipTable);

  const rv = await fetchRadarFrame();
  const tileRequests = await prefetchConusRadar(rv, opts.tileZoom);
  const mesh = buildGridMesh(opts.gridKm);

  let land = 0, geo = 0, rain = 0;
  const raw = [];
  for (const [lat, lon] of mesh) {
    if (!onLand(landCells, lat, lon)) continue;
    land++;
    const hit = rawCandidateAt(rv, opts.tileZoom, lat, lon, startedAt);
    if (!hit) {
      const sun = solarReadout(startedAt, lat, lon);
      if (bowPossible(sun.elev)) geo++;
      continue;
    }
    geo++;
    rain++;
    raw.push(hit);
  }
  let anchorMatches = 0;
  for (const [label, lat, lon] of ANCHOR_POINTS) {
    if (!onLand(landCells, lat, lon)) continue;
    const hit = rawCandidateAt(rv, opts.tileZoom, lat, lon, startedAt, label);
    if (!hit) continue;
    anchorMatches++;
    raw.push(hit);
  }
  raw.sort((a, b) => b.score - a.score);
  const verify = selectClustered(raw, opts.maxVerify, opts.clusterKm);
  const observerChecked = await batchOpenMeteo(verify, opts.batchSize);
  const checked = await verifyRainPointSun(observerChecked, opts.batchSize);
  const gateDiagnostics = rejectionDiagnostics(checked);
  const materialize = (c, i, verdict) => {
      const zip = nearestZip(zipTable, landCells, c.lat, c.lon);
      return {
        id: `${startedAt.toISOString()}-${verdict}-${i + 1}`,
        rank: i + 1,
        lat: Number(c.lat.toFixed(4)),
        lon: Number(c.lon.toFixed(4)),
        nearestZip: zip ? {
          zip: zip.zip,
          name: zip.name,
          distanceKm: Number(zip.distanceKm.toFixed(1)),
        } : null,
        label: c.anchorLabel || zip?.name || `${c.lat.toFixed(2)}, ${c.lon.toFixed(2)}`,
        anchorLabel: c.anchorLabel || null,
        verdict,
        confidence: verdict === "go" ? "high" : "possible",
        direction: {
          bearing: Math.round(c.antiBearing),
          compass: compassLabel(c.antiBearing),
          label: `${compassLabel(c.antiBearing)} (${Math.round(c.antiBearing)}°)`,
        },
        criteria: {
          sunLowEnough: true,
          directSun: true,
          rainOppositeSun: true,
        },
        evidence: {
          sunElevationDeg: Number(c.sunElev.toFixed(1)),
          rainbowArcDeg: Number(Math.max(0, 42 - c.sunElev).toFixed(1)),
          directNormalIrradianceWm2: c.directRadiation == null ? null : Math.round(c.directRadiation),
          cloudCoverPct: c.cloudCover == null ? null : Math.round(c.cloudCover),
          sunlightDecision: verdict === "go" ? (gateFlags(c).mixedSun ? "mixed-go" : "go") : "watch",
          sunlightSources: {
            openMeteoCloud: c.cloudCover == null ? null : c.cloudCover < OBSERVER_SUNLIT_CLOUD_MAX,
            openMeteoDirectRadiation: c.directRadiation == null ? null : c.directRadiation >= DIRECT_GO_MIN,
          },
          rainPointDirectNormalIrradianceWm2: c.rainDirectRadiation == null ? null : Math.round(c.rainDirectRadiation),
          rainPointCloudCoverPct: c.rainCloudCover == null ? null : Math.round(c.rainCloudCover),
          rainIntensity: Number(c.rainIntensity.toFixed(2)),
          observerRainIntensity: Number(c.observerRain.toFixed(2)),
          rainPoint: c.rainPoint ? {
            lat: Number(c.rainPoint.lat.toFixed(4)),
            lon: Number(c.rainPoint.lon.toFixed(4)),
            distanceKm: c.rainPoint.distanceKm,
            bearing: Math.round(c.rainPoint.bearing),
          } : null,
          score: Number(opportunityScore(c).toFixed(1)),
          rawRadarScore: Number(c.score.toFixed(3)),
          calibration: "FAA+ARM-2026-07",
        },
      };
    };
  const go = checked
    .filter(goVerdict)
    .sort((a, b) => opportunityScore(b) - opportunityScore(a))
    .slice(0, opts.maxGo)
    .map((c, i) => materialize(c, i, "go"));
  const possible = checked
    .filter(watchVerdict)
    .sort((a, b) => opportunityScore(b) - opportunityScore(a))
    .slice(0, opts.maxGo)
    .map((c, i) => materialize(c, i, "watch"));

  const artifact = {
    generatedAt: startedAt.toISOString(),
    schemaVersion: CANDIDATE_SCHEMA_VERSION,
    dataStatus: "live",
    expiresAt: new Date(startedAt.getTime() + REFRESH_MIN * 60 * 1000).toISOString(),
    source: {
      radar: "RainViewer",
      radarFrameTime: new Date(rv.time * 1000).toISOString(),
      sunlight: "Open-Meteo direct_normal_irradiance_instant; GOES DSRF plus clear-sky mask corroboration when fresh",
    },
    cadenceMinutes: REFRESH_MIN,
    criteria: {
      physicalSunElevation: `>${NIGHT_BELOW} and <42 degrees`,
      highConfidenceSunElevation: `${OPTIMAL_SUN_MIN}-${OPTIMAL_SUN_MAX} degrees (validated positives 7.7-19.1 degrees)`,
      possibleSunElevation: `${WATCH_SUN_MIN}-${WATCH_SUN_MAX} degrees`,
      directSun: `GO requires direct_normal_irradiance_instant >= ${DIRECT_GO_MIN} W/m2`,
      observerSunlight: `possible tier requires direct_normal_irradiance_instant >= ${DIRECT_WATCH_MIN} W/m2`,
      uniformOvercast: "reject cloud cover >= 90% when direct-normal irradiance is below the possible threshold",
      rainOppositeSun: "radar return in anti-solar fan 5-48 km from observer",
      rainPointSunlight: `reported for diagnosis only; old gate was direct_radiation >= ${RAIN_LIGHT_MIN} W/m2 and cloud_cover < ${RAIN_CLOUD_MAX}%`,
    },
    candidates: go,
    possibleCandidates: possible,
    diagnostics: {
      meshPoints: mesh.length,
      landPoints: land,
      geometryMatches: geo,
      radarMatches: rain,
      clusteredForVerification: verify.length,
      weatherApiCalls: Math.ceil(verify.length / opts.batchSize) + Math.ceil(observerChecked.filter(c => c.rainPoint).length / opts.batchSize),
      anchorPoints: ANCHOR_POINTS.length,
      anchorMatches,
      rainViewerTileRequests: tileRequests + 1,
      goCandidates: go.length,
      possibleCandidates: possible.length,
      gateDiagnostics,
      options: {
        gridKm: opts.gridKm,
        clusterKm: opts.clusterKm,
        maxVerify: opts.maxVerify,
        maxGo: opts.maxGo,
        tileZoom: opts.tileZoom,
      },
    },
  };

  return artifact;
}

export const SCIENCE_THRESHOLDS = Object.freeze({
  directGoMinWm2: DIRECT_GO_MIN,
  directWatchMinWm2: DIRECT_WATCH_MIN,
  optimalSunMinDeg: OPTIMAL_SUN_MIN,
  optimalSunMaxDeg: OPTIMAL_SUN_MAX,
  watchSunMinDeg: WATCH_SUN_MIN,
  watchSunMaxDeg: WATCH_SUN_MAX,
  physicalSunMinDeg: NIGHT_BELOW,
  physicalSunMaxDeg: 42,
});
export { gateFlags, goVerdict, watchVerdict, opportunityScore };

async function main() {
  const opts = parseArgs();
  const artifact = await buildCandidates(opts);
  if (!opts.dryRun) await writeFile(opts.output, JSON.stringify(artifact, null, 2), "utf8");
  console.log(JSON.stringify({
    output: opts.dryRun ? null : opts.output,
    diagnostics: artifact.diagnostics,
    top: artifact.candidates.slice(0, 5),
  }, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(err => {
    console.error(err);
    process.exit(1);
  });
}
