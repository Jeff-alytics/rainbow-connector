# Ingredients Map — parity guidance (remake vs. approved mockup)

Target: make the live "Rainbow Ingredients Right Now" map (`index.html`, `candMap`) match
`docs/mockups/ingredients-map-baltimore.html` + `baltimore-scan-data.json` (the approved look).
Reference renders: `baltimore-replay-national.png` (this is the acceptance image).

Current state: ~75% there. The MapLibre **paint specs already match the mockup exactly**
(`index.html:1304-1309` — same color ramp, same opacities, same line styles). Do not touch them.
The remaining gap is in the **data fed to those layers** and the **framing**. Three fixes, in
priority order:

---

## 1. Golden band is blocky and flat — fix `solarIngredientLayers()` (index.html:1410-1423)

The mockup band is a smooth graduated wash; the live one is a flat tan slab with staircase
edges. Three data differences cause this:

| | Live app | Mockup (target) |
|---|---|---|
| Grid cell size | 1.0° | **0.35°** |
| Cell opacity | fixed `0.3` | **feathered: `0.3 × min(1, (42 − elevation) / 14)`** |
| Coastline | cell dropped if center off-land | cell rectangle **clipped** to the CONUS polygon |

The feather formula is exact — recovered from the frozen mockup data (e=28.35→op 0.293,
e=33.69→0.178, e=39.83→0.046; all fit `0.3·(42−e)/14`, clamped to [0, 0.3]). This is what
makes the band fade out gracefully toward the 42° iso-line instead of ending in a hard edge.
The low-sun (terminator) side stays at full 0.3 — correct, don't feather that side.

Concretely in `solarIngredientLayers`:
- loop step `1` → `0.35` (both lat and lon); cell half-width `.5` → `.175`
- `properties: { elevation, opacity: .3 }` → `properties: { elevation, opacity: 0.3 * Math.min(1, (42 - elevation) / 14) }`
- Perf: ~12k `solarReadout` calls instead of ~1.5k — still trivially fast, but this runs on
  every `renderIngredients`; if it ever matters, cache by scanTime (it's called with the same
  scan repeatedly).
- Coastline polish (optional, do last): the mockup intersected each cell rect with the
  `us-nation` polygon, so coastal cells are partial slivers rather than missing. At 0.35° the
  center-test blockiness is already 3× less noticeable; ship the grid+feather first and only
  add true clipping if the coast still looks ragged next to the acceptance PNG.

## 2. Rain / aligned layers render as speckle — dissolve into footprints

In the mockup, `rain` is **one Feature with a MultiPolygon of 288 dissolved blobs** and
`aligned` is one MultiPolygon of 218 blobs. The live map is drawing thousands of individual
per-cell squares from `/api/map-tiers`, and the 1.1px outline layer around *every tiny cell*
is what produces the gray speckled look at national zoom — the outlines visually swamp the fills.

Fix on the server side (worker that builds `map-tiers`): dissolve adjacent radar cells into
merged polygons before shipping. The run-length encoding in
`docs/rain-footprint-sidecar-v1.md` is the natural input — each run `[row, c0, c1, tier]` is
a rectangle; union rectangles that touch (share an edge) across rows into polygons. Any
rectangle-merge/greedy-meshing approach is fine; it does not need to be geometrically perfect,
it needs to eliminate interior edges so the outline only traces each blob's perimeter.

Payload sanity: quantize coordinates to ~3 decimals (≈100 m) after dissolving. The mockup's
frozen national-scale rain+aligned+band JSON is ~3.6 MB un-gzipped; a live dissolved payload
should come in well under that.

Quick client-side stopgap if the server fix has to wait: hide `ingredient-rain-line` /
`ingredient-aligned-line` when feature counts are huge. That kills the speckle but loses the
mockup's outlined-blob look — treat it as temporary only.

## 3. Framing — the map shows half of Canada and Mexico

Mockup: `center: [-95.5, 37.6], zoom: 3.9`, map height **560px**, border-radius 1.25rem.
Live: `center: [-96, 38.5], zoom: 3` (index.html:1299) and a shorter container.
CONUS should fill the frame like in `baltimore-replay-national.png`. Update the constructor
and the `#candmap` CSS height.

## 4. Minor chrome (only after 1–3)

- Legend sun chip: mockup is a gradient chip `linear-gradient(135deg, #e89b2e, #f0d060)`;
  live one is flat yellow. Rain/aligned chips already match (`index.html:327`).
- Bow pins: white 27px circle, emoji content, ring via box-shadow —
  violet `oklch(0.55 0.21 305 / .85)` for GO 🌈, amber `oklch(0.68 0.15 70 / .85)` for
  possible ⛅. See `.bow-pin` in the mockup HTML.
- Mockup has no zoom control; the live app keeping its NavigationControl is fine.

---

## How to verify (acceptance loop)

1. Build a throwaway test page (or query param) that feeds the frozen
   `docs/mockups/baltimore-scan-data.json` into `renderIngredients` — property names differ
   (`e`/`op` in the fixture vs `elevation`/`opacity` in the app), so map them when loading.
2. Screenshot at 1400px wide and compare side-by-side against
   `docs/mockups/baltimore-replay-national.png`. The bands, blue rain blobs with outlines,
   and green aligned slivers hugging the rain edges should be visually indistinguishable.
3. Then load live data and confirm: no speckle at zoom 3.9, band fades smoothly at its
   eastern/high-sun edge, coastline not staircased.

Do NOT restyle by eye — the paint layers are already correct; if something looks wrong after
these fixes, the bug is in the data pipeline, not the styles.
