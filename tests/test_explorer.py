import datetime as dt
import unittest
import json
import tempfile
from pathlib import Path
from dart_trial import database
from scripts.build_explorer import extract
from scripts.build_explorer import TZ,asof_delay,local_time,near_time,seconds,duration_minutes

class ExplorerTests(unittest.TestCase):
    def test_passenger_stop_and_timing_point_with_same_code_are_not_two_visits(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);db=database(directory/'trial.sqlite3')
            start=local_time(dt.date(2026,11,18),6*3600)
            db.execute("insert into run values(1,?,?, 'complete')",(start,start+86400))
            db.execute("insert into services(code,service_date,first_seen,last_seen,last_active,scope) values('E102','2026-11-18',?,?,?,'included')",(start,start,start))
            db.execute("insert into requests(id,started,finished,method) values('movement',?,?, 'getTrainMovementsXML')",(start,start+7200))
            locations=[('MHIDE','O',1,None,'06:00:00',None,'06:01:00'),('PERSE','S',2,'06:50:00','06:52:00','06:52:24','06:53:48'),('PERSE','T',3,'06:53:00','06:53:00','06:52:24','06:53:48'),('BRAY','D',4,'07:20:00',None,'07:23:00',None)]
            for loc,typ,order,sa,sd,a,d in locations:
                data={'locationcode':loc,'locationtype':typ,'locationorder':str(order),'scheduledarrival':sa,'scheduleddeparture':sd,'arrival':a,'departure':d,'trainorigin':'Malahide','traindestination':'Bray'}
                db.execute("insert into observations values('movement','movement','E102','2026-11-18',?,?,?)",(loc,start+7200,json.dumps(data)))
            db.commit();db.close();result=extract(directory)
            stops=result['trains'][0]['stops']
            self.assertEqual([s['station'] for s in stops],['MHIDE','PERSE','BRAY'])
            self.assertEqual(stops[1]['type'],'S')
            self.assertEqual(result['flags']['timing_points_shadowed_by_passenger_stop'],1)
            self.assertNotIn('negative_section_duration',result['flags'])

    def test_midnight_event_attaches_to_next_calendar_day(self):
        scheduled=local_time(dt.date(2026,11,17),23*3600+59*60)
        actual=near_time('00:04:00',scheduled)
        self.assertEqual(actual-scheduled,5*60)
        self.assertEqual(dt.datetime.fromtimestamp(actual,TZ).date(),dt.date(2026,11,18))

    def test_fetch_cutoff_and_freshness(self):
        snapshots=[(100,5),(200,90)]
        self.assertEqual(asof_delay(snapshots,[100,200],150),5)
        self.assertIsNone(asof_delay(snapshots,[100,200],99))
        self.assertIsNone(asof_delay(snapshots,[100,200],501))
        self.assertIsNone(asof_delay([(100,5),(140,None)],[100,140],150))

    def test_invalid_and_endpoint_sentinels(self):
        self.assertIsNone(seconds('00:00:00'))
        self.assertEqual(seconds('00:00:00',True),0)
        self.assertIsNone(seconds('25:00:00'))
        self.assertIsNone(duration_minutes(100,200))
        self.assertEqual(duration_minutes(260,200),1)

if __name__=='__main__':unittest.main()
