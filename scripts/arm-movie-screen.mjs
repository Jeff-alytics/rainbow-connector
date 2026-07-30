#!/usr/bin/env node

import { createWriteStream } from "node:fs";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { pipeline } from "node:stream/promises";
import { spawnSync } from "node:child_process";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUT = path.join(ROOT, "validation", "arm-lamont");
const MOVIE_STREAM = "sgpasimovieC1.a1";
const REAL_SECONDS_PER_VIDEO_SECOND = 375; // 15 s source cadence rendered at 25 fps.

function arg(name, fallback = null) {
  const index = process.argv.indexOf(`--${name}`);
  return index < 0 ? fallback : process.argv[index + 1];
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

async function movieInfo(observedAt) {
  const target = new Date(observedAt);
  const days = [target, new Date(target.getTime() - 86_400_000)];
  const sources = [];
  for (const date of days) {
    const day = date.toISOString().slice(0, 10).replaceAll("-", "");
    const url = new URL("https://adc.arm.gov/elastic/file_info/_search");
    url.searchParams.set("q", `file_name.keyword:${MOVIE_STREAM}.${day}*`);
    url.searchParams.set("size", "5");
    const response = await fetch(url, { headers: { "User-Agent": "rainbow-connector-validation/1.0" } });
    if (!response.ok) throw new Error(`ARM movie lookup failed: HTTP ${response.status}`);
    const data = await response.json();
    sources.push(...(data.hits?.hits || []).map(hit => hit._source));
  }
  const eligible = sources
    .filter(source => movieStart(source.file_name) <= target)
    .sort((a, b) => movieStart(a.file_name) - movieStart(b.file_name));
  const source = eligible.at(-1);
  if (!source) throw new Error(`No ARM ASI movie covers ${observedAt}.`);
  return source;
}

async function downloadMovie(info, auth) {
  const directory = path.join(OUT, "movies");
  await mkdir(directory, { recursive: true });
  const destination = path.join(directory, info.file_name);
  try { if ((await stat(destination)).size === info.file_size) return destination; } catch {}
  const url = new URL("https://adc.arm.gov/armlive/saveData");
  url.searchParams.set("user", auth);
  url.searchParams.set("file", info.file_name);
  const response = await fetch(url, { headers: { "User-Agent": "rainbow-connector-validation/1.0" } });
  if (!response.ok || !response.body) throw new Error(`ARM movie download failed: HTTP ${response.status}`);
  await pipeline(response.body, createWriteStream(destination));
  return destination;
}

function movieStart(fileName) {
  const match = fileName.match(/(20\d{6})\.(\d{6})\.mpg$/);
  if (!match) throw new Error(`Cannot parse movie start time: ${fileName}`);
  const day = match[1], time = match[2];
  return new Date(`${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6, 8)}T${time.slice(0, 2)}:${time.slice(2, 4)}:${time.slice(4, 6)}Z`);
}

function stamp(date) {
  return date.toISOString().replaceAll(/[-:]/g, "").slice(0, 15);
}

async function extractScreen(movie, info, target, candidate) {
  const directory = path.join(OUT, "movie-frames", candidate);
  await mkdir(directory, { recursive: true });
  const start = movieStart(info.file_name);
  const offsets = [-600, -300, 0, 300, 600];
  const frames = [];
  for (const offset of offsets) {
    const observedAt = new Date(target.getTime() + offset * 1000);
    const videoSeconds = (observedAt.getTime() - start.getTime()) / 1000 / REAL_SECONDS_PER_VIDEO_SECOND;
    if (videoSeconds < 0) continue;
    const output = path.join(directory, `${MOVIE_STREAM}.${stamp(observedAt)}.jpg`);
    const result = spawnSync("ffmpeg", [
      "-loglevel", "error", "-y", "-ss", videoSeconds.toFixed(3), "-i", movie,
      "-frames:v", "1", "-q:v", "2", output,
    ], { encoding: "utf8" });
    if (result.status !== 0) throw new Error(result.stderr || "ffmpeg movie extraction failed");
    frames.push({ file: output, observedAt: observedAt.toISOString(), offsetSeconds: offset });
  }
  if (!frames.length) throw new Error("No movie frames were within range.");

  const review = path.join(OUT, "review", `${candidate}-movie-screen.jpg`);
  await mkdir(path.dirname(review), { recursive: true });
  const inputs = frames.flatMap(frame => ["-i", frame.file]);
  const labels = frames.map((_, index) => `[${index}:v]scale=320:320[${String.fromCharCode(97 + index)}]`).join(";");
  const streams = frames.map((_, index) => `[${String.fromCharCode(97 + index)}]`).join("");
  const layout = "0_0|320_0|640_0|160_320|480_320";
  const sheet = spawnSync("ffmpeg", [
    "-loglevel", "error", "-y", ...inputs,
    "-filter_complex", `${labels};${streams}xstack=inputs=${frames.length}:layout=${layout}[out]`,
    "-map", "[out]", "-frames:v", "1", "-q:v", "6", review,
  ], { encoding: "utf8" });
  if (sheet.status !== 0) throw new Error(sheet.stderr || "ffmpeg contact sheet failed");
  return { frames, review };
}

async function main() {
  const pairNumber = Number(arg("pair"));
  const at = arg("at");
  const candidate = arg("candidate", `movie-pair-${pairNumber}`);
  if (!Number.isInteger(pairNumber) || !at) throw new Error("Usage: --pair N --at ISO_TIME [--candidate NAME]");
  const manifest = JSON.parse(await readFile(path.join(OUT, "manifest.json"), "utf8"));
  const pair = manifest.pairs[pairNumber - 1];
  if (!pair) throw new Error(`Manifest has no pair ${pairNumber}.`);
  const target = new Date(at);
  if (Number.isNaN(target.getTime())) throw new Error(`Invalid --at value: ${at}`);
  const info = await movieInfo(target.toISOString());
  const movie = await downloadMovie(info, await credentials());
  const extracted = await extractScreen(movie, info, target, candidate);
  const horizonReview = path.join(OUT, "review", `${candidate}-horizon-screen.jpg`);
  const dewarp = spawnSync("uv", [
    "run", "python", path.join(ROOT, "scripts", "arm-movie-review.py"),
    "--candidate", candidate,
  ], { encoding: "utf8", cwd: ROOT });
  if (dewarp.status !== 0) {
    throw new Error(dewarp.stderr || dewarp.stdout || "Horizon-view conversion failed");
  }
  const result = {
    pairId: pair.id,
    candidate,
    centerTime: target.toISOString(),
    movie: { file: movie, bytes: info.file_size, sourceFile: info.file_name },
    ...extracted,
    horizonReview,
  };
  await writeFile(path.join(OUT, "movie-screen.json"), `${JSON.stringify(result, null, 2)}\n`);
  console.log(JSON.stringify(result, null, 2));
}

main().catch(error => { console.error(error.message || error); process.exitCode = 1; });
