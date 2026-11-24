import unittest
from scripts.audit_coverage import epoch,around,match_pattern_and_departure,passenger

class CoverageTests(unittest.TestCase):
    def rows(self):
        return [
          {'locationcode':'MHIDE','locationtype':'O','locationorder':'1','scheduleddeparture':'23:30:00'},
          {'locationcode':'PERSE','locationtype':'S','locationorder':'2'},
          {'locationcode':'PERSE','locationtype':'T','locationorder':'3'},
          {'locationcode':'GSTNS','locationtype':'D','locationorder':'4'},
        ]
    def test_code_identity_needs_exact_origin_and_stopping_pattern(self):
        trip={'start':epoch('2026-11-21','23:30:00'),'pattern':['MHIDE','PERSE','GSTNS']}
        self.assertEqual(match_pattern_and_departure(trip,self.rows()),0)
        wrong_time=self.rows();wrong_time[0]['scheduleddeparture']='23:31:00'
        self.assertIsNone(match_pattern_and_departure(trip,wrong_time))
        wrong_destination=self.rows();wrong_destination[-1]['locationcode']='BRAY'
        self.assertIsNone(match_pattern_and_departure(trip,wrong_destination))
        missing_origin=self.rows()[1:]
        self.assertIsNone(match_pattern_and_departure(trip,missing_origin))
    def test_duplicate_operational_point_is_not_an_extra_passenger_stop(self):
        self.assertEqual([r['locationcode'] for r in passenger(self.rows())],['MHIDE','PERSE','GSTNS'])
    def test_gtfs_after_midnight_uses_next_day_without_changing_service_date(self):
        self.assertEqual(epoch('2026-11-21','24:15:00'),epoch('2026-11-22','00:15:00'))
        self.assertEqual(around('00:00:00',epoch('2026-11-21','24:01:00')),epoch('2026-11-22','00:00:00'))

if __name__=='__main__':unittest.main()
