"""Pure schedule, coverage and health rules for daily reports."""
import datetime as dt
from zoneinfo import ZoneInfo
from gtfs_timetables import plan,gtfs_seconds
TZ=ZoneInfo('Europe/Dublin')

def stamp(day,clock):
    s=gtfs_seconds(clock);date=day+dt.timedelta(days=s//86400);s%=86400
    return dt.datetime.combine(date,dt.time(s//3600,s//60%60,s%60),TZ).timestamp()

def planned_journeys(index,start,end):
    first=dt.datetime.fromtimestamp(start,TZ).date()-dt.timedelta(days=1);last=dt.datetime.fromtimestamp(end,TZ).date();out={}
    for n in range((last-first).days+1):
        day=first+dt.timedelta(days=n);date=day.strftime('%Y%m%d')
        if any((f.get('feed_start_date') and date<f['feed_start_date']) or (f.get('feed_end_date') and date>f['feed_end_date']) for f in index.get('feed_info',[])):continue
        for trip in plan(index,day):
            if trip['scope']!='corridor':continue
            stops=[s for s in trip['stops'] if s.get('pickup_type')!='1' or s.get('drop_off_type')!='1']
            if not stops:continue
            origin=stamp(day,stops[0]['departure_time'])
            if not start<=origin<end:continue
            code=(trip['train_code'] or '').strip();key=code+'|'+trip['service_date']
            pattern=[index['station_mapping'][s['stop_id']]['code'] for s in stops]
            record={'code':code,'date':trip['service_date'],'origin':origin,'finish':stamp(day,stops[-1]['arrival_time']),'origin_clock':dt.datetime.fromtimestamp(origin,TZ).strftime('%H:%M:%S'),'pattern':pattern}
            if key in out and out[key]!=record:raise ValueError('Conflicting timetable variants for '+key)
            out[key]=record
    return out

def assess(journeys,live,settled):
    result={'scheduled':len(journeys),'live_seen':0,'strict_live_matches':0,'settlement_only':0,'missing':[],'missing_terminal':[],'midnight_terminal_reviews':[],'pattern_unverified':[]}
    for key,trip in journeys.items():
        l=live.get(key);back=settled.get(key)
        if l:result['live_seen']+=1
        elif back:result['settlement_only']+=1
        else:result['missing'].append(key)
        strict=l and any(p['stops']==trip['pattern'] and p['departure']==trip['origin_clock'] for p in l.get('patterns',[]))
        if strict:result['strict_live_matches']+=1
        elif l:result['pattern_unverified'].append(key)
        # The later record is separate, and may add a final event without changing live coverage.
        events={**(l or {}).get('events',{}),**(back or {}).get('events',{})}
        terminal=events.get(trip['pattern'][-1]+'|arrival')
        if not terminal:result['missing_terminal'].append(key)
        elif terminal.get('midnight_review'):result['midnight_terminal_reviews'].append(key)
    return result

def health(now,start,end,latest,coverage,request_failures,cost_error=None,execution_errors=0,static_checked=None):
    issues=[];active=start<=now<end+7200
    if active and (not latest or now-latest['observed_at']>600):issues.append('Collector heartbeat is missing or more than 10 minutes old')
    if request_failures:issues.append(f'{request_failures} source requests failed in the reporting window')
    if execution_errors:issues.append(f'{execution_errors} collector executions failed')
    if coverage['missing']:issues.append(f"{len(coverage['missing'])} timetable journeys still have no collected or settled record")
    if coverage['settlement_only']:issues.append(f"{coverage['settlement_only']} journeys were recovered only by the next-day check")
    if coverage['pattern_unverified']:issues.append(f"{len(coverage['pattern_unverified'])} live journey identities need stopping-pattern review")
    if coverage['missing_terminal']:issues.append(f"{len(coverage['missing_terminal'])} journeys still lack a reported final arrival")
    if coverage['midnight_terminal_reviews']:issues.append(f"{len(coverage['midnight_terminal_reviews'])} final-arrival midnight values need review")
    if active and (not static_checked or now-static_checked>86400):issues.append('Timetable refresh is missing or older than 24 hours')
    if cost_error:issues.append('Cost figures unavailable: '+cost_error)
    return issues
