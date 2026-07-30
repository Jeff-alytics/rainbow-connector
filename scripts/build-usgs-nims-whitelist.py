"""Build the compact production whitelist for reviewed USGS NIMS cameras."""

import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "validation" / "usgs-nims-pilot"
OUTPUT = ROOT / "usgs-nims-sites.json"
API = "https://api.waterdata.usgs.gov/nims/v0/cameras"
HEADERS = {"User-Agent": "RainbowConnector/1.0 (USGS NIMS whitelist builder)"}
OVERRIDES = {
    "SC_Pee_Dee_River_at_Georgetown": "limited",
    "WI_Green_Lake_Inlet_at_HWY_A_near_Green_Lake_WETLAND": "limited",
}


def main():
    grades = json.loads((PILOT / "grades.json").read_text(encoding="utf-8"))
    screen = json.loads((PILOT / "camera-screen.json").read_text(encoding="utf-8"))
    catalog = requests.get(API, headers=HEADERS, timeout=60).json()
    by_id = {camera["camId"]: camera for camera in catalog}
    rows = []
    for reviewed in screen["cameras"]:
        grade = OVERRIDES.get(reviewed["id"], grades.get(reviewed["id"]))
        if grade not in {"usable", "limited"}:
            continue
        camera = by_id.get(reviewed["id"])
        if not camera or not camera.get("overlayDir"):
            continue
        rows.append({
            "id": reviewed["id"],
            "siteId": camera.get("nwisId"),
            "name": reviewed["name"],
            "state": reviewed["state"],
            "lat": reviewed["lat"],
            "lon": reviewed["lon"],
            "intervalMinutes": reviewed["intervalMinutes"],
            "viewQuality": grade,
            "skyScore": reviewed["skyScore"],
            "horizonSkyPct": reviewed["horizonSkyPct"],
            "imageBaseUrl": camera["overlayDir"],
            "pageUrl": f"https://waterdata.usgs.gov/monitoring-location/{camera.get('nwisId')}/" if camera.get("nwisId") else "https://api.waterdata.usgs.gov/docs/nims/",
        })
    rows.sort(key=lambda row: (row["viewQuality"] != "usable", -row["horizonSkyPct"], row["id"]))
    OUTPUT.write_text(json.dumps(rows, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT.resolve()), "cameras": len(rows),
                      "usable": sum(row["viewQuality"] == "usable" for row in rows),
                      "limited": sum(row["viewQuality"] == "limited" for row in rows)}))


if __name__ == "__main__":
    main()
