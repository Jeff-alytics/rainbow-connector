# Rainbow Connector camera system: review brief

**Current as of July 29, 2026**

> Historical brief: all DOT/511 integrations described below were retired on
> August 3, 2026 because their road-focused views, limited sky coverage, and
> unreliable orientation made them poor rainbow-review evidence. The current
> collector no longer queries Maryland CHART, DelDOT, Caltrans, Iowa DOT,
> Ohio OHGO, WSDOT, or 511GA.

## What the project is trying to do

The Rainbow Connector identifies locations where sunlight, rain, solar geometry,
and observer conditions could produce a rainbow. Cameras are not used to create
GO alerts. They are used afterward to collect evidence for human review and
calibrate the detection model.

The production review order is:

1. Every GO with usable camera imagery.
2. Strong POSSIBLEs with usable imagery.
3. No routine weak-POSSIBLE review. A one-off test of 19 lower-ranked FAA
   windows produced zero bows, while a strong POSSIBLE review did catch a bow.

Candidate meteorology and review reliability remain separate. Camera distance,
direction error, view quality, and frame timing affect how seriously a human
grade should be taken, but do not alter the meteorological model score.

## Production camera integrations

| Source | Approximate coverage | Media/history | Direction knowledge | Current status and limitations |
| --- | ---: | --- | --- | --- |
| FAA WeatherCam | 960 sites, 3,474 directional cameras, 49 state/territory labels in the local catalog | Historical stills around an event, normally five frames within ±25 minutes | Published bearing per camera | Fully integrated and the highest-yield validation source. Requires a match within 35 km and no more than 50° bearing error. Coverage is geographically uneven. |
| ALERTCalifornia | About 1,275 cameras | Current high-definition JPEG, refreshed about every 15 seconds | Live pan/tilt metadata compared with predicted bow bearing | Fully integrated for California. Current-only evidence; required credit retained. |
| WebCOOS | 79 catalogued; 60 online and 49 with verified one-minute still archives | Full-resolution timestamped still/video archives | Published viewshed | Fully integrated with an authenticated public API. Direction screening is good, but human review found uneven usefulness and no confirmed bow in the initial archive batch. |
| USGS NIMS | 40 locally whitelisted cameras: 32 graded usable and 8 limited | Timestamped still-image archive, often 5- or 15-minute cadence | Usually unknown | Integrated. Many river cameras point too low or show too little sky; quality is materially weaker than FAA. |
| Maryland CHART | 553 listed cameras | Current JPEG thumbnails only | Bearing unpublished; several nearby views may be captured | Integrated as an East Coast fallback. Resolution is modest and absence of a bow is weak evidence when view direction is unknown. |
| DelDOT | 360 records | Current HLS frame captured by the AWS worker | Unknown/changeable | Integrated. Event-triggered only. Traffic cameras may point too low. |
| Caltrans CWWP2 | 3,303 in-service cameras | Current JPEG, with HLS fallback | Roadway label is not treated as actual camera bearing | Integrated without a key through official public feeds. Current-only. |
| Iowa DOT | 1,246 rows | Current JPEG, with HLS fallback | Usually unknown | Integrated through the official public ArcGIS source. Duplicate device rows are removed. |
| Ohio OHGO | 1,119 sites | Current JPEG snapshots, advertised five-second updates | PTZ/current bearing not reliably published | Integrated with a free server-side API key. |
| WSDOT | 1,700 active records | Current JPEG snapshots | Roadway direction is not treated as camera bearing | Integrated with a free server-side access code. |

Maryland CHART is handled with the FAA evidence path. The five generic DOT
sources—Delaware, California, Iowa, Ohio, and Washington—are handled by an AWS
worker that retrieves current images or extracts an HLS frame.

## Access obtained or applications submitted

### Active credentials or access

- **Ohio OHGO:** API key obtained and in production.
- **Washington WSDOT:** access code obtained and in production.
- **WebCOOS:** API token obtained and in production.
- **ARM:** account and token obtained for historical research. ARM sky imagery
  informed the science calibration but is not a useful nationwide production
  camera network.

All credentials are server-side and must not be exposed in camera jobs,
review-page payloads, or documentation.

### Submitted and awaiting approval/access

- **Georgia 511:** developer application submitted; approval/key pending.
- **North Carolina DriveNC:** developer access request submitted; response/key
  pending.
- **VDOT 511:** free third-party video-feed agreement/request sent by email;
  approval pending. The public layer showed about 1,679 active cameras with
  snapshot and HLS fields, making this the highest-priority pending expansion.

### Outreach status that needs confirmation

- **WeatherSTEM:** outreach was discussed and appears to have been sent, but
  the repository does not contain a definitive response or permission record.
  Automated image access and licensing remain unresolved.
- **FL511:** the public developer onboarding link was broken. A support request
  was recommended, but there is no definitive local record of an issued key.
  Florida remains a very high-value target because convective sun showers are
  frequent.

## High-priority access opportunities not yet integrated

