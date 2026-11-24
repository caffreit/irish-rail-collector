import csv,datetime as dt,io,json,tempfile,unittest,zipfile
from pathlib import Path
from gtfs_timetables import index_zip,plan,service_active,gtfs_seconds
from nta_comparison import analyse_feed
from aws.handler import claim
from aws.template_builder import template
from aws.deploy import scheduler_time

class TimetableTests(unittest.TestCase):
    def fixture(self,path):
        tables={
         'agency.txt':[{'agency_id':'IR','agency_name':'Irish Rail'}],
         'routes.txt':[{'route_id':'dart','agency_id':'IR','route_short_name':'DART'},{'route_id':'commuter','agency_id':'IR','route_short_name':'Commuter'}],
         'trips.txt':[{'trip_id':'good','route_id':'dart','service_id':'daily','trip_short_name':'E100'},{'trip_id':'outside','route_id':'dart','service_id':'daily','trip_short_name':'E101'},{'trip_id':'other','route_id':'commuter','service_id':'daily','trip_short_name':'P100'}],
         'stops.txt':[{'stop_id':'m','stop_name':'Malahide','stop_lat':'53.4509','stop_lon':'-6.15649'},{'stop_id':'b','stop_name':'Bray','stop_lat':'53.204','stop_lon':'-6.1'},{'stop_id':'k','stop_name':'Kilcoole','stop_lat':'53.1','stop_lon':'-6.05'}],
         'stop_times.txt':[{'trip_id':t,'stop_id':s,'stop_sequence':str(i),'arrival_time':'24:05:00','departure_time':'24:06:00'} for t,stops in [('good',['m','b']),('outside',['m','k']),('other',['m','b'])] for i,s in enumerate(stops,1)],
         'calendar.txt':[{'service_id':'daily','start_date':'20260101','end_date':'20261231',**{d:'1' for d in ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']}}],
         'calendar_dates.txt':[{'service_id':'daily','date':'20261122','exception_type':'2'}],
         'feed_info.txt':[{'feed_start_date':'20261120','feed_end_date':'20261130','feed_version':'v1'}],
        }
        with zipfile.ZipFile(path,'w') as z:
            for name,data in tables.items():
                f=io.StringIO();w=csv.DictWriter(f,fieldnames=data[0]);w.writeheader();w.writerows(data);z.writestr(name,f.getvalue())
        anchors=[{'StationCode':'MHIDE','StationLatitude':'53.4509','StationLongitude':'-6.15649'},{'StationCode':'BRAY','StationLatitude':'53.204','StationLongitude':'-6.1'}]
        return index_zip(path,anchors)

    def test_corridor_scope_and_calendar_removal(self):
        with tempfile.TemporaryDirectory() as tmp:i=self.fixture(Path(tmp)/'feed.zip')
        self.assertEqual(i['trips']['good']['scope'],'corridor')
        self.assertEqual(i['trips']['outside']['scope'],'outside_or_unmapped')
        self.assertEqual(i['trips']['other']['scope'],'non_dart')
        self.assertEqual(len(plan(i,dt.date(2026,11,21))),2)
        self.assertEqual(plan(i,dt.date(2026,11,22)),[])
        with self.assertRaises(ValueError):plan(i,dt.date(2026,11,17))
        self.assertEqual(gtfs_seconds('24:05:00'),86700)

    def test_calendar_addition_without_calendar_row(self):
        self.assertTrue(service_active({'exceptions':{'special':{'20261121':'1'}},'calendar':{}},'special',dt.date(2026,11,21)))

    def test_nta_join_preserves_cancellations_and_unknowns(self):
        with tempfile.TemporaryDirectory() as tmp:i=self.fixture(Path(tmp)/'feed.zip')
        raw=json.dumps({'header':{'timestamp':'123'},'entity':[
         {'id':'one','tripUpdate':{'trip':{'tripId':'good','startDate':'20261121','scheduleRelationship':'CANCELED'}}},
         {'id':'two','vehicle':{'trip':{'trip_id':'outside'}}},
         {'id':'bus','vehicle':{'trip':{'trip_id':'unknown'}}},
        ]}).encode()
        result=analyse_feed(raw,i)
        self.assertEqual(result['counts'],{'corridor':1,'outside_or_unmapped':1,'unmatched_trip':1})
        self.assertEqual(result['corridor_entities'][0]['schedule_relationship'],'CANCELED')
        self.assertTrue(result['corridor_entities'][0]['scheduled_service_active'])
        self.assertEqual(result['timetable_sha256'],i['sha256'])
        self.assertEqual(len(result['unmatched_sample']),1)

    def test_rate_limit_claim_is_conditional_and_persisted_before_fetch(self):
        class DDB:
            def put_item(self,**kwargs):self.kwargs=kwargs
        ddb=DDB();self.assertTrue(claim(ddb,'t','nta',100,65))
        self.assertEqual(ddb.kwargs['Item']['until']['N'],'165')
        self.assertIn('ConditionExpression',ddb.kwargs)
        class ConditionalError(Exception):response={'Error':{'Code':'ConditionalCheckFailedException'}}
        class Busy:
            def put_item(self,**kwargs):raise ConditionalError()
        self.assertFalse(claim(Busy(),'t','nta',100,65))

    def test_scheduler_dates_use_required_millisecond_utc_format(self):
        t=dt.datetime(2026,11,21,20,54,33,564010,tzinfo=dt.timezone.utc)
        self.assertEqual(scheduler_time(t),'2026-11-21T20:54:33.564Z')

    def test_deployment_is_disabled_and_has_fixed_stop(self):
        t=template();r=t['Resources']
        self.assertEqual(t['Parameters']['ScheduleState']['Default'],'DISABLED')
        self.assertIn('EndDate',r['Schedule']['Properties'])
        self.assertEqual(r['Schedule']['Properties']['ScheduleExpression'],'rate(2 minutes)')
        self.assertEqual(r['Archive']['DeletionPolicy'],'Retain')
        self.assertEqual(r['Worker']['Properties']['Environment']['Variables']['NTA_SECRET_ARN'],{'Ref':'NtaSecretArn'})
        self.assertNotIn('SecretString',json.dumps(t))

if __name__=='__main__':unittest.main()
