/* ============================================================
   compute_grid.js — nationwide SKY grid for The Rainbow Connector

   The browser now computes the two time-sensitive ingredients itself:
     • geometry  — sun elevation/bearing (pure math, SunCalc)
     • rain      — live RainViewer radar in the anti-solar fan
   So the ONLY thing that needs a server-side, rate-limited fetch is cloud
   cover (clouds drift slowly, so a periodic snapshot is fine). This script
   samples MET Norway cloud_area_fraction on a ~1° CONUS mesh (daytime half
   only, to save calls) and writes a tiny rainbow-grid.json:

     { generated, sky: [[lat, lon, cloud%], ...] }

   MET Norway is keyless and has no per-IP daily cap (unlike Open-Meteo);
   it only requires an identifying User-Agent.

   Run:  node compute_grid.js
   ============================================================ */
const fs = require("fs");
const path = require("path");

/* ---------- sun position (inlined SunCalc, MIT — V. Agafonkin) ---------- */
const RAD = Math.PI / 180;
const DAY_MS = 86400000, J1970 = 2440588, J2000 = 2451545;
const OBLIQ = RAD * 23.4397;
const toJulian = d => d.valueOf() / DAY_MS - 0.5 + J1970;
const toDays   = d => toJulian(d) - J2000;
const rightAsc = (l, b) => Math.atan2(Math.sin(l) * Math.cos(OBLIQ) - Math.tan(b) * Math.sin(OBLIQ), Math.cos(l));
const decl     = (l, b) => Math.asin(Math.sin(b) * Math.cos(OBLIQ) + Math.cos(b) * Math.sin(OBLIQ) * Math.sin(l));
const azC      = (H, phi, dec) => Math.atan2(Math.sin(H), Math.cos(H) * Math.sin(phi) - Math.tan(dec) * Math.cos(phi));
const altC     = (H, phi, dec) => Math.asin(Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(H));
const sidereal = (d, lw) => RAD * (280.16 + 360.9856235 * d) - lw;
const solarAnom= d => RAD * (357.5291 + 0.98560028 * d);
function eclLon(M){
  const C = RAD * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2*M) + 0.0003 * Math.sin(3*M));
  return M + C + RAD * 102.9372 + Math.PI;
}
function sunPosition(date, lat, lng){
  const lw = RAD * -lng, phi = RAD * lat, d = toDays(date);
  const M = solarAnom(d), L = eclLon(M);
  const dec = decl(L, 0), ra = rightAsc(L, 0);
  const H = sidereal(d, lw) - ra;
  return { azimuth: azC(H, phi, dec), altitude: altC(H, phi, dec) };
}
function solarReadout(date, lat, lng){
  const p = sunPosition(date, lat, lng);
  const elev = p.altitude / RAD;
  let bearing = (p.azimuth / RAD + 180) % 360;
  if (bearing < 0) bearing += 360;
  return { elev, bearing };
}

/* ---------- MET Norway cloud cover (keyless, no daily cap) ---------- */
const sleep = ms => new Promise(r => setTimeout(r, ms));
const MET_UA = "RainbowConnector/1.0 (github.com/jeff-alytics; jasher@ahdatalytics.com)";
async function metCloud(lat, lon){
  const url = `https://api.met.no/weatherapi/locationforecast/2.0/compact`
    + `?lat=${lat.toFixed(4)}&lon=${lon.toFixed(4)}`;
  for (let i = 0; i < 3; i++){
    try {
      const r = await fetch(url, { headers: { "User-Agent": MET_UA } });
      if (r.status === 429 || r.status === 403){ await sleep(800 * (i + 1)); continue; }
      if (!r.ok) return null;
      const j = await r.json();
      const ts = j.properties && j.properties.timeseries && j.properties.timeseries[0];
      const c = ts && ts.data && ts.data.instant && ts.data.instant.details
        && ts.data.instant.details.cloud_area_fraction;
      return (typeof c === "number") ? c : null;
    } catch { await sleep(400 * (i + 1)); }
  }
  return null;
}
async function cloudBatch(cells, conc = 8){
  const out = new Array(cells.length).fill(null);
  let idx = 0, fail = 0, done = 0;
  async function worker(){
    while (idx < cells.length){
      const my = idx++;
      out[my] = await metCloud(cells[my].lat, cells[my].lon);
      if (out[my] == null) fail++;
      if (++done % 100 === 0) console.log(`  cloud ${done}/${cells.length}`);
      await sleep(30);
    }
  }
  await Promise.all(Array.from({ length: conc }, worker));
  if (fail) console.warn(`  cloud fetch: ${fail}/${cells.length} cells unknown`);
  return out;
}

/* ---------- 1° CONUS sky mesh (daytime cells only) ---------- */
const CONUS = { latMin: 24.5, latMax: 49.4, lonMin: -124.8, lonMax: -66.9 };
// Skip the night half — those cells can't host a bow during this file's short life.
// A generous elevation margin (-6°) covers points near the terminator and the ~15-min
// staleness window before the next recompute.
function skyCells(now){
  const cells = [];
  for (let lat = Math.floor(CONUS.latMin); lat <= Math.ceil(CONUS.latMax); lat++){
    for (let lon = Math.floor(CONUS.lonMin); lon <= Math.ceil(CONUS.lonMax); lon++){
      if (solarReadout(now, lat, lon).elev > -6) cells.push({ lat, lon });
    }
  }
  return cells;
}

async function main(){
  const now = new Date();
  console.log("Rainbow sky grid @", now.toISOString());
  const cells = skyCells(now);
  console.log(`sky cells (daytime CONUS 1°): ${cells.length}`);

  const clouds = await cloudBatch(cells);
  const sky = cells.map((c, i) => [c.lat, c.lon, clouds[i] == null ? null : Math.round(clouds[i])]);

  writeOut(now, sky);
}

function writeOut(now, sky){
  const out = { generated: now.toISOString(), sky };
  const file = path.join(__dirname, "rainbow-grid.json");
  fs.writeFileSync(file, JSON.stringify(out));
  console.log("wrote", file, `(${sky.length} sky cells, ${(fs.statSync(file).size/1024).toFixed(1)} KB)`);
}

if (require.main === module){
  main().catch(e => { console.error(e); process.exit(1); });
}
module.exports = { solarReadout, metCloud, skyCells };
