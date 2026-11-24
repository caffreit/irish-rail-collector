import unittest
from scripts.prediction_recovery import forecast_at,recovered_after

class PredictionRecoveryTests(unittest.TestCase):
    def test_forecast_never_uses_snapshot_after_cutoff(self):
        def snap(t,pred):return {'finished':t,'raw_key':str(t),'rows':[{'locationtype':'S','locationorder':'2','locationcode':'B','scheduledarrival':'12:00:00','expectedarrival':pred,'arrival':None}]}
        import datetime as dt
        from scripts.audit_coverage import epoch
        target=epoch('2026-11-22','12:00:00')
        rows=[snap(target-330,'11:58:00'),snap(target-299,'12:00:00')]
        r=forecast_at(rows,'B',target,5,target)
        self.assertEqual(r['fetched'],target-330)
        self.assertEqual(r['error_seconds'],-120)
        self.assertIsNone(forecast_at([snap(target-481,'12:00:00')],'B',target,5,target))
        rows[0]['rows'][0]['arrival']='12:00:00'
        self.assertIsNone(forecast_at(rows,'B',target,5,target))

    def test_recovery_needs_continuity_and_sufficient_trains(self):
        good=lambda t:{'t':t,'active':10,'covered':8,'late':1}
        self.assertIsNotNone(recovered_after([good(t) for t in range(0,1801,120)],0))
        self.assertIsNone(recovered_after([good(0),good(1800)],0))
        self.assertIsNone(recovered_after([{**good(t),'active':3,'covered':3} for t in range(0,1801,120)],0))
        series=[good(t) for t in range(0,1801,120)];series[5]['covered']=7
        self.assertIsNone(recovered_after(series,0))
