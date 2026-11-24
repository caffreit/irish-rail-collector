# DART data collection

Target: DART services between Malahide/Howth and Bray/Greystones, including intermediate stops and short workings.

The recommended primary source is Irish Rail's realtime XML API. See [API research and collector audit](docs/dart-api-research.md) for source links, available fields, limitations and a proposed collection trial.

Run a read-only live probe with Python 3, without dependencies or an API key:

```sh
python3 scripts/probe_dart_api.py > /tmp/dart-api-sample.json
```

The [local 24-hour trial](docs/local-trial.md) started on 17 November 2026 at 18:09 Dublin time. It completed on 18 November at 18:09. Its raw archive and reports are in `data/trials/20261117T180920Z/`. A separate bounded AWS week is now running; see [daily reporting](docs/daily-reporting.md).

A [saved live sample](docs/dart-api-sample.json) from 17 November 2026 includes DART positions, a station board and a train's movement records.

`collector.py` is the original unfiltered NTA vehicle-position collector. It does not enforce this project's DART scope and should not be used as the new DART collector without changes. It remains separate from the Irish Rail collector deployed to AWS.

## Interactive movement and delay explorer

Build a self-contained HTML page from the trial archive:

```sh
python3 scripts/build_explorer.py --directory data/trials/20261117T180920Z --output data/trials/20261117T180920Z/analysis
```

Open `data/trials/20261117T180920Z/analysis/explorer.html` directly in a modern browser. No server, network requests or external chart libraries are needed. The page uses the browser's built-in gzip decompression. On narrow screens, scroll individual charts horizontally to keep station labels readable.

The explorer includes scheduled/reported movement diagrams, running-time and dwell comparisons, and a forecast-delay case study. Filter the movement and duration plots by day, branch, direction, time and service. The case study stays fixed so its interpretation does not change with those filters.

The extraction opens SQLite read-only and writes `explorer-data.json` alongside the page. The latest non-empty complete movement response defines each service itinerary. Locations retain their order within that response; orders can change between revisions, so historical per-order records are not merged into a single itinerary. Midnight clocks are placed relative to the scheduled journey; origin/departure and destination/arrival events are treated separately. The build does not rewrite the collector's scope decisions or archived observations.

When a response contains both a passenger stop and a timing point with the same code, the passenger-stop row takes precedence. Pearse has this pattern; do not count the timing point as a second passenger visit. Station-board evidence is used for a timing-point row only if that response has no passenger-stop row for the station. Source types and automatic flags remain in the JSON. Invalid negative durations are excluded and counted. Timing discrepancies remain unresolved; the plots describe feed-reported events rather than independently measured train movements. See [the quality review](docs/dart-trial-quality.md).

The case study selects the largest sufficiently covered daytime sum of positive forecast delays on 18 November. Each running train uses only its most recent movement snapshot available at that position poll, no older than five minutes. Its next-location expected arrival delay is used, falling back to its current-location expected departure delay. Missing snapshots are not counted as on time. The recovery rule is an exploratory threshold over changing observed services, not proof of network clearance or causal delay propagation. The case-study date and window are specific to this trial.

Run the collector and explorer checks with:

```sh
python3 -m unittest discover -s tests -v
```

## Versioned timetables and AWS comparison

[The AWS comparison guide](docs/aws-comparison.md) documents the versioned GTFS archives, NTA comparison, sign-in, deployment and stop conditions. Timetable snapshots are versioned locally and uploaded to AWS. The comparison completed on 22 November 2026 after 24 hours of discovery and a two-hour final-movement tail. Its 780 poll manifests contain no source request failures. See `data/aws-build/dart-comparison-20261121205725.json` for its receipt. A new Irish Rail-only week reuses that stack with a separate archive prefix.

```sh
python3 gtfs_timetables.py --source irish-rail
python3 gtfs_timetables.py --source realtime-operators
python3 aws/deploy.py
```

The last command only prepares the deployment. Actual deployment requires AWS authentication and an NTA key, then an explicit `--apply`. Defaults: Dublin AWS region, one NTA combined-feed request every two minutes for 24 hours, Irish Rail collection alongside it, and a bounded two-hour tail for final movement records. Credentials are not packaged or committed.

## Timetable coverage result

The [coverage audit](docs/timetable-coverage-audit.md) matches all 89 scheduled corridor departures in the 21–22 November AWS trial to Irish Rail records, plus ten journeys scheduled to be underway at the start. This establishes journey-level discovery against both archived timetable versions; it does not establish complete stop-event histories. E907 has missing events, and later backfill is kept separate.

Offline reports are `data/coverage-audit/coverage.html` and the separate `data/coverage-audit/nta-comparison.html`. The audit guide includes the download and rebuild commands.

## Current week and daily email

The [daily reporting guide](docs/daily-reporting.md) describes the Irish Rail-only week from 23–30 November, next-day settlement at 06:00, daily cost/data emails at 09:00, fixed stop dates, and the SNS email confirmation. The first collection was verified against real archived responses. The initial report was generated; email delivery requires subscription confirmation.

The new prediction accuracy and delay recovery report is `data/coverage-audit/prediction-recovery.html`. Rebuild it with `.venv/bin/python scripts/prediction_recovery.py`.
