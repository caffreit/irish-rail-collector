"""Resumable local DART collection trial. Python 3.12+, standard library only."""

import argparse
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import logging
import signal
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = 'https://api.irishrail.ie/realtime/realtime.asmx/'
# Explicit requested corridor, including short workings and Woodbrook, excluding Kilcoole.
STATIONS = frozenset('MHIDE PMNCK GRGRD SUTTN BYSDE HWTHJ HOWTH KBRCK RAHNY HTOWN KLSTR CTARF CNLLY TARA PERSE GCDK LDWNE SMONT SIDNY BTSTN BROCK SEAPT SHILL DLERY SCOVE GLGRY DLKEY KILNY SKILL WBROK BRAY GSTNS'.split())
POSITION_METHOD = 'getCurrentTrainsXML_WithTrainType'
BOARD_METHOD = 'getStationDataByCodeXML_WithNumMins'
MOVEMENT_METHOD = 'getTrainMovementsXML'


def utc(timestamp=None):
    return dt.datetime.fromtimestamp(time.time() if timestamp is None else timestamp, dt.timezone.utc).isoformat()


def parse_rows(raw):
    root = ET.fromstring(raw)
    if root.tag.rsplit('}', 1)[-1] not in {'ArrayOfObjStation', 'ArrayOfObjTrainPositions', 'ArrayOfObjStationData', 'ArrayOfObjTrainMovements'}:
        raise ValueError('Unexpected API XML root: ' + root.tag)
    return [{field.tag.rsplit('}', 1)[-1].lower(): field.text for field in row} for row in root]


def service_key(row):
    return (row['traincode'].strip(), dt.datetime.strptime(row['traindate'].strip(), '%d %b %Y').date().isoformat())


def in_corridor(rows):
    stops = [r for r in rows if r.get('locationtype') in {'O', 'S', 'D'}]
    return bool(stops) and any(r.get('locationtype') == 'O' for r in stops) and any(r.get('locationtype') == 'D' for r in stops) and all((r.get('locationcode') or '').strip() in STATIONS for r in stops)


