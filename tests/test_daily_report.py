import datetime as dt,io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from aws.evidence import project
from aws.report_core import assess,health,planned_journeys,stamp,TZ
from aws.daily_report import cost_summary
from aws.template_builder import template
import test_timetables_nta

class DailyReportTests(unittest.TestCase):
    def test_projection_keeps_original_actuals_and_ignores_timing_duplicates(self):
        req={'finished':10,'raw_key':'raw-1'}
        row={'locationtype':'S','locationorder':'2','locationcode':'PERSE','arrival':'12:05:00','autoarrival':'1'}
        original=project([row],req)
        later=project([{**row,'arrival':None},{**row,'locationtype':'T','arrival':'12:00:00'}],{'finished':20,'raw_key':'raw-2'},original)
        self.assertEqual(later['events']['PERSE|arrival']['value'],'12:05:00')
        self.assertEqual(later['events']['PERSE|arrival']['raw_key'],'raw-1')
        self.assertEqual(original['events'],later['events'])

    def test_backfill_never_becomes_live_coverage(self):
        trip={'pattern':['A','B'],'origin_clock':'12:00:00'};back={'events':{'B|arrival':{'value':'13:00:00'}}}
        c=assess({'E1|date':trip},{},{'E1|date':back})
        self.assertEqual((c['live_seen'],c['strict_live_matches'],c['settlement_only']),(0,0,1))
        self.assertEqual(c['missing_terminal'],[])
        c=assess({'E1|date':trip},{'E1|date':{'patterns':[{'stops':['A','B'],'departure':'12:01:00'}]}},{})
        self.assertEqual(c['pattern_unverified'],['E1|date'])

    def test_gtfs_after_midnight_belongs_to_previous_service_date(self):
        with tempfile.TemporaryDirectory() as temp:i=test_timetables_nta.TimetableTests().fixture(Path(temp)/'f.zip')
        start=stamp(dt.date(2026,11,22),'00:00:00');end=start+3600
        j=planned_journeys(i,start,end)
        self.assertEqual(list(j),['E100|2026-11-21'])
        self.assertEqual(planned_journeys(i,start,start+360),{}) # 00:06 departure is excluded at end

    def test_missing_data_and_cost_cannot_be_green(self):
        c=assess({}, {}, {})
        warnings=health(1000,0,2000,None,c,0,'AccessDenied',static_checked=1)
        self.assertEqual(len(warnings),2)
        self.assertEqual(health(10000,0,1000,None,c,0,static_checked=1),[])

    def test_daily_cost_cache_is_reused_with_correct_reporting_day(self):
        cached={'daily':{'2026-11-22':'0.12','2026-11-23':'0.03'},'report_day_cost':'0.03'}
        s3=Mock();s3.get_object.return_value={'Body':io.BytesIO(json.dumps(cached).encode())};ce=Mock()
        result=cost_summary(ce,s3,'b','p',dt.date(2026,11,22),dt.datetime(2026,11,23,9,tzinfo=TZ))
        self.assertEqual(result['report_day_cost'],'0.12');ce.get_cost_and_usage.assert_not_called()

    def test_all_jobs_stop_and_use_dublin_clock(self):
        t=template();r=t['Resources']
        self.assertEqual(t['Parameters']['EnableNta']['Default'],'false')
        for n in ['ReportSchedule','SettlementSchedule']:
            self.assertEqual(r[n]['Properties']['ScheduleExpressionTimezone'],'Europe/Dublin')
            self.assertEqual(r[n]['Properties']['EndDate'],{'Ref':'ReportEndIso'})
        self.assertEqual(r['ReportSchedule']['Properties']['ScheduleExpression'],'cron(0 9 * * ? *)')
        self.assertEqual(r['SettlementSchedule']['Properties']['ScheduleExpression'],'cron(0 6 * * ? *)')
        self.assertIn('Fn::If',r['WorkerRole']['Properties']['Policies'][0]['PolicyDocument']['Statement'][3])
        self.assertEqual(r['Errors']['Properties']['AlarmActions'],[{'Ref':'ReportTopic'}])

    def test_unconfirmed_email_is_not_published_or_marked_sent(self):
        from aws.daily_report import handler
        s3,ddb,sns=Mock(),Mock(),Mock();ddb.get_item.return_value={}
        sns.list_subscriptions_by_topic.return_value={'Subscriptions':[{'Protocol':'email','Endpoint':'me@example.com','SubscriptionArn':'PendingConfirmation'}]}
        fake=Mock();fake.client.side_effect=lambda name,**kwargs:{'s3':s3,'dynamodb':ddb,'sns':sns,'ce':Mock(),'cloudwatch':Mock()}[name]
        env={'BUCKET':'bucket','PREFIX':'week','TABLE':'state','REPORT_TOPIC':'topic','REPORT_EMAIL':'me@example.com'}
        with patch.dict(os.environ,env),patch.dict('sys.modules',{'boto3':fake}),patch('aws.daily_report.claim',return_value=True),patch('aws.daily_report.build',return_value={'status':'ALL OK'}),patch('aws.daily_report.text_report',return_value='Test report'):
            result=handler({'action':'initial'},None)
        self.assertEqual(result['status'],'email_confirmation_pending')
        sns.publish.assert_not_called();ddb.put_item.assert_not_called()
        self.assertEqual(s3.put_object.call_count,2)
        ddb.delete_item.assert_called_once()

    def test_irish_rail_only_preflight_never_reads_nta_secret(self):
        from aws.handler import handler
        s3,ddb,secrets=Mock(),Mock(),Mock()
        with patch.dict(os.environ,{'BUCKET':'b','INDEX_KEY':'i','PREFIX':'week','NTA_ENABLED':'false'}),patch('aws.handler.clients',return_value=(s3,ddb,secrets)),patch('aws.handler.get_json',return_value={'trips':{}}),patch('aws.handler.archive_request',return_value=({'error':None},b'<ArrayOfObjTrainPositions/>')):
            result=handler({'action':'preflight'},None)
        self.assertEqual(result['source'],'irishrail')
        secrets.get_secret_value.assert_not_called()
