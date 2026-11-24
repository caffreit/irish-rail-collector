"""Profile the local historical S3 mirror and build a self-contained HTML report.

Install pyarrow in your analysis environment before running this script.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


DUBLIN = ZoneInfo("Europe/Dublin")
TRACE_DATE = "2025-10-13"
LAG_LABELS = ["Clock ahead", "0–30 s", "30–60 s", "1–2 min", "2–5 min", "5–15 min", "15+ min"]


def analyse_day(paths):
    fields = Counter()
    routes = Counter()
    lags = Counter()
    rows_by_hour = Counter()
    trace_points = defaultdict(dict)
    poll_times = []
    rows = 0

    by_hour = defaultdict(list)
    for path in paths:
        poll = int(path.stem)
        poll_times.append(poll)
        by_hour[poll // 3600].append(path)

    for hour, files in sorted(by_hour.items()):
        tables = []
        stamp = datetime.fromtimestamp(hour * 3600, timezone.utc)
        trace_window = stamp.date().isoformat() == TRACE_DATE and 7 <= stamp.hour < 11
        for path in files:
            table = pq.ParquetFile(path).read(use_threads=False)
            rows += table.num_rows
            fields.update({column: table.num_rows - table[column].null_count for column in table.column_names})
            columns = ["route_id", "recorded_at", "timestamp"]
            if trace_window:
                columns.extend(["trip_id", "vehicle_id", "start_date", "start_time", "lat", "lon"])
            tables.append(table.select(columns))
        batch = pa.concat_tables(tables, promote_options="default").combine_chunks()
        rows_by_hour[str(stamp.astimezone(DUBLIN).hour)] += batch.num_rows
        for item in batch.group_by("route_id").aggregate([("route_id", "count")]).to_pylist():
            if item["route_id"]:
                routes[item["route_id"]] += item["route_id_count"]

        age = pc.divide(pc.cast(pc.subtract(batch["recorded_at"], batch["timestamp"]), pa.int64()), 1_000_000_000.0)
        under = [pc.sum(pc.cast(pc.less(age, edge), pa.int64())).as_py() or 0
                 for edge in (0, 30, 60, 120, 300, 900)]
        edges = [0, *under, len(age) - age.null_count]
        lags.update(dict(zip(LAG_LABELS, (b - a for a, b in zip(edges, edges[1:])))))

        if trace_window:
            fresh = batch.filter(pc.and_(pc.greater_equal(age, 0), pc.less_equal(age, 120)))
            for record in fresh.to_pylist():
                lat, lon = record["lat"], record["lon"]
                if not (lat is not None and lon is not None and 53.1 < lat < 53.6 and -6.65 < lon < -5.95):
                    continue
                key = tuple(record[c] for c in ("route_id", "trip_id", "vehicle_id", "start_date", "start_time"))
                if all(key):
                    trace_points[key].setdefault(int(record["timestamp"].timestamp()), [lat, lon])

    traces = []
    for (route, trip, vehicle, service_date, start), points in trace_points.items():
        # Split when the feed stops reporting fresh positions for more than three minutes.
        segments = [[]]
        for ts, (lat, lon) in sorted(points.items()):
            if segments[-1] and ts - segments[-1][-1][0] > 180:
                segments.append([])
            segments[-1].append([ts, lat, lon])
        segment = max(segments, key=len)
        if len(segment) >= 20:
            distinct = len({(lat, lon) for _, lat, lon in segment})
            if distinct >= 15:
                traces.append({"route": route, "trip": trip, "points": segment, "distinct": distinct})
    # Separate route IDs so the examples are not all journeys from one route.
    examples = {}
    for trace in sorted(traces, key=lambda t: (-t["distinct"], t["route"], t["trip"])):
        examples.setdefault(trace["route"], trace)
    return {
        "fields": dict(fields), "routes": dict(routes), "lags": dict(lags),
        "rows_by_hour": dict(rows_by_hour), "traces": list(examples.values())[:4],
        "rows": rows, "poll_times": sorted(poll_times),
    }


def build_report(directory):
    inventory = json.loads((directory / "inventory.json").read_text())
    summaries = []
    cache = directory / "daily-profile-v1"
    cache.mkdir(exist_ok=True)
    for expected in inventory:
        date = expected["date"]
        paths = sorted((directory / "vehicle_positions" / f"ingest_date={date}").glob("hour=*/*.parquet"))
        sizes = sum(path.stat().st_size for path in paths)
        if (len(paths), sizes) != (expected["files"], expected["bytes"]):
            continue
        saved = cache / f"{date}.json"
        if saved.exists():
            day = json.loads(saved.read_text())
        else:
            day = analyse_day(paths)
            saved.write_text(json.dumps(day))
            print(f"Analysed {date}: {len(paths):,} files, {day['rows']:,} rows", flush=True)
        summaries.append((expected, day))

    if len(summaries) != len(inventory):
        missing = [item["date"] for item in inventory if item["date"] not in {row[0]["date"] for row in summaries}]
        raise SystemExit(f"Archive not yet complete. Days to verify: {', '.join(missing)}")

    totals = {name: Counter() for name in ("fields", "routes", "lags", "rows_by_hour")}
    all_polls = []
    dates = []
    traces = []
    for expected, day in summaries:
        for name, counter in totals.items():
            counter.update(day[name])
        all_polls.extend(day["poll_times"])
        dates.append({**expected, "rows": day["rows"]})
        traces.extend(day["traces"])
    gaps = [b - a for a, b in zip(all_polls, all_polls[1:])]
    max_gap_index = max(range(len(gaps)), key=gaps.__getitem__)
    result = {
        "dates": dates, "files": len(all_polls), "rows": sum(d["rows"] for d in dates),
        "bytes": sum(d["bytes"] for d in dates), "fields": totals["fields"],
        "route_count": len(totals["routes"]), "routes": totals["routes"].most_common(12),
        "lags": [[label, totals["lags"][label]] for label in LAG_LABELS],
        "hours": [totals["rows_by_hour"][str(hour)] for hour in range(24)],
        "median_gap": median(gaps), "max_gap": gaps[max_gap_index],
        "max_gap_start": datetime.fromtimestamp(all_polls[max_gap_index], DUBLIN).isoformat(),
        "first_poll": datetime.fromtimestamp(all_polls[0], DUBLIN).isoformat(),
        "last_poll": datetime.fromtimestamp(all_polls[-1], DUBLIN).isoformat(),
        "traces": traces,
    }
    if sum(result["hours"]) != result["rows"]:
        raise ValueError("Hourly observation counts do not match the archive total")
    (directory / "profile.json").write_text(json.dumps(result, indent=2))
    template = Path(__file__).with_name("historical_report.html").read_text()
    embedded = json.dumps(result, separators=(",", ":")).replace("<", "\\u003c")
    (directory / "report.html").write_text(template.replace("/* ARCHIVE_DATA */ null", embedded))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=Path("data/historical-audit"))
    build_report(parser.parse_args().directory)
