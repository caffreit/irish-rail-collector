# Timetable coverage audit: 21–22 November 2026

## Conclusion

All 89 corridor DART journeys scheduled to depart during the AWS trial window were matched to Irish Rail records. The two GTFS timetable versions archived during the run agree on those journeys. Every match has the same train code, service date, exact scheduled origin departure and ordered passenger-stop pattern. Every departing journey has a reported origin departure.

The scoped claim is:

> All corridor DART journeys listed in the archived GTFS timetables with scheduled departures between 21 November 2026 at 21:27:25 and 22 November 2026 at 21:27:25 Dublin time are represented in the Irish Rail archive, with reported departure evidence.

This proves journey discovery against those published timetables. It does not prove that all planned stops were served, that actual-time fields are physically accurate, or that all events were captured. It does not establish completeness for the separate 17–18 November trial, for which these timetable snapshots are not valid.

## Denominator and matching

The audit uses operating calendars, calendar-date additions/removals and GTFS times beyond 24:00. It selects explicitly labelled DART routes and passenger stops within the requested 32-station corridor. Station mappings were reviewed against all 32 GTFS names; operational timing points do not count as additional passenger stops.

Of the 89 departures inside the window, 18 have service date 21 November and 71 have service date 22 November. Ten additional scheduled journeys overlap the window but start before it; all ten also match, reported separately rather than changing the departure denominator to 99. Five additional observed identities lie outside this planned-overlap cohort. Observed-service count alone is therefore not a coverage denominator.

The archived realtime-operator timetable ZIP hashes are:

- `9690f1d4125596dbd95b8d21e355f0470d8463f86f775f9b64854bc182d0dc1d`, feed version `9B010371-78C0-4689-803B-7E2340FA4453`.
- `8874287a8d747dcf15752b2fb817a59e0dcc438fb3256d2ed35dd2f56b6c38af`, feed version `E8F07A50-7640-43A4-91FC-24AEA1171AF3`.

Both enumerate the same 99 overlapping journeys. No conflicting timetable variant or unmatched departure was found. No service-date offset was needed for any match; every matched origin clock differs by exactly zero seconds.

A matching train code alone is insufficient. The audit requires a unique matching identity with its origin clock and ordered passenger-stop pattern. It searches coherent movement responses rather than merging unrelated location-order revisions. Raw references for every match and every event are retained in `coverage.json`.

All 7,485 archived Irish Rail response bodies were decompressed and their SHA-256 checksums verified. The audit includes every DART candidate in the raw archive, including undocumented states; it does not rely solely on the collector's final scope labels.

## Operating evidence and missing events

Eighty-eight of the 89 departing journeys appear in running-position snapshots. E137 on 22 November does not, but it has reported origin departure and destination arrival. Therefore running-position polling alone would undercount journey coverage.

All 89 have origin-departure values. Eighty-eight have non-empty destination-arrival fields in the original archive, one of which is a midnight value requiring review. Required stop-event fields contain 2,350 of 2,359 arrivals and 2,350 of 2,359 departures. Two non-empty arrival values and one departure value equal `00:00:00`; these are explicitly flagged for midnight/sentinel review rather than silently dropped or treated as unquestionably correct.

All nine missing arrival fields and nine missing departure fields belong to E907 on 22 November, Greystones to Howth. The original archive contains its Connolly arrival at 13:29:06 but no later actual arrivals along the northern route, and no Connolly or subsequent departure values. Its last archived movement fetch was at 16:03:57 that day. This is a missing-event history within a captured journey, not a wholly missed journey.

A separately archived follow-up query on 23 November at 18:13 UTC returned a Howth arrival of 14:00 with `AutoArrival=0`. That later value was not in the original trial and is not included in the coverage numerator or original event counts. Intermediate gaps remain unresolved. The follow-up is stored under `data/coverage-audit/followup/`, with its URL, fetch time and checksum. It demonstrates why a next-day settlement fetch is useful; it does not independently validate the reported physical arrival.

For continuous collection, retain the original raw histories, add a next-day fetch for known journeys, and keep settled/backfilled values distinguishable from values captured during the run. An automatically generated flag is provenance, not a quality score or exclusion rule.

## Separate NTA finding

The NTA assessment is in `nta-comparison.json` and `nta-comparison.html`, separate from the coverage report. The 715 scoped snapshots contain 9,774 repeated DART trip-update records. These are explicitly GTFS-matched DART journeys, not bus records. Every one of the 89 core scheduled journeys also appears in NTA; none fills a journey-discovery gap in the Irish Rail archive.

All 135,614 supplied per-stop delay values are whole-minute multiples. The recorded DART payloads contain trip identity, stop-time updates and update timestamps, with no vehicle-position entries. All trip relationships are `SCHEDULED`; this run provides no example of an explicit cancelled/added DART journey. These observations are not claims about every possible future API response.

A content comparison paired 13,144 future event predictions with matching static scheduled clocks, same service/date and source/fetch skew of at most 30 seconds. The median absolute difference between NTA and Irish Rail predictions is 42 seconds, with a 95th percentile of 102 seconds. This compares content, not independent prediction accuracy. Repeated observations are correlated, and 13,173 potential event pairs were excluded for differing scheduled clocks. No wholesale equivalence claim is justified.

Keep versioned timetables and Irish Rail as the primary DART sources. Retain this NTA archive for reproducibility. There is no demonstrated DART position or journey-discovery advantage here. A separate bus project can use NTA vehicle positions without mixing those records into the DART dataset.

## Reproduce

Download only the read-only evidence needed for the audit. The cache includes private source records but no API key or AWS credentials:

```sh
.venv/bin/python aws/download_coverage.py --receipt data/aws-build/dart-comparison-20261121205725.json
python3 scripts/audit_coverage.py
python3 scripts/compare_nta_dart.py
python3 scripts/render_coverage.py
python3 -m unittest discover -s tests -v
```

The latter analysis commands run offline from `data/coverage-audit/`. `compare_nta_dart.py` also refreshes the offline coverage audit as a dependency. The reports and JSON ledgers are saved in that directory. The NTA and coverage reports remain separate.

The downloader preserves immutable S3 object keys in the local cache. A rerun skips already downloaded objects; checksum verification occurs in the audit. The follow-up response is deliberately outside the original poll manifests, so it cannot inflate the original coverage result.
