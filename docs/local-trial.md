# Local 24-hour DART trial

The trial started on 17 November 2026 at 18:09:20 UTC and ends on 18 November 2026 at 18:09:20 UTC. Dublin is on UTC for these dates. The local directory is `data/trials/20261117T180920Z/`.

## Running service

The transient systemd user service is `irish-rail-dart-trial-20261117.service`. It resumes the same SQLite state on failure, with a 30-second restart delay. The collector retains its original deadline on restart and stops automatically. A separate 25-hour service runtime limit is a fallback. A filesystem lock prevents two collectors writing to the same trial.

Keep the machine awake and online, and keep the user session available. This is a local trial, without AWS storage. The transient service does not survive a reboot. The files do survive a reboot; collection can resume within the original time window with:

```sh
python3 dart_trial.py --directory data/trials/20261117T180920Z
```

Downtime remains a gap. Resuming does not recover missed position snapshots or extend the trial to hide that gap.

Inspect the service or stop it gracefully:

```sh
systemctl --user status irish-rail-dart-trial-20261117.service
systemctl --user stop irish-rail-dart-trial-20261117.service
```

## Files and progress

- `data/current-trial.json` identifies the trial directory and service.
- `report.json` in the trial directory updates about once a minute and at clean shutdown. It contains the stop time, request failures, service counts, actual-time field counts and the largest interval between position requests.
- `collector.log` records progress and failures.
- `raw/YYYY-MM-DD/HH/*.xml.gz` preserves exact response bodies, including mixed station boards and HTTP error bodies when available.
- `trial.sqlite3` indexes request times, HTTP status, endpoint parameters, body hashes and paths. It also stores normalized observations and service identity/scope. Keep the database's `-wal` and `-shm` files with it while it is running; use SQLite backup rather than copying only the main database.

The initial detached process was migrated to the user service. A duplicate startup correctly refused the collection lock. Those messages remain in the log. Initial verification under the service found 38 included services, 32 station-board responses and 38 movement responses, with no API failures. This is startup verification, not the completed trial result.

## Collection rules

Positions are requested every 60 seconds when a cycle finishes within that interval. Movement polling is every 120 seconds for active services, every five minutes for upcoming/recently disappeared services. Each of the 32 corridor station boards is checked every 30 minutes with a 90-minute lookahead. Calls run sequentially with a short gap. Slow requests or a large cycle can extend these intervals; report the observed polling gaps rather than assuming a guaranteed cadence.

DART services are discovered using `TrainType=D` and station-board `Traintype=DART`. Boards retain other trains in the raw archive only. The stop pattern must have both an origin and a destination and keep all passenger stops within the explicit 32-station corridor. Kilcoole is excluded. Short workings are included. Train codes and location codes are trimmed for matching; raw field values remain unchanged. The identity is the train code plus service date, not a rolling stock identifier.

Use the `scoped_observations` SQLite view for corridor data. The underlying observations table retains DART candidates and timing points for audit. The view excludes unverified/out-of-scope services and non-corridor locations. Rows store a JSON `data` object with the source's field names lowercased. Time strings, missing values, expected/actual distinctions and automatic-generation flags are preserved. No speed or fabricated event timestamps are added. `observed` is the local fetch-processing time; `requests.started` and `requests.finished` bound the actual fetch.

Recently disappeared services are revisited for 30 minutes to collect terminal actuals. A service near the fixed trial cutoff may remain unfinished. A missing or empty response does not erase previously collected records.

## Review after the deadline

Once the collector stops, refresh the final report with:

```sh
python3 dart_trial.py --directory data/trials/20261117T180920Z --report
```

Review request failures and polling gaps, both directions and both northern branches, repeated positions, stop-time availability, terminal actual times, and services discovered on boards but missing from the running feed. Future stops and origin/destination sentinel times require separate handling when computing punctuality or dwell times. Use `Europe/Dublin` and the service date when interpreting time-of-day fields, including journeys over midnight.

This trial establishes observed feed coverage. It does not yet prove that every scheduled DART journey appeared: an independent, versioned timetable comparison is still required to identify journeys that never appeared in either live source. No historical backfill or GPS accuracy is assumed.

Run the collector tests with:

```sh
python3 -m unittest discover -s tests -v
```
