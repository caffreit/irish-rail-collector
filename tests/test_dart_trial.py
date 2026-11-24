import gzip
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.error

from dart_trial import Trial, in_corridor, parse_rows, service_key, MOVEMENT_METHOD, POSITION_METHOD

SAMPLE = json.loads((Path(__file__).parents[1] / 'docs/dart-api-sample.json').read_text())


def normalized(name):
    return [{k.lower(): v for k, v in row.items()} for row in SAMPLE[name]['rows']]


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.trial = Trial(self.temp.name)

    def tearDown(self):
        self.trial.db.close()
        self.temp.cleanup()

    def test_filters_scope_and_normalizes_identity(self):
        rows = normalized('example_movements')
        self.assertTrue(in_corridor(rows))
        rows[-1]['locationcode'] = 'KCOOL'
        self.assertFalse(in_corridor(rows))
        self.assertFalse(in_corridor([]))
        self.assertEqual(service_key(rows[0]), ('E939', '2026-11-17'))

    def test_restart_keeps_original_deadline(self):
        original = self.trial.db.execute('SELECT ends FROM run').fetchone()[0]
        other = Trial(self.temp.name, 48)
        self.assertEqual(other.db.execute('SELECT ends FROM run').fetchone()[0], original)
        other.db.close()

    def test_raw_body_and_errors_are_archived(self):
        raw = b'<ArrayOfObjTrainPositions xmlns="http://api.irishrail.ie/realtime/"></ArrayOfObjTrainPositions>'
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return raw
        with patch('urllib.request.urlopen', return_value=Response()), patch('time.sleep'):
            self.trial.request(POSITION_METHOD, TrainType='D')
            self.trial.request(POSITION_METHOD, TrainType='D')
        rows = self.trial.db.execute('SELECT * FROM requests').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]['raw_path'], rows[1]['raw_path'])
        self.assertEqual(gzip.decompress((Path(self.temp.name) / rows[0]['raw_path']).read_bytes()), raw)
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('offline')), patch('time.sleep'):
            self.trial.request(POSITION_METHOD, TrainType='D')
        self.assertIn('offline', self.trial.db.execute('SELECT error FROM requests WHERE error IS NOT NULL').fetchone()[0])

    def test_tick_scopes_data_preserves_actuals_and_ignores_buses(self):
        def request(method, **params):
            if method == POSITION_METHOD:
                return 'positions', normalized('trains')
            if method == MOVEMENT_METHOD:
                rows = normalized('example_movements')
                return 'movement', rows if params['TrainId'] == 'E939' else []
            if params.get('StationCode') == 'MHIDE':
                return 'board', normalized('malahide_board')
            return 'empty', []
        with patch.object(self.trial, 'request', side_effect=request):
            self.trial.tick()
        self.assertIsNone(self.trial.db.execute("SELECT * FROM services WHERE code='D827'").fetchone())
        rows = self.trial.db.execute("SELECT data FROM scoped_observations WHERE kind='movement'").fetchall()
        self.assertEqual(len(rows), sum(r["locationcode"].strip() in __import__("dart_trial").STATIONS for r in normalized("example_movements")))
        parsed = [json.loads(r[0]) for r in rows]
        self.assertEqual(parsed[0]['departure'], '17:55:30')
        self.assertIsNone(parsed[1]['departure'])
        self.assertEqual(self.trial.db.execute("SELECT COUNT(*) FROM scoped_observations WHERE kind='position'").fetchone()[0], 1)

    def test_final_retry_of_disappeared_service_and_no_requests_after_deadline(self):
        row = normalized('example_movements')[0]
        code, date = self.trial.discover(row, active=True)
        self.trial.db.execute('UPDATE services SET last_seen=?,last_active=?,last_movement=?', (time.time()-600,time.time()-600,time.time()-301))
        for name in ['stations','positions'] + ['board:' + s for s in __import__('dart_trial').STATIONS]:
            self.trial.due(name, 3600)
        with patch.object(self.trial, 'request', return_value=('final', normalized('example_movements'))) as request:
            self.trial.tick()
        self.assertEqual(request.call_args.kwargs['TrainId'], code)
        self.trial.db.execute('UPDATE run SET ends=?', (time.time()-1,))
        with patch.object(self.trial, 'request') as request:
            self.trial.tick()
        request.assert_not_called()

    def test_deadline_stops_run_and_writes_final_report(self):
        self.trial.db.execute("UPDATE run SET ends=?", (time.time()-1,))
        self.trial.db.commit()
        with patch.object(self.trial, "request") as request:
            self.trial.run()
        request.assert_not_called()
        report = json.loads((Path(self.temp.name) / "report.json").read_text())
        self.assertEqual(report["status"], "complete")

    def test_rejects_error_payload(self):
        with self.assertRaises(ValueError):
            parse_rows(b'<html><body>error</body></html>')


if __name__ == '__main__':
    unittest.main()
