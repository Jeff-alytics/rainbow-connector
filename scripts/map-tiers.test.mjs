import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import { validateMapTiers } from "../api/map-tier-common.mjs";

function functionSource(html, name, nextName) {
  const start = html.indexOf(`function ${name}`);
  const end = html.indexOf(`function ${nextName}`, start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  return html.slice(start, end);
}

class FakeClassList {
  constructor(element) { this.element = element; }
  values() { return new Set(String(this.element.className || "").split(/\s+/).filter(Boolean)); }
  write(values) { this.element.className = [...values].join(" "); }
  add(...names) { const values = this.values(); names.forEach(name => values.add(name)); this.write(values); }
  toggle(name, force) {
    const values = this.values(), enabled = force === undefined ? !values.has(name) : !!force;
    if (enabled) values.add(name); else values.delete(name);
    this.write(values);
    return enabled;
  }
  contains(name) { return this.values().has(name); }
}

class FakeMarker {
  constructor({ element }) {
    this.element = element;
    element.classList.add("maplibregl-marker");
  }
  setLngLat(coords) { this.coords = [...coords]; return this; }
  addTo(map) { this.map = map; return this; }
  getLngLat() { return { lng: this.coords[0], lat: this.coords[1] }; }
  getElement() { return this.element; }
  remove() { this.removed = true; }
}

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
  assert.match(page,/step=\.35,half=step\/2/);
  assert.match(page,/\.3\*Math\.min\(1,\(42-elevation\)\/14\)/);
  assert.match(page,/elevation>=-1&&elevation<=42/);
  assert.match(page,/Math\.min\(1,\(elevation\+1\)\/3\)/);
  assert.match(page,/mapFixture/);
});

test("candidate pins retain MapLibre positioning and update to exact artifact coordinates",async()=>{
  const page=await readFile(new URL("../index.html",import.meta.url),"utf8");
  const source=functionSource(page,"updateCandidatePinMarkers","renderCandidatePins");
  const first={id:"candidate-1",lat:39.2904,lon:-76.6122,verdict:"go",label:"Baltimore"};
  const context={
    candMapLoaded:true,
    candidateArtifact:{candidates:[first]},
    visibleMapCandidates:artifact=>artifact.candidates,
    isFiniteCoord:(lat,lon)=>Number.isFinite(lat)&&Number.isFinite(lon),
    candPinMarkers:new Map(),
    activeCandidateId:null,
    document:{createElement:()=>{const element={className:"",textContent:"",title:"",addEventListener(){}};element.classList=new FakeClassList(element);return element;}},
    maplibregl:{Marker:FakeMarker},
    candMap:{},
    selectCandidate(){},
  };
  vm.runInNewContext(`${source}; updateCandidatePinMarkers();`,context);
  const marker=context.candPinMarkers.get("candidate-1");
  assert.deepEqual(marker.coords,[-76.6122,39.2904]);
  assert.equal(marker.getElement().classList.contains("maplibregl-marker"),true);
  assert.equal(marker.getElement().classList.contains("bow-pin"),true);

  context.candidateArtifact={candidates:[{...first,lat:39.2911,lon:-76.6107,verdict:"watch"}]};
  vm.runInNewContext("updateCandidatePinMarkers();",context);
  assert.deepEqual(marker.coords,[-76.6107,39.2911]);
  assert.equal(marker.getElement().classList.contains("maplibregl-marker"),true);
  assert.equal(marker.getElement().classList.contains("maybe"),true);
});

test("endpoint advertises fresh and stale CDN behavior",async()=>{
  const endpoint=await readFile(new URL("../api/map-tiers.mjs",import.meta.url),"utf8");
  assert.match(endpoint,/max-age=60, stale-while-revalidate=60/);
  assert.match(endpoint,/age>20\*60000/);
});
