#!/usr/bin/env node

import { createWriteStream } from "node:fs";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { pipeline } from "node:stream/promises";
import { spawnSync } from "node:child_process";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUT = path.join(ROOT, "validation", "arm-lamont");
const MANIFEST_PATH = path.join(OUT, "manifest.json");
const SITE = {
  name: "ARM Southern Great Plains C1, Lamont, Oklahoma",
  lat: 36.607322,
  lon: -97.487643,
  datastream: "sgpasiskyimageC1.a1",
  imageStart: "2023-04-15",
};
const RAD = Math.PI / 180;
const DAY_MS = 86_400_000;

function arg(name, fallback = null) {
  const index = process.argv.indexOf(`--${name}`);
  return index < 0 ? fallback : process.argv[index + 1];
}

function hasArg(name) {
  return process.argv.includes(`--${name}`);
}

function solarReadout(date) {
  const days = date.valueOf() / DAY_MS - 0.5 + 2440588 - 2451545;
  const meanAnomaly = RAD * (357.5291 + 0.98560028 * days);
  const longitude = meanAnomaly + RAD * (
    1.9148 * Math.sin(meanAnomaly)
      + 0.02 * Math.sin(2 * meanAnomaly)
      + 0.0003 * Math.sin(3 * meanAnomaly)
  ) + RAD * 102.9372 + Math.PI;
  const declination = Math.asin(Math.sin(RAD * 23.4397) * Math.sin(longitude));
  const rightAscension = Math.atan2(
    Math.sin(longitude) * Math.cos(RAD * 23.4397),
    Math.cos(longitude),
  );
  const latitude = SITE.lat * RAD;
  const hourAngle = RAD * (280.16 + 360.9856235 * days)
    + SITE.lon * RAD - rightAscension;
  const elevation = Math.asin(
    Math.sin(latitude) * Math.sin(declination)
      + Math.cos(latitude) * Math.cos(declination) * Math.cos(hourAngle),
  ) / RAD;
  const azimuth = Math.atan2(
    Math.sin(hourAngle),
    Math.cos(hourAngle) * Math.sin(latitude)
      - Math.tan(declination) * Math.cos(latitude),
  ) / RAD + 180;
  const sunBearing = ((azimuth % 360) + 360) % 360;
  return {
    sunElevationDeg: Number(elevation.toFixed(2)),
    sunBearingDeg: Number(sunBearing.toFixed(2)),
    antiSolarBearingDeg: Number(((sunBearing + 180) % 360).toFixed(2)),
    expectedRainbowTopElevationDeg: Number(Math.max(0, 42 - elevation).toFixed(2)),
  };
}

