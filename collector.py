# pip install requests gtfs-realtime-bindings pandas pyarrow boto3 tenacity python-dotenv

import os, io, gzip, datetime as dt, boto3
import pandas as pd
import requests
from tenacity import retry, wait_exponential, stop_after_attempt
from dotenv import load_dotenv
from google.transit import gtfs_realtime_pb2 as gtfsrt

load_dotenv()

API_KEY = os.getenv("NTA_API_KEY")
HEADERS = {"Ocp-Apim-Subscription-Key": API_KEY}

VEHICLE_POS_URL = os.getenv("NTA_VEHICLE_POS_URL")  # from NTA portal
S3_BUCKET = os.getenv("S3_BUCKET")                   # e.g. tfi-archive-ivan
S3_PREFIX = os.getenv("S3_PREFIX", "gtfsrt")         # e.g. gtfsrt
POLL_SECS = int(os.getenv("POLL_SECS", "20"))        # 20–30s fits under 5k/day

s3 = boto3.client("s3")

def _maybe_gunzip(b: bytes) -> bytes:
    try: return gzip.decompress(b)
    except Exception: return b

@retry(wait=wait_exponential(min=1, max=60), stop=stop_after_attempt(5))
def fetch_feed(url: str) -> gtfsrt.FeedMessage:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    raw = _maybe_gunzip(r.content)
    feed = gtfsrt.FeedMessage()
    feed.ParseFromString(raw)
    return feed

def feed_ts(feed) -> pd.Timestamp:
    ts = getattr(feed.header, "timestamp", 0) or int(dt.datetime.utcnow().timestamp())
    return pd.to_datetime(ts, unit="s", utc=True)

def vehicle_positions_df(feed) -> pd.DataFrame:
    rows = []
    now = feed_ts(feed)
    for ent in feed.entity:
        if not ent.vehicle or not ent.vehicle.position: 
            continue
        v = ent.vehicle
        rows.append({
            "entity_id": ent.id,
            "recorded_at": now,
            "trip_id": v.trip.trip_id or None,
            "route_id": v.trip.route_id or None,
            "start_time": v.trip.start_time or None,
            "start_date": v.trip.start_date or None,
            "vehicle_id": v.vehicle.id or None,
            "license_plate": v.vehicle.license_plate or None,
            "lat": v.position.latitude,
            "lon": v.position.longitude,
            "bearing": v.position.bearing if v.position.HasField("bearing") else None,
            "speed_mps": v.position.speed if v.position.HasField("speed") else None,
            "current_stop_sequence": v.current_stop_sequence if v.HasField("current_stop_sequence") else None,
            "current_status": v.current_status if v.HasField("current_status") else None,
            "timestamp": pd.to_datetime(v.timestamp, unit="s", utc=True) if v.HasField("timestamp") else now,
            "congestion_level": v.congestion_level if v.HasField("congestion_level") else None,
        })
    return pd.DataFrame(rows)

def write_parquet_s3(df: pd.DataFrame, ts: pd.Timestamp):
    if df.empty: 
        return
    date = ts.strftime("%Y-%m-%d")
    hour = ts.strftime("%H")
    key = f"{S3_PREFIX}/kind=vehicle_positions/date={date}/hour={hour}/{int(ts.timestamp())}.parquet"
    buf = io.BytesIO()
    df.drop_duplicates().to_parquet(buf, index=False)
    buf.seek(0)
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.getvalue())

if __name__ == "__main__":
    import time
    while True:
        try:
            feed = fetch_feed(VEHICLE_POS_URL)
            ts = feed_ts(feed)
            df = vehicle_positions_df(feed)
            write_parquet_s3(df, ts)
            print(f"[{ts}] wrote {len(df)} rows")
        except Exception as e:
            print("poll error:", e)
        time.sleep(POLL_SECS)
