# Camera source inventory

Updated August 3, 2026. Only official, documented sources belong in the
production collector.

## Current integrations

| Source | Coverage | Access | Image type | Notes |
| --- | --- | --- | --- | --- |
| FAA WeatherCam | Participating US airport sites | Public API | Historical still frames | Highest validation yield so far; camera bearing is available. |
| ALERTCalifornia | About 1,275 cameras | Public ArcGIS feature service, no key | Current high-definition JPEG, refreshed about every 15 seconds | Integrated for California. Exact live pan/tilt and capture time are compared with the predicted bow direction; required credit is retained. |
| WebCOOS | 79 catalogued; 60 up and 49 with current one-minute still archives verified July 28 | Free authenticated public API | Full-resolution timestamped stills and video archives | Integrated. Published camera viewsheds screen for the predicted bow direction; views remain unreviewed until human quality grading. |

## Retired transportation-camera sources

All DOT and 511 camera integrations were removed on August 3, 2026. DelDOT,
Caltrans CWWP2, Iowa DOT, Ohio OHGO, WSDOT, 511GA, and Maryland CHART produced
imagery that was generally too road-focused, contained too little sky, or had
unknown/changeable view direction. The project is not pursuing additional
transportation-camera networks unless a future source can demonstrate
meaningfully better sky coverage and reliable camera orientation.

## Rejected or deferred leads

- CTroads developer API currently documents messages, events, and advisories,
  not cameras.
- 511NJ offers frequently refreshed public camera images, but no official
  developer feed or third-party automation terms were found. Contact NJDOT
  before integration.
- 511SC offers current public cameras but no documented developer feed was
  found; do not reverse-engineer the site without permission.
- NYS/other mesonet camera claims were not verified from an official source.
- WeatherSTEM may be useful, but automated image access and licensing need
  written confirmation.
- NPS, harbor, tourism, and coastal webcams are too fragmented for the first
  scale-up pass and often use third-party embeds with unclear automation terms.
- Undocumented NJ endpoints should not be scraped without permission.
- Illinois Travel Midwest offers XML and JPEG camera feeds at no charge, but
  requires the user to complete its Traffic Information Access/Reuse
  Registration and accept the IDOT policy. Feeds and images may be polled no
  more than once every five minutes, and IDOT attribution is required.
- Colorado COtrip's public application uses a camera service, but no current
  official third-party camera-feed documentation or reuse permission was
  located. Do not build against the application's internal endpoint without
  written confirmation from CDOT.

## Integration rule

Each source adapter must retain source, camera ID/name, capture time, distance
to the model observer, and known/calibrated bearing. Unknown or changeable PTZ
bearing lowers review-evidence quality but does not lower the meteorological
model score. Store only reviewer-selected positive frames permanently.
