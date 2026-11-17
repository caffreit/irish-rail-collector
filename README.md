# DART data collection

Target: DART services between Malahide/Howth and Bray/Greystones, including intermediate stops and short workings.

The recommended primary source is Irish Rail's realtime XML API. See [API research and collector audit](docs/dart-api-research.md) for source links, available fields, limitations and a proposed collection trial.

Run a read-only live probe with Python 3, without dependencies or an API key:

```sh
python3 scripts/probe_dart_api.py > /tmp/dart-api-sample.json
```

The [local 24-hour trial](docs/local-trial.md) started on 17 November 2026 at 18:09 Dublin time. It runs under a temporary local user service and ends the following day at the same time. Progress and raw archives are in `data/trials/20261117T180920Z/`. Keep the machine awake and online.

A [saved live sample](docs/dart-api-sample.json) from 17 November 2026 includes DART positions, a station board and a train's movement records.

`collector.py` is the original unfiltered NTA vehicle-position collector. It does not enforce this project's DART scope and should not be used as the new DART collector without changes. Nothing has been deployed to AWS as part of this investigation.