async function fetchJson(url) {
  const response = await fetch(url, {
    headers: { "User-Agent": "rainbow-connector-validation/1.0" },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${url}`);
  return response.json();
}

async function loadHourlyRows(start, end) {
  const url = new URL("https://archive-api.open-meteo.com/v1/archive");
  url.searchParams.set("latitude", SITE.lat);
  url.searchParams.set("longitude", SITE.lon);
  url.searchParams.set("start_date", start);
  url.searchParams.set("end_date", end);
  url.searchParams.set("hourly", "direct_normal_irradiance,precipitation,cloud_cover");
  url.searchParams.set("timezone", "UTC");
  const data = await fetchJson(url);
  if (!data.hourly?.time) throw new Error("No hourly archive data returned.");
  return data.hourly.time.map((time, index) => {
    const observedAt = new Date(`${time}Z`);
    return {
      observedAt: observedAt.toISOString(),
      ...solarReadout(observedAt),
      directNormalIrradianceWm2: data.hourly.direct_normal_irradiance[index],
      precipitationMm: data.hourly.precipitation[index],
      cloudCoverPct: data.hourly.cloud_cover[index],
    };
  });
}

function archiveName(observedAt) {
  const day = observedAt.slice(0, 10).replaceAll("-", "");
  return `${SITE.datastream}.${day}.000000.jpg.tar`;
}

function selectEvents(rows, limit) {
  const possible = rows.filter(row =>
    row.sunElevationDeg > -0.833
      && row.sunElevationDeg < 42
      && row.directNormalIrradianceWm2 >= 120
      && row.precipitationMm > 0
  ).map(row => ({
    ...row,
    prefilterScore: Number((row.precipitationMm * Math.log1p(row.directNormalIrradianceWm2)).toFixed(3)),
  })).sort((a, b) => b.prefilterScore - a.prefilterScore);
  const events = [];
  const usedDays = new Set();
  for (const row of possible) {
    const day = row.observedAt.slice(0, 10);
    if (usedDays.has(day)) continue;
    usedDays.add(day);
    events.push(row);
    if (events.length >= limit) break;
  }
  return { possible, events };
}

function matchedControl(event, rows, usedTimes) {
  const month = event.observedAt.slice(5, 7);
  return rows.filter(row =>
    row.observedAt.slice(5, 7) === month
      && !usedTimes.has(row.observedAt)
      && row.sunElevationDeg > -0.833
      && row.sunElevationDeg < 42
      && row.directNormalIrradianceWm2 >= 120
      && row.precipitationMm === 0
  ).map(row => ({
    row,
    distance: Math.abs(row.sunElevationDeg - event.sunElevationDeg)
      + Math.abs(row.directNormalIrradianceWm2 - event.directNormalIrradianceWm2) / 150
      + Math.abs(row.cloudCoverPct - event.cloudCoverPct) / 50,
  })).sort((a, b) => a.distance - b.distance)[0]?.row ?? null;
}

async function buildManifest() {
  const start = arg("start", SITE.imageStart);
  const end = arg("end", new Date(Date.now() - DAY_MS).toISOString().slice(0, 10));
  const limit = Math.max(1, Math.min(500, Number(arg("limit", 20)) || 20));
  const archiveUrl = new URL("https://adc.arm.gov/elastic/file_info/_search");
  archiveUrl.searchParams.set("q", `datastream:${SITE.datastream}`);
  archiveUrl.searchParams.set("size", "0");
  const [rows, archive] = await Promise.all([loadHourlyRows(start, end), fetchJson(archiveUrl)]);
  const selected = selectEvents(rows, limit);
  const usedTimes = new Set(selected.events.map(event => event.observedAt));
  const pairs = selected.events.map((event, index) => {
    const control = matchedControl(event, rows, usedTimes);
    if (control) usedTimes.add(control.observedAt);
    return {
      id: `lamont-${String(index + 1).padStart(3, "0")}`,
      event: { ...event, dayArchive: archiveName(event.observedAt) },
      control: control ? { ...control, dayArchive: archiveName(control.observedAt) } : null,
      reviewStatus: "pending",
      observedRainbow: null,
      notes: "",
    };
  });
  const manifest = {
    schemaVersion: 1,
    generatedAt: new Date().toISOString(),
    purpose: "Camera feasibility only; this hourly prefilter does not recreate historical production radar.",
    site: SITE,
    archive: {
      indexedDailyArchives: archive.hits?.total?.value ?? null,
      countRelation: archive.hits?.total?.relation ?? null,
    },
    sampleWindow: { start, end, hourlyRows: rows.length, potentialHours: selected.possible.length },
    criteria: {
      sunElevationDeg: "> -0.833 and < 42",
      directNormalIrradianceWm2: ">= 120",
      precipitationMm: "> 0 at camera",
      controls: "same month, no rain, matched on sun elevation, DNI, and cloud cover",
    },
    pairs,
  };
  await mkdir(OUT, { recursive: true });
  await writeFile(MANIFEST_PATH, `${JSON.stringify(manifest, null, 2)}\n`);
  return manifest;
}

async function credentials() {
  const text = await readFile(path.join(ROOT, ".env.arm.local"), "utf8");
  const values = Object.fromEntries(text.split(/\r?\n/).map(line => {
    const index = line.indexOf("=");
    return index < 0 ? null : [line.slice(0, index).trim(), line.slice(index + 1).trim()];
  }).filter(Boolean));
  if (!values.ARM_USER_ID || !values.ARM_ACCESS_TOKEN) throw new Error("ARM credentials are missing.");
  return `${values.ARM_USER_ID}:${values.ARM_ACCESS_TOKEN}`;
}

async function downloadArchive(fileName, auth) {
  const directory = path.join(OUT, "downloads");
  await mkdir(directory, { recursive: true });
  const destination = path.join(directory, fileName);
  try { if ((await stat(destination)).size > 0) return destination; } catch {}
  const url = new URL("https://adc.arm.gov/armlive/saveData");
  url.searchParams.set("user", auth);
  url.searchParams.set("file", fileName);
  const response = await fetch(url, { headers: { "User-Agent": "rainbow-connector-validation/1.0" } });
  if (!response.ok || !response.body) throw new Error(`ARM download failed: HTTP ${response.status}`);
  console.error(`Downloading ${fileName}...`);
  await pipeline(response.body, createWriteStream(destination));
  return destination;
}

function imageTimestamp(name) {
  const match = path.basename(name).match(/(20\d{6})[._-]?(\d{6})/);
  if (!match) return null;
  const day = match[1], time = match[2];
  return new Date(`${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6, 8)}T${time.slice(0, 2)}:${time.slice(2, 4)}:${time.slice(4, 6)}Z`);
}

async function extractNearest(tarFile, sample, id, kind) {
  const listing = spawnSync("tar", ["-tf", tarFile], { encoding: "utf8", maxBuffer: 32 * 1024 * 1024 });
  if (listing.status !== 0) throw new Error(listing.stderr);
  const target = new Date(sample.observedAt).getTime();
  const available = listing.stdout.split(/\r?\n/)
    .filter(name => /\.jpe?g$/i.test(name))
    .map(name => ({ name, time: imageTimestamp(name) }))
    .filter(frame => frame.time);
  const windowMinutes = Math.max(0, Number(arg("window-minutes", 0)));
  const stepSeconds = Math.max(15, Number(arg("step-seconds", 30)));
  let frames;
  if (windowMinutes > 0) {
    const chosen = new Map();
    for (let offset = -windowMinutes * 60; offset <= windowMinutes * 60; offset += stepSeconds) {
      const desired = target + offset * 1000;
      const nearest = available.reduce((best, frame) => (
        !best || Math.abs(frame.time.getTime() - desired) < Math.abs(best.time.getTime() - desired) ? frame : best
      ), null);
      if (nearest) chosen.set(nearest.name, nearest);
    }
    frames = [...chosen.values()].sort((a, b) => a.time - b.time);
  } else {
    frames = available
      .sort((a, b) => Math.abs(a.time.getTime() - target) - Math.abs(b.time.getTime() - target))
      .slice(0, 5);
  }
  const destination = path.join(OUT, "frames", id, kind);
  await mkdir(destination, { recursive: true });
  const extraction = spawnSync("tar", ["-xf", tarFile, "-C", destination, ...frames.map(frame => frame.name)], { encoding: "utf8" });
  if (extraction.status !== 0) throw new Error(extraction.stderr);
  return frames.map(frame => ({
    file: path.join(destination, frame.name),
    observedAt: frame.time.toISOString(),
    offsetSeconds: Math.round((frame.time.getTime() - target) / 1000),
  }));
}

async function downloadPair(manifest) {
  const pairIndex = Math.max(0, Number(arg("pair", 1)) - 1);
  const pair = manifest.pairs[pairIndex];
  if (!pair) throw new Error(`Manifest has no pair ${pairIndex + 1}.`);
  const requestedAt = arg("at");
  const eventSample = requestedAt ? { ...pair.event, observedAt: new Date(requestedAt).toISOString(), dayArchive: archiveName(new Date(requestedAt).toISOString()) } : pair.event;
  const auth = await credentials();
  const selections = [
    { kind: "event", sample: eventSample },
    ...(!hasArg("event-only") && pair.control ? [{ kind: "control", sample: pair.control }] : []),
  ];
  const extracted = [];
  for (const selection of selections) {
    const tarFile = await downloadArchive(selection.sample.dayArchive, auth);
    extracted.push({ id: pair.id, ...selection, frames: await extractNearest(tarFile, selection.sample, pair.id, selection.kind) });
  }
  await writeFile(path.join(OUT, "extracted.json"), `${JSON.stringify(extracted, null, 2)}\n`);
  return extracted;
}

async function main() {
  const manifest = hasArg("use-manifest") ? JSON.parse(await readFile(MANIFEST_PATH, "utf8")) : await buildManifest();
  console.log(JSON.stringify({
    manifest: MANIFEST_PATH,
    pairs: manifest.pairs.length,
    potentialHours: manifest.sampleWindow.potentialHours,
    firstEvent: manifest.pairs[0]?.event ?? null,
  }, null, 2));
  if (hasArg("download")) {
    const extracted = await downloadPair(manifest);
    console.log(JSON.stringify({ extractedSelections: extracted.length, frameDirectory: path.join(OUT, "frames") }, null, 2));
  }
}

main().catch(error => { console.error(error.message || error); process.exitCode = 1; });
