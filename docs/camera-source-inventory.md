# Camera source inventory

Verified July 28, 2026. Only official, documented sources belong in the
production collector. A traffic direction label is not necessarily the live
PTZ camera bearing, so it must not be treated as strong directional evidence.

## Current integrations

| Source | Coverage | Access | Image type | Notes |
| --- | --- | --- | --- | --- |
| FAA WeatherCam | Participating US airport sites | Public API | Historical still frames | Highest validation yield so far; camera bearing is available. |
| ALERTCalifornia | About 1,275 cameras | Public ArcGIS feature service, no key | Current high-definition JPEG, refreshed about every 15 seconds | Integrated for California. Exact live pan/tilt and capture time are compared with the predicted bow direction; required credit is retained. |
| WebCOOS | 79 catalogued; 60 up and 49 with current one-minute still archives verified July 28 | Free authenticated public API | Full-resolution timestamped stills and video archives | Integrated. Published camera viewsheds screen for the predicted bow direction; views remain unreviewed until human quality grading. |
| Maryland CHART | 553 listed cameras | Public JSON feed, no key | Current JPEG thumbnails | Integrated as the first East Coast fallback; no historical imagery. |
| DelDOT | 360 current camera records | Public official ArcGIS layer | Live HLS frame captured in AWS | Integrated; nearest three cameras within 35 km are captured only for pending GO events. Camera bearing is unknown. |
| Caltrans CWWP2 | 3,303 in-service cameras verified July 28 | Public official district JSON feeds, no key and no charge | Current JPEG; HLS where reported | Integrated with image-first capture and video fallback. The published roadway direction is not treated as camera bearing. |
| Iowa DOT | 1,246 camera rows verified July 28 | Public official ArcGIS feature service, no credentials | Current JPEG; HLS where available | Integrated with image-first capture and video fallback. Duplicate device rows are removed before matching. |
| Ohio OHGO | 1,119 camera sites verified July 28 | Free public API key stored server-side | Current JPEG snapshots | Integrated. OHGO documents five-second snapshot updates; the collector still captures only for pending GO events. |
| WSDOT | 1,700 active camera records verified July 28 | Free access code stored server-side | Current JPEG snapshots | Integrated. The access code is used only for the server-side catalog request and is never included in review jobs or image URLs. |

## Verified next sources

| Priority | Source | Coverage | Access | Image type | Decision |
| --- | --- | --- | --- | --- | --- |
| 1 | VDOT 511 | 1,679 active cameras verified July 28 | Free third-party user agreement requested by email | Current JPEG snapshot and HLS | Strongest immediate expansion. Public catalog is technically excellent, but VDOT requires an agreement before third-party use. |
| 2 | FL511 | Florida statewide | Developer access currently broken; support request required | Camera API remains online but rejects requests without a key | Best expected rainbow yield because of frequent convective sun showers. |
| 3 | 511GA | Georgia statewide; 4,043 active geolocated cameras at integration | Approved developer key; integrated | Direct PNG snapshot returned by each enabled camera-view URL | Catalog is cached for 30 minutes; published direction is context only and does not make a negative grade conclusive. |
| 4 | DriveNC | Statewide | Free developer key; 10 calls per 60 seconds | Camera view URL; video availability varies | Clean official API and the same adapter family as Georgia. |
| 5 | 511NY | New York statewide | Free developer-access application and key | Current image URL and live `VideoUrl` | Explicitly supports camera images/video; public attribution and access agreement apply. |
| 6 | New England 511 | Maine, New Hampshire, Vermont | Camera API exists but developer documentation currently returns 404; support contact needed | Camera views | One approval could cover three states, but access terms need confirmation. |
| 7 | PennDOT | More than 950 cameras | Free data-feed request plus video-sharing agreement | HTTP stills or streaming video | High coverage; approval normally takes longer and public attribution is required. |
| 8 | NYC DOT TMC | New York City | Contact TMC and sign data-sharing agreement | Camera feed | Potentially denser than 511NY in the city; permission required. |

## Official links

- Maryland CHART: https://chart.maryland.gov/DataFeeds/GetDataFeeds
- FL511 developer API: https://fl511.com/developers/doc
- 511GA developer API: https://511ga.org/developers/doc
- 511GA cameras schema: https://511ga.org/help/endpoint/cameras
- DelDOT camera map: https://deldot.gov/map/index.shtml?tab=Cameras
- DelDOT ArcGIS camera layer: https://enterprise.firstmaptest.delaware.gov/arcgis/rest/services/Transportation/DE_TMC_Traffic_Feeds/MapServer/1
- DriveNC developer API: https://www.drivenc.gov/developers/doc
- 511NY developer API: https://www.511ny.org/developers/help
- VDOT third-party video access: https://www.vdot.virginia.gov/news-events/media/
- New England 511: https://newengland511.org/
- PennDOT data-feed request: https://www.pa.gov/services/penndot/request-access-to-transportation-related-data-feeds

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

The generic DOT collector now accepts either a current image URL or a stream
URL. It tries the still image first for speed and falls back to HLS when the
still is unavailable. A failure in one state catalog does not prevent another
state's camera evidence from being collected.
