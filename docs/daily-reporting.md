# Weekly collection and daily reporting

The Irish Rail-only week started on **23 November 2026 at 20:48 Dublin time**. New journey discovery stops on **30 November at 20:48**. Final movement polling stops at **22:48** that evening. All times below use Europe/Dublin.

The existing CloudFormation stack `dart-comparison-20261121205725` now runs this week. The completed comparison archive remains intact. The new run uses a separate prefix, `weeks/dart-week-20261123T204622Z`, and separate DynamoDB state and leases. NTA requests are disabled and its secret is no longer permitted by the worker IAM policy.

## Scheduled checks

| Job | Time | Result |
| --- | --- | --- |
| Live collection | Every two minutes | Raw Irish Rail bodies, request metadata, service evidence and poll manifests |
| Timetable refresh | Every six hours while collecting | Content-addressed Irish Rail GTFS ZIP, index and check manifest |
| Next-day settlement | 06:00 daily | Re-query the previous day's planned and discovered services; save later records separately |
| Daily email | 09:00 daily | Previous calendar day's costs, data volume, journey coverage, collection gaps and health warnings |

The final settlement and report run on **1 December**. Their schedules end that day at 10:00. No open-ended compute was introduced. Archived objects, state, alarms and logs remain after collection stops.

The report recipient is **ivan.caffrey@gmail.com**. AWS SNS requires that address to confirm the subscription before reports and execution-error alerts can arrive. The initial real report was generated successfully and saved in S3; its first delivery attempt returned `email_confirmation_pending`. The confirmation state can change after this document was written. A publish response proves AWS accepted the message, not that it reached the inbox.

## What the report checks

The report gives compressed raw response bytes, response counts, source failures, cumulative journey identities, the largest running-list polling gap, and the latest collector heartbeat. Gaps or an active heartbeat older than ten minutes cause a warning. It checks recent collector execution errors and timetable refresh age. CloudWatch separately sends collector, settlement and reporting execution failures to the same SNS topic.

For departures in the day's collected time window, it compares archived GTFS calendars, exact origin departure clocks and ordered passenger-stop patterns. It distinguishes live captures, services recovered only by the next-day query, journeys still unseen, missing final arrivals, and suspicious midnight values. First and last days are explicitly partial. Journeys already underway when the run begins are stored but are outside this departure denominator. The report does not yet audit every intermediate arrival/departure field or establish the physical accuracy of source-generated times.

`service-evidence` preserves previously non-empty actual fields when a later response clears them. `settlement-evidence` and `settlements` are separate backfill records. Raw responses are retained independently. Backfill never increases the live-capture count. Conflicting timetable variants, missing settlement results, unavailable billing or unavailable monitoring metrics produce warnings rather than silently passing.

Costs are **account-wide USD estimates from Cost Explorer**, using UTC billing dates, with daily and month-to-date values. They are not isolated DART costs and can lag usage. One successful billing query is cached per report date, because Cost Explorer requests themselves cost money. Existing account-wide $10 monthly and $5 daily budget alerts remain in place; neither is a hard spending cap.

Each report is saved under `daily-reports/YYYY-MM-DD.json` and `.txt`; the initial partial-day check uses `initial-YYYY-MM-DD`. Daily SNS publication is recorded in DynamoDB to prevent ordinary retries from duplicating email. A crash immediately after SNS accepts a message but before that marker is stored can still cause a duplicate. Failed or unconfirmed email attempts do not get a sent marker.

## Inspect and maintain

Deployment receipt: `data/aws-build/dart-week-20261123T204622Z.json`.

```sh
.venv/bin/python aws/status.py --receipt data/aws-build/dart-week-20261123T204622Z.json --output data/aws-week-status.json
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/cfn-lint -i W1030 -- aws/template.json
```

W1030 is the optional empty NTA-secret ARN default in an inactive conditional IAM branch. No secret access is included in Irish Rail-only mode.

`aws/start_week.py` prepares a package by default. `--apply` starts a **new** seven-day run in the existing stack. Do not rerun it to repair this run. Use `aws/update_runtime.py --receipt ...` for runtime changes that retain the existing prefix and stop dates. Both require AWS authentication. Deployment receipts and data remain under ignored `data/`; no credentials are packaged.

To inspect the saved initial report locally, see `data/aws-build/initial-week-report.txt`. Once the subscription is confirmed, invoking the report Lambda with `{"action":"initial"}` retries that initial email. It does not resend after a successful publication marker exists.

## Prediction and recovery analysis

Build the separate offline analysis from the original trial archive:

```sh
.venv/bin/python scripts/prediction_recovery.py
```

The output is `data/coverage-audit/prediction-recovery.html` with a JSON evidence ledger alongside it. It scores forecasts captured before each arrival against the original final reported actual, excluding suspicious midnight values and later backfill. Five-minute forecasts have a mean absolute error of about 61 seconds on 1,035 eligible arrivals, versus 260 seconds for scheduled times on the same events. Different horizons have different eligible cohorts. This is one day of descriptive evidence, with correlated stops and automatic reported times as the reference.

The exploratory network recovery rule requires sufficient train counts, fresh forecasts, continuous observations and 30 minutes of sustained improvement. It measures conditions over changing trains rather than establishing a traffic jam's physical cause or clearance.
