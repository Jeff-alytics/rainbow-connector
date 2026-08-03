"""Claude verification: does the 3-45 km prefilter drop physically valid assignments?

Runs the builder's exact compatibility loop with the frozen bounds [3,45] and with
permissive bounds [0,60], over all bow targets, and reports any component that
swath_contains accepts but the frozen prefilter would have excluded.
"""
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
for directory in (ROOT, ROOT / "worker"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from opportunity_ledger import observer_swath, swath_contains
from scripts.evaluate_storm_object_replay import distance_to_object_km, nearest_scan
from scripts.storm_object_replay import load_artifact, parse_utc

SITE_ID = re.compile(r"^site(\d+)-")  # fixed regex
sources = json.loads((ROOT / "validation/storm-object-replay-gate/scan-artifact-manifest.json")
                     .read_text(encoding="utf-8"))["files"]
targets = json.loads((ROOT / "validation/causal-replay-gate/bow-evaluation-targets.json")
                     .read_text(encoding="utf-8"))["targets"]
sites = {str(item["siteId"]): item
         for item in json.loads((ROOT / "validation/faa/sites.json").read_text(encoding="utf-8-sig"))}

frozen_low, frozen_high = 3.0, 45.0
wide_low, wide_high = 0.0, 60.0

rows = []
started = time.time()
for old in targets:
    match = SITE_ID.match(old["eventId"])
    if not match or match.group(1) not in sites:
        rows.append({"eventId": old["eventId"], "status": "site_missing"})
        continue
    site = sites[match.group(1)]
    source = nearest_scan(old["targetAt"], sources)
    if source is None:
        rows.append({"eventId": old["eventId"], "status": "no_scan_within_5min"})
        continue
    artifact = load_artifact(Path(source["path"]))
    sidecar = {"grid": artifact["grid"]}
    observed_at = parse_utc(artifact["observedAt"])
    lat, lon = float(site["latitude"]), float(site["longitude"])
    inside_frozen, inside_wide, distances = [], [], []
    for item in artifact["stormObjects"]:
        distance = distance_to_object_km(lat, lon, item, artifact["grid"])
        if not wide_low <= distance <= wide_high:
            continue
        swath = observer_swath(item, sidecar, observed_at)
        contained, swath_distance = swath_contains(swath, lat, lon)
        if not contained:
            continue
        distances.append(round(distance, 2))
        inside_wide.append((item["componentId"], round(distance, 2)))
        if frozen_low <= distance <= frozen_high:
            inside_frozen.append((item["componentId"], round(distance, 2)))
    dropped = [c for c in inside_wide if c not in inside_frozen]
    rows.append({"eventId": old["eventId"], "site": site.get("siteName"),
                 "totalObjects": len(artifact["stormObjects"]),
                 "compatibleFrozen": len(inside_frozen), "compatibleWide": len(inside_wide),
                 "droppedByPrefilter": dropped,
                 "envelopeDistances": sorted(distances)})
    print(f"{old['eventId']:34s} frozen={len(inside_frozen)} wide={len(inside_wide)} "
          f"dropped={len(dropped)} dist={sorted(distances)[:4]}", flush=True)

out = Path(r"C:\Users\jeffm\AppData\Local\Temp\claude\C--Users-jeffm\751564b5-3548-4f35-a2f7-f5ebf93b2cfe\scratchpad\prefilter-verification.json")
out.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")

def status(row):
    n = row.get("compatibleFrozen")
    return "unique" if n == 1 else "ambiguous" if n and n > 1 else "none"

from collections import Counter
print()
print("frozen-bound assignment counts:", dict(Counter(status(r) for r in rows if "compatibleFrozen" in r)))
wide_counts = Counter(("unique" if r.get("compatibleWide") == 1 else "ambiguous" if r.get("compatibleWide") else "none")
                      for r in rows if "compatibleWide" in r)
print("wide-bound assignment counts:  ", dict(wide_counts))
print("targets where prefilter dropped a swath-compatible object:",
      sum(1 for r in rows if r.get("droppedByPrefilter")))
print("elapsed", round(time.time() - started, 1), "s")