def database(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS run (id INTEGER PRIMARY KEY CHECK(id=1), started REAL, ends REAL, status TEXT);
    CREATE TABLE IF NOT EXISTS requests (
      id TEXT PRIMARY KEY, started REAL, finished REAL, method TEXT, params TEXT,
      http_status INTEGER, raw_path TEXT, sha256 TEXT, error TEXT, row_count INTEGER);
    CREATE TABLE IF NOT EXISTS services (
      code TEXT, service_date TEXT, first_seen REAL, last_seen REAL, last_active REAL,
      last_movement REAL DEFAULT 0, scope TEXT DEFAULT 'pending', PRIMARY KEY(code,service_date));
    CREATE TABLE IF NOT EXISTS observations (
      request_id TEXT, kind TEXT, code TEXT, service_date TEXT, location TEXT,
      observed REAL, data TEXT);
    CREATE INDEX IF NOT EXISTS observations_service ON observations(code,service_date,kind);
    CREATE INDEX IF NOT EXISTS observations_latest ON observations(kind,code,service_date,location);
    CREATE TABLE IF NOT EXISTS jobs (name TEXT PRIMARY KEY, next_due REAL);
    ''')
    return db


class Trial:
    def __init__(self, directory, hours=24):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = database(self.directory / 'trial.sqlite3')
        now = time.time()
        self.db.execute("INSERT OR IGNORE INTO run VALUES (1,?,?, 'running')", (now, now + hours * 3600))
        self.db.commit()
        self.stopping = False

    def expired(self):
        return self.stopping or time.time() >= self.db.execute('SELECT ends FROM run').fetchone()[0]

    def request(self, method, **params):
        if self.expired():
            return None, []
        request_id = uuid.uuid4().hex
        started = time.time()
        raw = None
        status = None
        error = None
        rows = []
        relative = None
        digest = None
        url = BASE + method + '?' + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'dart-collection-trial/1.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
            error = str(exc)
        except (OSError, urllib.error.URLError) as exc:
            error = str(exc)
        if raw is not None:
            relative = Path('raw') / dt.datetime.fromtimestamp(started, dt.timezone.utc).strftime('%Y-%m-%d/%H') / (request_id + '.xml.gz')
            target = self.directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(target, 'wb') as output:
                output.write(raw)
            digest = hashlib.sha256(raw).hexdigest()
            if error is None:
                try:
                    rows = parse_rows(raw)
                except (ET.ParseError, ValueError) as exc:
                    error = str(exc)
        self.db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (request_id, started, time.time(), method, json.dumps(params), status,
                         str(relative) if relative else None, digest, error, len(rows)))
        self.db.commit()
        if error:
            logging.warning('%s %s: %s', method, params, error)
        # Small spacing limits bursts; failures retry on next scheduled poll, not in a tight loop.
        time.sleep(0.25)
        return request_id, rows

    def discover(self, row, active=False):
        code, date = service_key(row)
        now = time.time()
        self.db.execute('''INSERT INTO services(code,service_date,first_seen,last_seen,last_active)
          VALUES (?,?,?,?,?) ON CONFLICT(code,service_date) DO UPDATE SET
          last_seen=excluded.last_seen,
          last_active=CASE WHEN ? THEN excluded.last_active ELSE services.last_active END''',
                        (code, date, now, now, now if active else 0, active))
        return code, date

    def observe(self, request_id, kind, row, location=''):
        code, date = service_key(row)
        self.db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?)',
                        (request_id, kind, code, date, location.strip(), time.time(), json.dumps(row)))

    def due(self, name, interval):
        now = time.time()
        row = self.db.execute('SELECT next_due FROM jobs WHERE name=?', (name,)).fetchone()
        if row and row[0] > now:
            return False
        self.db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?)', (name, now + interval))
        self.db.commit()
        return True

    def tick(self):
        if self.expired():
            return
        if self.due('stations', 86400):
            self.request('getAllStationsXML_WithStationType', StationType='D')
        positions = []
        position_id = None
        if self.due('positions', 60):
            position_id, positions = self.request(POSITION_METHOD, TrainType='D')
            for row in positions:
                self.discover(row, active=True)
            self.db.commit()
        # Sweep all corridor boards once per 30 minutes. This also finds future DARTs
        # and provides a separate check on services absent from the running list.
        for station in sorted(STATIONS):
            if self.expired():
                break
            if self.due('board:' + station, 1800):
                request_id, rows = self.request(BOARD_METHOD, StationCode=station, NumMins=90)
                for row in rows:
                    if (row.get('traintype') or '').strip().upper() == 'DART':
                        self.discover(row)
                        self.observe(request_id, 'board', row, row['stationcode'])
                self.db.commit()
        now = time.time()
        # Continue polling recently disappeared services for 30 minutes to capture
        # terminal actual times. Future board discoveries are polled every 5 minutes.
        services = self.db.execute('SELECT * FROM services WHERE last_seen >= ? ORDER BY last_movement', (now - 1800,)).fetchall()
        for service in services:
            if self.expired():
                break
            interval = 120 if now - service['last_active'] < 180 else 300
            if now - service['last_movement'] < interval:
                continue
            code, date = service['code'], service['service_date']
            request_id, rows = self.request(MOVEMENT_METHOD, TrainId=code,
                                           TrainDate=dt.date.fromisoformat(date).strftime('%d %b %Y'))
            # Empty/failed responses never erase a previously established scope.
            if rows:
                if any(service_key(row) != (code, date) for row in rows):
                    raise ValueError('Movement response service does not match request')
                scope = 'included' if in_corridor(rows) else 'excluded'
                self.db.execute('UPDATE services SET scope=? WHERE code=? AND service_date=?', (scope, code, date))
                for row in rows:
                    self.observe(request_id, 'movement', row, row['locationcode'])
            self.db.execute('UPDATE services SET last_movement=? WHERE code=? AND service_date=?', (time.time(), code, date))
            self.db.commit()
        for row in positions:
            self.observe(position_id, 'position', row)
        self.db.commit()
        if self.due("report", 60):
            result = self.report()
            logging.info("Collected %s position polls; services %s", result["position_polls"], result["services_by_scope"])

    def report(self):
        run = dict(self.db.execute('SELECT * FROM run').fetchone())
        counts = [dict(r) for r in self.db.execute('''SELECT method, COUNT(*) requests,
            SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) failures,
            SUM(row_count) rows FROM requests GROUP BY method''')]
        scope = {r[0]: r[1] for r in self.db.execute('SELECT scope,COUNT(*) FROM services GROUP BY scope')}
        ticks = [r[0] for r in self.db.execute('SELECT started FROM requests WHERE method=? ORDER BY started', (POSITION_METHOD,))]
        now = time.time()
        gaps = [b-a for a,b in zip([run['started']] + ticks, ticks + [min(now,run['ends'])])]
        # Only expose normalized records whose service's stop pattern is in scope.
        latest = self.db.execute('''SELECT o.data FROM observations o JOIN services s
          ON o.code=s.code AND o.service_date=s.service_date
          WHERE o.kind='movement' AND s.scope='included'
          AND o.rowid IN (SELECT MAX(rowid) FROM observations WHERE kind='movement'
                         GROUP BY code,service_date,location)''').fetchall()
        movements = [json.loads(r[0]) for r in latest]
        movements = [r for r in movements if (r.get("locationcode") or "").strip() in STATIONS]
        result = {'started_at_utc': utc(run['started']), 'ends_at_utc': utc(run['ends']),
                  'updated_at_utc': utc(), 'status': run['status'], 'requests': counts,
                  'services_by_scope': scope, 'position_polls': len(ticks),
                  'largest_position_poll_gap_seconds': round(max(gaps, default=0),1),
                  'latest_in_scope_stop_records': len(movements),
                  'latest_stops_with_actual_arrival': sum(bool(r.get('arrival')) for r in movements),
                  'latest_stops_with_actual_departure': sum(bool(r.get('departure')) for r in movements),
                  'limitations': ['Observed services only; no independent timetable completeness denominator.',
                    'Missing actual times remain missing; future stops naturally have no actual times.',
                    'Raw DART feeds and station boards can include out-of-scope services. Use scoped_observations view.',
                    'Positions are signalling-derived, without measured speed or a source position timestamp.',
                    'The fixed trial cutoff can truncate journeys discovered near its end.']}
        self.db.execute('''CREATE VIEW IF NOT EXISTS scoped_observations AS
          SELECT o.* FROM observations o JOIN services s ON o.code=s.code AND o.service_date=s.service_date
          WHERE s.scope='included' AND (o.kind='position' OR o.location IN (''' + ','.join("'" + s + "'" for s in sorted(STATIONS)) + '))')
        self.db.commit()
        target = self.directory / 'report.json'
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, indent=2) + '\n')
        temporary.replace(target)
        return result

    def run(self):
        self.db.execute("UPDATE run SET status='running'")
        self.db.commit()
        while not self.expired():
            self.tick()
            time.sleep(min(5, max(0, self.db.execute('SELECT ends FROM run').fetchone()[0] - time.time())))
        status = 'paused' if self.stopping else 'complete'
        self.db.execute('UPDATE run SET status=?', (status,))
        self.db.commit()
        self.report()
        logging.info('Trial %s', status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--hours', type=float, default=24, help='Only used when creating a new trial')
    parser.add_argument('--report', action='store_true', help='Read existing trial and refresh report')
    parser.add_argument('--once', action='store_true', help='Collect one tick only; intended for smoke testing')
    args = parser.parse_args()
    if args.hours <= 0:
        parser.error('--hours must be positive')
    args.directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    with (args.directory / 'collector.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('This trial is already running; read report.json for progress')
        if args.report and not (args.directory / 'trial.sqlite3').exists():
            parser.error('No existing trial database')
        trial = Trial(args.directory, args.hours)
        def stop(signum, frame):
            trial.stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            if args.report:
                print(json.dumps(trial.report(), indent=2))
            elif args.once:
                trial.tick()
                trial.db.execute("UPDATE run SET status='paused'")
                trial.db.commit()
                trial.report()
            else:
                trial.run()
        except Exception:
            trial.db.rollback()
            trial.db.execute("UPDATE run SET status='failed'")
            trial.db.commit()
            trial.report()
            logging.exception("Trial failed; resume with the same directory after resolving the error")
            raise
        finally:
            trial.db.close()


if __name__ == '__main__':
    main()