| Source | Value | Remaining obstacle |
| --- | --- | --- |
| FL511 | Statewide Florida; likely excellent rainbow climatology | Broken onboarding/key page; contact support |
| 511GA | Statewide Georgia and Travel-IQ camera schema | Application pending |
| DriveNC | Statewide North Carolina; Travel-IQ schema | Application pending |
| VDOT 511 | 1,679 verified active cameras, strong Mid-Atlantic coverage | Signed agreement approval pending |
| 511NY | Statewide images and video | Developer application and attribution/access terms |
| New England 511 | Maine, New Hampshire, Vermont in one system | Developer documentation returns 404; vendor/support contact needed |
| PennDOT/511PA | More than 950 cameras | Data-feed request and video-sharing agreement |
| Illinois Travel Midwest | Multi-agency XML/JPEG feed | Registration, five-minute polling limit, IDOT attribution |
| Oregon TripCheck | Official CCTV API with current still URLs | Self-service developer account/subscription |
| Alaska, Arizona, Idaho, Utah, Wisconsin 511 | Travel-IQ developer APIs | Self-service accounts/keys; not yet registered in the project |
| New York City DOT TMC | Very dense urban coverage | Data-sharing agreement |

## Research-only and rejected/deferred sources

- **ARM atmospheric observatories:** extremely useful for learning the
  meteorological envelope and produced confirmed historical bows, but sites are
  sparse and several sky-camera products were gray, distorted, or hard to
  interpret. Not a production coverage solution.
- **GLOBE Observer:** one rainbow found in a 60-item global review, but uploads
  are opportunistic, heterogeneous, and not real-time enough for dependable
  collection.
- **IEM camera imagery:** poor resolution and many fully gray/bad-weather
  frames; rejected as a primary review source.
- **USGS NIMS:** retained as a low-cost fallback, but much imagery lacks enough
  horizon sky for strong negative evidence.
- **EarthCam, Roundshot, tourism, harbor, NPS, wildlife, and trail cameras:**
  fragmented coverage, unclear automation/licensing, third-party embeds,
  inconsistent archives, or no practical application mechanism. Roundshot has
  US installations but no clear general US data-access program was found.
- **511NJ, 511SC, Colorado COtrip, CTroads:** public cameras exist, but a
  documented reusable developer feed or permission was not established. Do not
  reverse-engineer private site endpoints.

## Current capture architecture

1. The nationwide detector writes GO and POSSIBLE artifacts every five minutes.
2. Vercel stores review events and queries FAA, Maryland CHART, USGS NIMS,
   WebCOOS, and ALERTCalifornia.
3. An AWS worker polls for recent DOT-camera jobs and handles current JPEG/HLS
   retrieval for Delaware, California, Iowa, Ohio, and Washington.
4. Evidence collection is event-triggered rather than continuous. Typical
   maximum matching distance is 35 km.
5. The review page places GO imagery first, then strong POSSIBLE imagery.
6. Human reviewers grade Rainbow, No rainbow, Possible, Obscured, or Bad view.
7. Only reviewer-selected frames are placed in the permanent confirmed-rainbow
   gallery record. Event records have finite retention, but a formal blob
   cleanup policy for rejected/unselected evidence should be audited.

## What validation has taught us

- FAA imagery is the most productive source because it combines history,
  multiple compass views, useful sky coverage, and known bearings.
- Yield falls sharply down the ranked list. A test of 19 lower-score POSSIBLE
  FAA windows produced zero bows.
- At least one strong POSSIBLE review produced a confirmed bow, supporting
  continued review of the strong tier.
- Camera availability should not justify reviewing weak meteorological
  candidates.
- Direction error is essential. A camera near a candidate but facing away from
  the predicted anti-solar direction cannot provide meaningful negative
  evidence.
- Unknown/PTZ traffic-camera direction and low sky fraction are the largest
  weaknesses of the DOT expansion.
- Confirmed social reports in Baltimore, Dundalk, Person County, and Idaho
  exposed geographic gaps or imperfect candidate placement. The Idaho event
  was strongly detected and eventually reached GO, but no FAA camera matched
  it.
- East Coast, Southeast, and populated inland coverage remain the most
  important gaps despite Maryland CHART and the initial DOT additions.

## Questions for Claude

Please review this camera strategy and recommend:

1. The five highest-value camera networks to pursue next, prioritizing
   historical frames, reliable bearing/FOV, sky coverage, geographic gaps,
   legal reuse, and low integration cost.
2. Better non-transportation sources with official APIs or explicit
   noncommercial reuse terms.
3. A scalable method to estimate or calibrate camera bearing, field of view,
   horizon sky fraction, and PTZ changes from the imagery itself.
4. How to rank camera evidence using meteorological score, camera distance,
   direction error, frame timing, image quality, and source reliability without
   conflating evidence quality with rainbow likelihood.
5. Whether lightweight computer vision should pre-screen frames, and how to
   prevent it from discarding faint or partial bows.
6. Improvements to the Vercel/AWS division of work that preserve fast page
   loads and low costs as more state feeds are added.
7. A retention and rights-management policy for temporary negatives,
   reviewer-selected positives, attribution, and source-specific restrictions.
8. Any camera sources or architectural risks that this inventory has missed.

Please distinguish verified facts, likely opportunities, and suggestions that
require permission or further validation.
