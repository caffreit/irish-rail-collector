"""Offline timetable coverage audit. All denominators come from archived GTFS versions."""
import argparse,collections,datetime as dt,gzip,hashlib,json,statistics,sys,urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dart_trial import parse_rows,service_key
from gtfs_timetables import plan,gtfs_seconds
TZ=ZoneInfo('Europe/Dublin')

def clock(value):
    if not value:return None
    return gtfs_seconds(value)

def epoch(date,value):
    # Preserve GTFS operating-day hours beyond 24:00. Construct local date/time,
    # rather than silently interpreting all time strings as UTC or same-day.
    seconds=gtfs_seconds(value);day=dt.date.fromisoformat(date)+dt.timedelta(days=seconds//86400);seconds%=86400
    return dt.datetime.combine(day,dt.time(seconds//3600,seconds//60%60,seconds%60),TZ).timestamp()

def around(value,reference):
    if clock(value) is None:return None
    day=dt.datetime.fromtimestamp(reference,TZ).date()
    return min((epoch((day+dt.timedelta(days=i)).isoformat(),value) for i in [-1,0,1]),key=lambda t:abs(t-reference))

def passenger(rows):
    # A station may have both S and T entries. Never let a T entry erase the S.
    return sorted((r for r in rows if r.get('locationtype') in {'O','S','D'}),key=lambda r:int(r['locationorder']))

def match_pattern_and_departure(trip,rows):
    ps=passenger(rows);origin=next((r for r in ps if r['locationtype']=='O'),None)
    if not origin or not origin.get('scheduleddeparture'):return None
    departure=around(origin['scheduleddeparture'],trip['start'])
    if departure is None or departure!=trip['start']:return None
    if [r['locationcode'].strip() for r in ps]!=trip['pattern']:return None
    return departure-trip['start']

def expected(index,date):
    out=[]
    for t in plan(index,date):
        if t['scope']!='corridor':continue
        stops=[s for s in t['stops'] if s.get('pickup_type')!='1' or s.get('drop_off_type')!='1']
        if not stops:continue
        codes=[index['station_mapping'][s['stop_id']]['code'] for s in stops]
        out.append({'code':t['train_code'].strip(),'service_date':date.isoformat(),'trip_id':t['trip_id'],'stops':stops,'pattern':codes,'start':epoch(date.isoformat(),stops[0]['departure_time']),'finish':epoch(date.isoformat(),stops[-1]['arrival_time'])})
    return out

def audit(directory):
    obj=directory/'objects';receipt=json.loads((directory/'receipt.json').read_text());inventory=json.loads((directory/'inventory.json').read_text());prefix=inventory['prefix']
    start=dt.datetime.fromisoformat(receipt['start']).timestamp();end=dt.datetime.fromisoformat(receipt['discovery_ends']).timestamp();tail=dt.datetime.fromisoformat(receipt['tail_ends']).timestamp()
    indexes={k:json.loads((obj/k).read_text()) for k in inventory['timetable_indexes']}
    polls=sorted((json.loads(p.read_text()) for p in (obj/prefix/'polls').glob('*.json')),key=lambda p:p['observed_at'])
    evidence={};verified=set();movement_by_poll={};nta_requests=[]
    def service(key):
        return evidence.setdefault(key,{'code':key[0],'service_date':key[1],'positions':0,'running_positions':0,'boards':0,'board_stations':set(),'movements':0,'snapshots':[],'actuals':{},'board_statuses':collections.Counter()})
    for pi,poll in enumerate(polls):
        pm={}
        for req in poll['requests']:
            if req['source']=='nta':nta_requests.append(req);continue
            if req['source']!='irishrail' or not req.get('raw_key') or req.get('error'):continue
            raw=gzip.decompress((obj/req['raw_key']).read_bytes())
            if hashlib.sha256(raw).hexdigest()!=req['sha256']:raise ValueError('Raw response hash mismatch: '+req['raw_key'])
            verified.add(req['raw_key']);rows=parse_rows(raw);method=urllib.parse.urlparse(req['url']).path.rsplit('/',1)[-1]
            for r in rows:
                if method.startswith('getStation') and (r.get('traintype') or '').strip().upper()!='DART':continue
                key=service_key(r);s=service(key)
                if method.startswith('getCurrent'):
                    s['positions']+=1;s['running_positions']+=r.get('trainstatus')=='R'
                elif method.startswith('getStation'):
                    s['boards']+=1;s['board_stations'].add(r['stationcode'].strip());s['board_statuses'][r.get('status') or '']+=1
            if method=='getTrainMovementsXML' and rows:
                key=service_key(rows[0]);s=service(key);s['movements']+=1;ps=passenger(rows)
                s['snapshots'].append({'finished':req['finished'],'raw_key':req['raw_key'],'rows':rows,'passenger':ps});pm[key]=s['snapshots'][-1]
                # Preserve non-T event rows even when the location type changes to
                # undocumented C; the timetable later establishes passenger scope.
                event_rows=[r for r in rows if r.get('locationtype')!='T']
                for r in event_rows:
                    loc=r['locationcode'].strip()
                    for f in ['arrival','departure']:
                        if (r['locationtype']=='O' and f=='arrival') or (r['locationtype']=='D' and f=='departure'):continue
                        if r.get(f) is not None and r.get(f)!='':s['actuals'][loc+'|'+f]={'value':r[f],'auto':r.get('autoarrival' if f=='arrival' else 'autodepart'),'source_location_type':r.get('locationtype'),'midnight_review':r[f]=='00:00:00','raw_key':req['raw_key'],'finished':req['finished']}
        movement_by_poll[pi]=pm
    # Build a version-union of semantic journeys. Conflicting variants sharing the
    # same code/day are kept explicit instead of counted as separate missing trains.
    scheduled={};versions=[]
    first_day=dt.datetime.fromtimestamp(start,TZ).date()-dt.timedelta(days=1);last_day=dt.datetime.fromtimestamp(end,TZ).date()
    for k,index in indexes.items():
        per=[]
        for n in range((last_day-first_day).days+1):
            date=first_day+dt.timedelta(days=n)
            if any(f.get('feed_start_date','00000000')>date.strftime('%Y%m%d') or f.get('feed_end_date','99999999')<date.strftime('%Y%m%d') for f in index['feed_info']):continue
            for t in expected(index,date):
                if t['finish']<start or t['start']>=end:continue
                fingerprint=(t['code'],t['service_date'],t['start'],tuple(t['pattern']),tuple((s['arrival_time'],s['departure_time']) for s in t['stops']))
                if fingerprint not in scheduled:scheduled[fingerprint]={**t,'versions':[]}
                scheduled[fingerprint]['versions'].append(k);per.append(t['code']+'|'+t['service_date'])
        versions.append({'index':k,'feed_info':index['feed_info'],'overlap_journeys':len(per),'keys':sorted(per)})
    variants=collections.Counter((t['code'],t['service_date']) for t in scheduled.values())
    results=[];matched_keys=set()
    for trip in sorted(scheduled.values(),key=lambda t:t['start']):
        key=(trip['code'],trip['service_date']);s=evidence.get(key);schedule_matches=[]
        # Permit an API service date offset only if its origin time and pattern
        # independently identify the same planned journey. Record that offset.
        candidates=[(k,v) for k,v in evidence.items() if k[0]==trip['code'] and abs((dt.date.fromisoformat(k[1])-dt.date.fromisoformat(trip['service_date'])).days)<=1]
        for ekey,ev in candidates:
            for snap in ev['snapshots']:
                difference=match_pattern_and_departure(trip,snap['rows'])
                if difference is not None:schedule_matches.append((ekey,ev,snap,difference))
        identities={m[0] for m in schedule_matches}
        strict=len(identities)==1
        if strict:
            matched_key=next(iter(identities));s=evidence[matched_key];matched_keys.add(matched_key)
        else:matched_key=key if s else None
        observed=s is not None
        status='strict_match' if strict else 'ambiguous' if len(identities)>1 else 'identity_only' if observed else 'not_seen'
        operating=bool(s and (s['running_positions'] or s['actuals']))
        stop_calls=[]
        for i,(code,stop) in enumerate(zip(trip['pattern'],trip['stops'])):
            arrival=None if i==0 else s['actuals'].get(code+'|arrival') if s else None
            departure=None if i==len(trip['pattern'])-1 else s['actuals'].get(code+'|departure') if s else None
            stop_calls.append({'station':code,'arrival_required':i>0,'departure_required':i<len(trip['pattern'])-1,'arrival':arrival,'departure':departure})
        results.append({**{k:v for k,v in trip.items() if k!='stops'},'cohort':'departures_in_window' if trip['start']>=start else 'underway_at_start','status':status,'version_conflict':variants[key]>1,'observed_key':matched_key,'operating_evidence':operating,'running_positions':s['running_positions'] if s else 0,'board_records':s['boards'] if s else 0,'movement_responses':s['movements'] if s else 0,'origin_departure_available':bool(stop_calls[0]['departure']),'destination_arrival_available':bool(stop_calls[-1]['arrival']),'arrivals_required':len(stop_calls)-1,'arrivals_available':sum(bool(c['arrival']) for c in stop_calls[1:]),'departures_required':len(stop_calls)-1,'departures_available':sum(bool(c['departure']) for c in stop_calls[:-1]),'match_evidence':next(({'raw_key':m[2]['raw_key'],'origin_schedule_difference_seconds':m[3]} for m in reversed(schedule_matches) if strict),None),'stop_calls':stop_calls})
    extras=[]
    for k,s in evidence.items():
        if k in matched_keys:continue
        snap=s['snapshots'][-1] if s['snapshots'] else None;ps=snap['passenger'] if snap else []
        extras.append({'code':k[0],'date':k[1],'running_positions':s['running_positions'],'movements':s['movements'],'boards':s['boards'],'pattern':[r['locationcode'].strip() for r in ps],'scheduled_origin':next((r.get('scheduleddeparture') for r in ps if r['locationtype']=='O'),None),'types':dict(collections.Counter(r.get('locationtype') for r in snap['rows'])) if snap else {}})
    summary={}
    for cohort in ['departures_in_window','underway_at_start']:
        ts=[t for t in results if t['cohort']==cohort]
        summary[cohort]={'scheduled':len(ts),'statuses':dict(collections.Counter(t['status'] for t in ts)),'operating_evidence':sum(t['operating_evidence'] for t in ts),'seen_running':sum(t['running_positions']>0 for t in ts),'origin_departures':sum(t['origin_departure_available'] for t in ts),'destination_arrivals':sum(t['destination_arrival_available'] for t in ts),'arrivals_required':sum(t['arrivals_required'] for t in ts),'arrivals_available':sum(t['arrivals_available'] for t in ts),'departures_required':sum(t['departures_required'] for t in ts),'departures_available':sum(t['departures_available'] for t in ts),'midnight_arrival_reviews':sum(bool(s.get('arrival') and s['arrival'].get('midnight_review')) for t in ts for s in t['stop_calls'][1:]),'midnight_departure_reviews':sum(bool(s.get('departure') and s['departure'].get('midnight_review')) for t in ts for s in t['stop_calls'][:-1])}
    out={'window':{'start':receipt['start'],'end':receipt['discovery_ends'],'tail_end':receipt['tail_ends']},'raw_bodies_verified':len(verified),'irishrail_service_identities':len(evidence),'timetable_versions':versions,'summary':summary,'journeys':results,'unmatched_observations':extras,'version_conflict_journeys':sum(t['version_conflict'] for t in results)}
    (directory/'coverage.json').write_text(json.dumps(out,indent=2))
    # Retain compact service evidence for investigating every unmatched identity.
    compact={k[0]+'|'+k[1]:{name:v for name,v in s.items() if name not in {'snapshots','board_stations','board_statuses'}}|{'latest_rows':s['snapshots'][-1]['rows'] if s['snapshots'] else [],'board_stations':sorted(s['board_stations']),'board_statuses':dict(s['board_statuses'])} for k,s in evidence.items()}
    (directory/'irishrail-evidence.json').write_text(json.dumps(compact,indent=2))
    print(json.dumps({'summary':summary,'verified_bodies':len(verified),'versions':[(v['feed_info'],v['overlap_journeys']) for v in versions],'version_conflicts':out['version_conflict_journeys'],'unmatched_observations':extras,'non_strict_journeys':[{k:v for k,v in t.items() if k not in {'stop_calls'}} for t in results if t['status']!='strict_match']},indent=2))
    return out,evidence,indexes,polls,movement_by_poll
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,default=Path('data/coverage-audit'));a=p.parse_args();audit(a.directory)
