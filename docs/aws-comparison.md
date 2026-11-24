# One-day AWS DART comparison

Deployment succeeded on 21 November 2026 in eu-west-1. Stack `dart-comparison-20261121205725` is scheduled to start at 21:27 Dublin time on 21 November and stop new collection at 21:27 on 22 November, with final Irish Rail movement polling ending by 23:27. The deployed NTA preflight passed and returned corridor entities. Scheduled collection has not yet been observed. The receipt is `data/aws-build/dart-comparison-20261121205725.json`. The previous local trial remains unchanged.

## What is collected

The worker archives complete NTA combined GTFS realtime JSON responses and Irish Rail XML responses. It runs every two minutes during a fixed 24-hour window, then follows already-discovered Irish Rail services for up to two more hours. The schedule has a fixed end date and the worker also checks its deadline.

NTA requests use the combined endpoint `https://api.nationaltransport.ie/gtfsr/v2/gtfsr?format=json`. Authentication defaults to `x-api-key`; the deployment must pass a real request before its schedule is enabled. The [official catalog](https://data.gov.ie/en_GB/dataset/nta-gtfs) lists this endpoint. NTA's [usage policy](https://developer.nationaltransport.ie/usagepolicy) limits each token to one request per 60 seconds. The two-minute schedule plus a conditional DynamoDB gate enforces at least 65 seconds between this collector's attempts, including failures and duplicate deliveries. Do not run other collectors with the same token during the trial.

Irish Rail running services are discovered every invocation. All 32 corridor station boards are swept approximately every 30 minutes. Movement records are fetched approximately every two minutes for active services and five minutes otherwise, while still recently seen. Provider latency and invocation duration can extend these intervals. Every response retains fetch start/end times, status, checksum and a compressed raw body where one exists.

Undocumented Irish Rail movement states remain `unresolved` instead of being discarded as non-DART. The raw movement responses retain all operational timing points and location-order duplicates for later analysis.

## Timetable versions and matching

`gtfs_timetables.py` archives the Irish Rail and realtime-operator GTFS ZIPs under SHA-256 version directories. It preserves the complete ZIP, feed metadata, retrieval/import receipt, a rail index, service calendars and exceptions, stop times, and a planned-DART list for a requested date. GTFS times beyond 24:00 are preserved. Dates outside the feed validity are rejected when building a daily plan.

The index identifies rail by agency and DART by an explicit route label. It classifies corridor service from its passenger stops, not a train-code prefix or a bounding box. GTFS stations are mapped to the nearest archived Irish Rail station within 400 metres; the mapping distance and method are saved for audit. The downloaded feed currently maps all 32 stations, with the largest offset at Connolly (325 metres). Validate mappings when feeds or stations change. Bus-replacement trips labelled with a BUS short name are kept separate.

The two downloaded feeds currently share their rail trip IDs and feed version. This is a checked property of these snapshots, not a promise about future IDs. GTFS `trip_short_name` contains train-code candidates such as E108; join to Irish Rail using service date and stop pattern as well as the code.

The Lambda refreshes both timetable sources about every six hours and uses the realtime-operator index for NTA joins. Every parsed NTA snapshot records the exact timetable SHA and check time. Unmatched IDs stay in the raw archive and are reported, never silently classified as buses. The gap between static refreshes can cause temporary unmatched IDs or stale joins; archive versions and inspect these before making completeness claims. The realtime feed does not necessarily announce its matching static version.

The downloaded feed starts on 20 November 2026. It cannot establish what was scheduled on 17–18 November. The old local trial needs a historical timetable for that comparison.

Example commands, from the project root:

```sh
python3 gtfs_timetables.py --source irish-rail
python3 gtfs_timetables.py --source realtime-operators
```

To import a previously downloaded ZIP, supply `--import-file PATH`. The manifest then explicitly says that this command imported the file rather than fetched it. No original HTTP headers are invented for an imported ZIP.

## AWS sign-in

A website console login does not authenticate local AWS tooling. Use an IAM Identity Center profile whose role permits the deployment. The agent can use the resulting temporary session without seeing the user's password.

The official AWS CLI has been extracted locally at `data/tools/aws/dist/aws`. If IAM Identity Center is already configured for the account, configure and sign in from a local terminal:

```sh
data/tools/aws/dist/aws configure sso --profile dart
data/tools/aws/dist/aws sso login --profile dart
data/tools/aws/dist/aws sts get-caller-identity --profile dart
```

The SSO wizard requires an AWS access-portal/start URL, SSO region and role selection. A normal IAM-user console login is not automatically an Identity Center login. If you use a regular console identity rather than Identity Center, the installed CLI also supports `data/tools/aws/dist/aws login --profile dart --region eu-west-1`, which opens a browser-based console-credential authorisation flow. That identity needs AWS's `SignInLocalDevelopmentAccess` policy. See [AWS console-credential login](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html). The local SDK includes the optional authentication dependency for this flow. Do not paste passwords or access keys into chat or source files.

The selected role needs CloudFormation deployment permissions and access to create/configure Lambda, S3, DynamoDB, EventBridge Scheduler, Secrets Manager, CloudWatch Logs/alarms and the scoped IAM service roles. It also needs `iam:PassRole`. This account has an initial Lambda concurrency quota of ten, so the template does not reserve concurrency. A conditional DynamoDB lease serializes workers and protects shared state; a separate gate limits NTA calls even if Scheduler delivers duplicate invocations.

The package uses AWS's `eu-west-1` Dublin region by default; this is configurable. AWS resources incur account charges. No exact estimate has been validated. The code does not create EC2 instances, a VPC or NAT gateway.

## Prepare and deploy

Local deployment dependencies are pinned in `aws/requirements.txt`. The project virtual environment already has them installed. Preparation is local and does not create AWS resources:

```sh
python3 aws/deploy.py
```

The prepared deployment is in `data/aws-build/`. `aws/template.json` is generated by `python3 aws/template_builder.py`. The Lambda ZIP includes only the collector modules and station reference, not local credentials or trial databases.

After AWS login and NTA access are available, use the existing private Secrets Manager ARN for the key:

```sh
.venv/bin/python aws/deploy.py --apply --profile dart --nta-secret-arn YOUR_SECRET_ARN
```

Alternatively supply `--nta-key-file PRIVATE_FILE_PATH`; the deployment reads it without printing the key and creates a Secrets Manager secret. Keep that file out of source control. The deployed Lambda receives only the secret ARN in its environment and reads the key using its IAM role.

Deployment refreshes both public timetable downloads before making account changes, so an older local snapshot is not presented as freshly checked. It then creates a disabled schedule first. It uploads the exact archived timetables, invokes a real NTA preflight using the shared rate-limit gate, and enables the schedule only when that preflight succeeds. A receipt in `data/aws-build/` records the stack, bucket, region and fixed times. Default discovery starts 30 minutes after deployment begins; it stops 24 hours later, with a final-movement tail ending two hours after that. If provisioning passes the chosen start or preflight fails, the schedule stays disabled. Inspect the receipt and clean up or repair the disabled trial before starting a fresh one; do not repeatedly create stacks without checking earlier receipts.

A successful deployment receipt says `scheduled`, which does not prove scheduled data collection has begun. After its start time, read the actual S3 `latest.json` and several raw responses to verify the first polls.

## Inspect and stop

The deployment receipt names the archive bucket and trial prefix. Within S3:

- `timetables/` contains content-addressed full ZIPs and rail indexes.
- `trials/TRIAL/raw/` contains complete compressed source bodies and per-request manifests.
- `trials/TRIAL/nta-scoped/` contains corridor entities linked to a timetable version, counts and unmatched-ID samples.
- `trials/TRIAL/polls/` contains invocation summaries.
- `trials/TRIAL/latest.json` contains the latest completed collection invocation.
- `trials/TRIAL/preflight.json` contains the authentication/join probe.

Lambda failures produce a CloudWatch alarm visible in the AWS console. No email/SMS notification destination is configured. There is no external watchdog for missing invocations; inspect successful response ages in S3. Function logs retain 14 days. Scheduler and Lambda automatic retries are disabled; subsequent polls retry normally.

The schedule stops automatically at its end date; check the final raw response timestamps rather than relying on the schedule alone. To stop early, disable the named schedule in EventBridge Scheduler. The deployment package does not automatically delete infrastructure afterward. Deleting the CloudFormation stack retains the archive bucket and state table to avoid losing data; the separate build bucket and NTA secret also remain. Review and remove unwanted resources explicitly after exporting the data.

## Validation

```sh
python3 -m unittest discover -s tests -v
.venv/bin/cfn-lint aws/template.json
```

These checks cover timetable scope/calendar exceptions, overnight GTFS clock strings, NTA joins and cancellation preservation, rate-limit claims, deadline configuration, and existing collector/explorer behaviour. They do not replace the deployed NTA preflight or AWS IAM validation.

Data attribution: National Transport Authority, CC BY 4.0, and Iarnród Éireann realtime API. Data is provided as is. This is an exploratory comparison, not a passenger-information service.

## Account-wide budget alerts

Created and verified on 21 November 2026: `Account-monthly-10USD` ($10 per calendar month) and `Account-daily-5USD` ($5 per UTC day). Both cover the whole account, include taxes and subscription fees, and account for credits/refunds/discounts. Actual-spend alerts trigger above 50%, 80% and 100%, emailed to the address explicitly selected by the user. No budget shutdown actions are configured. Receipt: `data/aws-budget-receipt.json`.

These targets are in USD. They are notifications, not hard caps. Billing and notifications can lag usage, and charges can exceed either target before an alert arrives. The collector's fixed end date is independent of these budgets.

`aws/configure_budgets.py --profile dart --email ADDRESS` creates or verifies these named budgets and their recipients. It refuses to overwrite conflicting existing budgets. It does not alter other budgets or the collector schedule.
