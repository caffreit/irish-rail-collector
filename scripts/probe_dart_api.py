"""Read-only Irish Rail API probe. Run with Python 3; no dependencies or keys."""

import datetime as dt
import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


BASE = "https://api.irishrail.ie/realtime/realtime.asmx/"


def fetch(method, **params):
    url = BASE + method + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as response:
        root = ET.fromstring(response.read())
    return {
        "url": url,
        "fetched_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "rows": [
            {field.tag.rsplit("}", 1)[-1]: field.text for field in row}
            for row in root
        ],
    }


def main():
    stations = fetch("getAllStationsXML_WithStationType", StationType="D")
    trains = fetch("getCurrentTrainsXML_WithTrainType", TrainType="D")
    board = fetch("getStationDataByCodeXML", StationCode="MHIDE")
    result = {"stations": stations, "trains": trains, "malahide_board": board}
    running = [row for row in trains["rows"] if row["TrainStatus"] == "R"]
    if running:
        train = running[0]
        result["example_movements"] = fetch(
            "getTrainMovementsXML",
            TrainId=train["TrainCode"].strip(),
            TrainDate=train["TrainDate"],
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
