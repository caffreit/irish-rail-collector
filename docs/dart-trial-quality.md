# DART trial quality review

**Follow-up correction, 21 November:** the explorer found that a movement response can contain both a passenger-stop row and a timing-point row with the same station code, notably at Pearse. The original latest-row-per-location audit below could select the timing point and discard the passenger row. Its stop sample and delay summaries are provisional. The new `build_explorer.py` uses one coherent final response per service and gives passenger-stop rows precedence over additional timing points with the same code. See the interactive explorer described in the README. The earlier interpretation of Pearse as simply a disagreement between sources is incomplete.

Trial: 17 November 2026 18:09 to 18 November 2026 18:09, Dublin time (UTC).

## Assessment

The trial provides useful station-level service progress and stop-time histories across all 32 requested stations, both northern branches and both directions. It does not provide continuous GPS, measured speed, independently measured event times or proven coverage of every scheduled journey. Correct the service-state handling and resolve conflicting stop classifications before deploying continuous collection.

The archive is intact: all 16,941 stored response bodies decompress and match their recorded SHA-256 hashes. SQLite integrity checks pass. Sixteen of 16,957 requests failed, all timeouts or connection refusals. The longest gap between successful position responses was 126 seconds. The original report's 76.8-second value measured attempts, including failures.

The scoped view contains 231 service-date identities, with position records for 220. The 11 without positions were scheduled to depart after the trial cutoff. Services first observed before departure are not proof that those journeys eventually operated.

## Position quality

All 19,087 scoped position observations use exactly 32 station coordinates. All are within 50 metres of a station from the archived station list; none fall outside the corridor's broad geographic bounds. Consecutive coordinates for the same service are unchanged 72.4% of the time. Treat positions as station-level progress indicators. Do not infer instantaneous speed or draw continuous tracks between these reports.

The map uses station metadata coordinates in a local equirectangular projection. Connecting lines indicate station order, not surveyed track geometry. Circle area indicates repeated position observations, not unique trains or passenger numbers.

## Timing quality and initial analysis

The analysis takes the last stored movement record for each service date and location. It excludes timing points, origin-arrival and destination-departure sentinels. For the main comparison it selects intermediate `S` stops scheduled at least 30 minutes after trial start and 30 minutes before trial end. This buffer reduces boundary effects; it does not establish that every selected service was completed.

Of 4,614 eligible intermediate stops, 4,545 have valid arrival values (98.5%) and 4,532 have departure values (98.2%). There are 173 destination arrivals among 179 eligible endpoints (96.6%) and 177 origin departures among 186 eligible origins (95.2%). These endpoint counts use the same buffer and include short workings.

The 69 missing intermediate arrivals occur in 10 service-date identities. E950 on 17 November contributes 18, and E923 on 18 November contributes 17. These require journey-level inspection; do not assume all missing events are collection failures or all are cancellations.

For the 4,545 available intermediate arrivals, reported arrival minus scheduled arrival has a median of 3.5 minutes and a 95th percentile of 27.6 minutes. The range is -5.6 to 99 minutes. These are stop-weighted statistics, with correlated stops within journeys, rather than representative network punctuality estimates. Extreme values require validation. Reported dwell time has a median of 1.4 minutes and a maximum of 63.8 minutes, another reason to inspect individual journeys before trusting dwell outliers.

Nearly all passenger-stop arrival values have `AutoArrival=1` (5,573 of 5,582 valid values). Keep this flag with the times. Irish Rail describes its data as estimates from signalling and schedules, and documents these automatic-generation flags in its [API field definitions](https://api.irishrail.ie/realtime/index.htm).

Time subtraction wraps differences to ±12 hours. `00:00:00` is treated as unavailable in this exploratory analysis; genuine midnight events need independent disambiguation. This implementation uses UTC for the November trial. Adapt the service-date and timezone treatment before using it for other dates, especially summer time and overnight journeys.

## Issues to resolve before continuous collection

1. **An undocumented location state excludes a relevant service.** E111, Howth to Greystones on 18 November, has 35 latest locations all marked `LocationType=C`. Because the collector requires O/D passenger endpoints, it marks this service excluded. The official documentation does not define `LocationType=C`; its separate `StopType=C` means current. Cancellation was an initial hypothesis, not a verified interpretation. Retain E111 and its raw records as an unresolved service state. The original database has not been rewritten.
2. **Station boards and movements disagree at Pearse.** For included services, 102 latest Pearse movement records have `LocationType=T`, documented as a timing point. For services in this group, 284 archived Pearse station-board rows say `LocationType=S`. These are rows, not 284 distinct journeys. Preserve the disagreement and reconcile the sources instead of treating T as proof that a passenger stop was skipped. The intermediate-stop statistics above omit T records and are therefore selective.
3. **Completeness is still unknown.** There is no independent, versioned timetable denominator. Compare scheduled journeys, service dates and stopping patterns before claiming every journey was captured. Keep cancellations, unresolved states, short workings and trial-boundary journeys distinct.
4. **The trial cutoff truncates journeys.** Production collection should run continuously. Finite trials should stop discovering new services at their cutoff but allow a bounded tail for already-discovered journeys.
5. **Health checks should measure successful responses.** Retain both attempted and successful polling gaps. Alert on fresh successful data rather than requests alone.

## Reproduce

Run from the project root. The audit opens the database read-only and writes analysis files separately. It verifies every saved response, so allow time for decompression.

```sh
python3 scripts/analyse_trial.py --directory data/trials/20261117T180920Z --output data/trials/20261117T180920Z/analysis
python3 scripts/render_trial_analysis.py data/trials/20261117T180920Z/analysis
```

Outputs are `quality.json`, `plot-data.json` and a self-contained `review.html` in the analysis directory. Open the HTML directly; it requires no server or external map tiles. The renderer's explanatory text is specific to this trial. Revisit that text and the audit's timezone assumptions for a new dataset.
