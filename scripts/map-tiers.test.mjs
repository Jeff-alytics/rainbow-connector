import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { validateMapTiers } from "../api/map-tier-common.mjs";

test("map tier contract requires timestamps, version, and both geometries",()=>{
  const payload={schemaVersion:"map-tiers.v1",scanTime:"2026-07-29T23:00:00Z",generatedAt:"2026-07-29T23:00:10Z",
    tierRuleVersion:"map-tiers-2026-07-v1",rain:{geometry:{type:"GeometryCollection",geometries:[]}},
    aligned:{geometry:{type:"GeometryCollection",geometries:[]}}};
  assert.equal(validateMapTiers(payload),"");
  assert.match(validateMapTiers({...payload,scanTime:"bad"}),/scanTime/);
});

test("public map keeps pins independent and matches ingredient scans exactly",async()=>{
  const page=await readFile(new URL("../index.html",import.meta.url),"utf8");
  assert.match(page,/function sameScan\(a,b\)/);
  assert.match(page,/attempt<6/);
  assert.match(page,/ingredients layer unavailable/);
  assert.match(page,/Pins are spots the Connector is actively tracking/);
  assert.match(page,/The sun is low enough/);
  assert.match(page,/center: \[-95\.5, 37\.6\], zoom: 3\.9/);
  assert.doesNotMatch(page,/cluster:\s*true/);
  assert.doesNotMatch(page,/querySourceFeatures\("candidates"\)/);
  assert.match(page,/new maplibregl\.Marker\(\{ element: el \}\)\.setLngLat\(\[c\.lon, c\.lat\]\)/);
  assert.match(page,/step=\.35,half=step\/2/);
  assert.match(page,/\.3\*Math\.min\(1,\(42-elevation\)\/14\)/);
  assert.match(page,/elevation>=-1&&elevation<=42/);
  assert.match(page,/Math\.min\(1,\(elevation\+1\)\/3\)/);
  assert.match(page,/mapFixture/);
});

test("endpoint advertises fresh and stale CDN behavior",async()=>{
  const endpoint=await readFile(new URL("../api/map-tiers.mjs",import.meta.url),"utf8");
  assert.match(endpoint,/max-age=60, stale-while-revalidate=60/);
  assert.match(endpoint,/age>20\*60000/);
});
