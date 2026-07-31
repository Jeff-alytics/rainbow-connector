# Lamont all-sky-image feasibility study

This study uses ARM's `sgpasiskyimageC1.a1` archive at the Southern Great
Plains C1 facility. The initial sampler uses hourly Open-Meteo archive data to
find promising camera periods and matched no-rain controls. It is a camera
feasibility test, not yet a historical reconstruction of the production radar
detector.

```powershell
node scripts/arm-lamont-feasibility.mjs
node scripts/arm-lamont-feasibility.mjs --use-manifest --download
```

Daily ARM image archives are approximately 200-250 MB. The downloader starts
with one event/control pair and extracts the five nearest images from each day.

After confirming image orientation and quality, add calibrated anti-solar
overlays and historical NEXRAD/MRMS reconstruction for the detector-accuracy
study.
