# DART collection API findings

Checked 17 November 2026 against primary sources. Scope assumed: DART services on the Malahide/Howth branches and south through Bray to Greystones, including intermediate stations and short workings.

## Recommended source

Use Irish Rail's own realtime XML service as the primary source. Its documented DART selector is `TrainType=D`; the station selector is `StationType=D`. The running list includes journeys already underway and those starting within ten minutes. Journeys have a train code and service date. Station boards include expected and scheduled arrival/departure times, delay, direction and train type, with a 5–90 minute lookahead. Irish Rail describes the feed as based on signalling and schedules, with weaker realtime coverage in listed areas. It does not promise GPS accuracy, a refresh interval, retention, or service support. [Irish Rail API overview](https://api.irishrail.ie/realtime/index.htm)

Relevant endpoints:

```text
https://api.irishrail.ie/realtime/realtime.asmx/getCurrentTrainsXML_WithTrainType?TrainType=D
https://api.irishrail.ie/realtime/realtime.asmx/getAllStationsXML_WithStationType?StationType=D
https://api.irishrail.ie/realtime/realtime.asmx/getStationDataByCodeXML_WithNumMins?StationCode=mhide&NumMins=90
https://api.irishrail.ie/realtime/realtime.asmx/getTrainMovementsXML?TrainId=SERVICE_CODE&TrainDate=SERVICE_DATE
```

Names and parameters are case sensitive. Station queries can contain other train types even at DART stations, so retain only confirmed DART journeys. Trim surrounding whitespace when comparing train codes across endpoints. Do not filter purely by a geographic bounding box or destination name. These filtering and normalization recommendations are collection design decisions.

## Available measurements

The position response contains `TrainStatus`, `TrainLatitude`, `TrainLongitude`, `TrainCode`, `TrainDate`, `PublicMessage` and `Direction`. It has no documented speed field or position measurement timestamp. [Position response schema](https://api.irishrail.ie/realtime/realtime.asmx?op=getCurrentTrainsXML)

The movement response contains each location's code, name, order and type, origin/destination, `ScheduledArrival`, `ScheduledDeparture`, `ExpectedArrival`, `ExpectedDeparture`, `Arrival`, `Departure`, `AutoArrival`, `AutoDepart` and `StopType`. Notably, the operation schema exposes expected times beyond the abbreviated overview. [Movement response schema](https://api.irishrail.ie/realtime/realtime.asmx?op=getTrainMovementsXML)

Irish Rail labels movement `Arrival` and `Departure` as actual times and describes the automatic-generation flags. Keep those flags with the times rather than treating every value as an independently observed event. [API field definitions](https://api.irishrail.ie/realtime/index.htm)

Design implications:

- Store scheduled, expected and reported actual times separately, together with the raw response.
- Derive delays and station dwell times only when usable values exist. Missing values must remain missing. Account for midnight and Europe/Dublin daylight-saving changes.
- A speed calculated from changes in coordinates would be an estimate over the sampling interval. Signalling-derived coordinates can jump or remain fixed; such a calculation must not be presented as measured instantaneous speed. A station-to-station average based on actual times and independently sourced track distance may be more useful.
- The train code plus service date identifies a service, not necessarily a physical trainset. Do not claim to track rolling stock identity.

## NTA GTFS and realtime alternative

NTA publishes timetable GTFS and realtime feeds. Realtime access requires a developer account and key. The production portal advertises version 2. [NTA portal](https://developer.nationaltransport.ie/) The official catalog lists combined, `TripUpdates` and `Vehicles` JSON endpoints under `https://api.nationaltransport.ie/gtfsr/v2/`. It also lists `GTFS_All.zip`, `GTFS_Realtime.zip`, and an operator reference CSV. [Official endpoint catalog](https://data.gov.ie/en_GB/broken_links?package=nta-gtfs)

The current production policy limits each token to one realtime request per 60 seconds. It permits research, requires attribution for public outputs and prohibits excessive downloading that degrades the service. Use the production policy rather than older 5,000-request/day descriptions. [NTA production usage policy](https://developer.nationaltransport.ie/usagepolicy)

Static GTFS is useful for planned journeys and identifying services that never enter a running feed. Archive timetable versions because the current download changes with current schedules. [NTA GTFS publication](https://developer.nationaltransport.ie/GTFS)

GTFS realtime defines a speed field, but a schema's capability does not prove an operator populates it. [Official protocol definition](https://github.com/google/transit/blob/master/gtfs-realtime/proto/gtfs-realtime.proto) Current DART presence, position quality, speed population and identifier joins in the NTA realtime feed remain unverified here. Do not choose it as the primary DART source until authenticated samples establish those facts.

## Suggested collection trial

These are proposed implementation choices, not provider guarantees:

1. Fetch the DART station list and validate both branches and the southern terminus before collection starts. The live station list includes Kilcoole beyond the requested Greystones boundary; explicitly exclude it. Keep short workings within the requested corridor.
2. Start with one DART position request per minute. Fetch movement records for active DART services on a bounded schedule and again shortly after they finish. Measure request volume before committing to continuous operation; Irish Rail publishes no numeric limit on the reviewed documentation.
3. Sample station boards if needed for prediction histories or upcoming-service discovery. Avoid polling every station redundantly when movement records already provide the required predictions.
4. Save raw responses in compressed S3 objects with UTC request and response times, endpoint, parameters, HTTP status and content hash. Keep a manifest of discovered service dates/codes so completed journeys can be revisited.
5. Compare a full day's output against planned DART trips. Check missing journeys, non-DART contamination, stale positions, empty actual times and both directions on each branch.
6. Only after this trial choose AWS scheduling and storage details. A minute-based Lambda collector is plausible, but fan-out volume, overlapping runs and final-journey retries need explicit handling.

## Existing collector audit

`collector.py` reads an environment-supplied NTA vehicle-position URL and archives every entity without filtering operator, route, train type or stops. No timetable join establishes which entities are DART. This explains how buses could enter the archive, but the repository does not contain the previous URL or archived data, so the old feed's actual contents remain unconfirmed.

The script collects positions only, not trip updates or station movement records. Its default 20-second polling interval and comment about 5,000 requests/day must be revisited against the current NTA policy. It does not retain raw responses. S3 filenames use feed timestamps, so repeated timestamps can overwrite earlier polls. These are audit findings; the old script has not been changed or deployed.

## Live verification

Ran `python3 scripts/probe_dart_api.py > docs/dart-api-sample.json` on 17 November 2026. The probe uses Python's standard library, makes public read-only requests, requires no API key, and exits with an error if a request fails. The saved response contains:

- 33 station records, including Woodbrook and Kilcoole. Some station codes have trailing spaces.
- 23 DART services, including both running and not-yet-running services.
- 16 Malahide station-board entries with mixed train types and whitespace in train codes.
- 32 location records for a Bray-to-Howth service. The origin reports actual departure `17:55:30`; Woodbrook reports arrival `17:57:12` with no departure yet. Future stops have empty actual fields. Expected and scheduled times are both present.

The sample is a single snapshot, not evidence of full-day completeness or position accuracy. Raw values, including missing fields and whitespace, are preserved in the JSON. XML responses themselves should also be retained by the eventual production collector. No historical backfill range was established, and no AWS deployment or external account change was made.
