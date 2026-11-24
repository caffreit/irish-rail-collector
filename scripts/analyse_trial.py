"""Read-only, repeatable audit of an archived DART trial; writes separate analysis artifacts."""
import argparse, collections, datetime as dt, gzip, hashlib, html, json, math, sqlite3, statistics, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dart_trial import STATIONS, parse_rows, POSITION_METHOD

def clock(s):
    if not s or s=='00:00:00': return None
    try:
        h,m,sec=map(int,s.split(':')); return h*3600+m*60+sec
    except ValueError: return None

def delta(a,b): return ((a-b+43200)%86400-43200)/60

def audit(directory, output):
    db=sqlite3.connect(f'file:{directory / "trial.sqlite3"}?mode=ro',uri=True); db.row_factory=sqlite3.Row
    run=dict(db.execute('select * from run').fetchone())
    req=[dict(x) for x in db.execute('select * from requests order by started')]
    services=[dict(x) for x in db.execute('select * from services')]
    latest=[dict(x) for x in db.execute("select * from observations where kind='movement' and rowid in(select max(rowid) from observations where kind='movement' group by code,service_date,location)")]
    for x in latest: x['data']=json.loads(x['data'])
    included={(s['code'],s['service_date']) for s in services if s['scope']=='included'}
    rows=[x for x in latest if (x['code'],x['service_date']) in included and x['location'] in STATIONS]
    stations={}
    archive={'checked':0,'missing':0,'hash_mismatches':0,'unreadable':0}
    for r in req:
        if not r['raw_path']: continue
        path=directory/r['raw_path']
        if not path.exists(): archive['missing']+=1; continue
        try: raw=gzip.open(path,'rb').read()
        except (OSError,EOFError): archive['unreadable']+=1; continue
        archive['checked']+=1
        if hashlib.sha256(raw).hexdigest()!=r['sha256']: archive['hash_mismatches']+=1
        if r['method']=='getAllStationsXML_WithStationType':
            for s in parse_rows(raw):
                if s['stationcode'].strip() in STATIONS:
                    stations[s['stationcode'].strip()]={'name':s['stationdesc'],'lat':float(s['stationlatitude']),'lon':float(s['stationlongitude'])}
    positions=[dict(x) for x in db.execute("select * from scoped_observations where kind='position' order by observed")]
    points=collections.Counter(); invalid=0; nearest=[]; stationary=0; pairs=0; previous={}; directions=collections.Counter(); hourly=collections.Counter(); pos_services=set()
    for x in positions:
        d=json.loads(x['data']); key=(x['code'],x['service_date']); pos_services.add(key)
        directions[d.get('direction')]+=1
        try: lat,lon=float(d['trainlatitude']),float(d['trainlongitude'])
        except (ValueError,TypeError): invalid+=1;continue
        if not (52.9<=lat<=53.6 and -6.5<=lon<=-5.8): invalid+=1;continue
        points[(lat,lon)]+=1
        nearest.append(min(math.hypot((lat-s['lat'])*111320,(lon-s['lon'])*111320*math.cos(math.radians(lat))) for s in stations.values()))
        if key in previous:
            pairs+=1;stationary+=previous[key]==(lat,lon)
        previous[key]=(lat,lon)
        hourly[dt.datetime.fromtimestamp(x['observed'],dt.timezone.utc).strftime('%m-%d %H:00')]+=1
    mature=[]; all_stops=[]; delays=[]; dwell=[]; auto=collections.Counter(); station_stats={k:collections.Counter() for k in stations}; routes=collections.defaultdict(set); excluded=[]
    for x in latest:
        d=x['data'];key=(x['code'],x['service_date'])
        if key not in included:
            continue
        routes[(d.get('trainorigin'),d.get('traindestination'))].add(key)
    for s in services:
        if s['scope']!='included':
            r=[x['data'] for x in latest if (x['code'],x['service_date'])==(s['code'],s['service_date'])]
            excluded.append({'code':s['code'],'date':s['service_date'],'scope':s['scope'],'origin':r[0].get('trainorigin') if r else None,'destination':r[0].get('traindestination') if r else None,'types':dict(collections.Counter(x.get('locationtype') for x in r))})
    for x in rows:
        d=x['data'];typ=d.get('locationtype'); st=x['location']
        if typ not in {'O','S','D'}: continue
        all_stops.append(x)
        if st in station_stats: station_stats[st]['stops']+=1
        for field,flag in [('arrival','autoarrival'),('departure','autodepart')]:
            if clock(d.get(field)) is not None: auto[field+':'+str(d.get(flag))]+=1
        if typ!='S': continue
        scheduled=clock(d.get('scheduledarrival'))
        if scheduled is None: continue
        epoch=dt.datetime.fromisoformat(x['service_date']).replace(tzinfo=dt.timezone.utc).timestamp()+scheduled
        # November Dublin is UTC. Boundary buffer avoids upcoming stops and catches late final updates.
        if not (run['started']+1800<=epoch<=run['ends']-1800): continue
        mature.append(x); station_stats[st]['eligible']+=1
        a,b=clock(d.get('arrival')),clock(d.get('departure'))
        if a is not None:
            station_stats[st]['arrivals']+=1
            delays.append(delta(a,scheduled))
        if b is not None: station_stats[st]['departures']+=1
        if a is not None and b is not None: dwell.append(delta(b,a))
    ticks=[r['started'] for r in req if r['method']==POSITION_METHOD]
    success=[r['started'] for r in req if r['method']==POSITION_METHOD and r['error'] is None]
    gaps=[b-a for a,b in zip([run['started']]+success,success+[run['ends']])]
    def summary(v):
        v=sorted(v)
        return {'n':len(v),'median':statistics.median(v) if v else None,'p95':v[min(len(v)-1,math.ceil(.95*len(v))-1)] if v else None,'min':min(v) if v else None,'max':max(v) if v else None}
    counts=collections.Counter('early (<-1)' if v < -1 else 'within -1 to +5' if v<=5 else '5 to 10 late' if v<=10 else '>10 late' for v in delays)
    terminal=collections.Counter()
    missing_services=collections.Counter()
    for x in mature:
        if clock(x['data'].get('arrival')) is None: missing_services[x['code']+' / '+x['service_date']]+=1
    for x in rows:
        d=x['data'];typ=d.get('locationtype')
        if typ not in {'O','D'}: continue
        field='departure' if typ=='O' else 'arrival'
        scheduled=clock(d.get('scheduled'+field))
        if scheduled is None: continue
        epoch=dt.datetime.fromisoformat(x['service_date']).replace(tzinfo=dt.timezone.utc).timestamp()+scheduled
        if run['started']+1800<=epoch<=run['ends']-1800:
            terminal[typ+'_eligible']+=1;terminal[typ+'_available']+=clock(d.get(field)) is not None
    pearse_board_types=collections.Counter()
    timing_keys={(x['code'],x['service_date']) for x in rows if x['location']=='PERSE' and x['data'].get('locationtype')=='T'}
    for x in db.execute("select code,service_date,data from observations where kind='board' and location='PERSE'"):
        if (x['code'],x['service_date']) in timing_keys: pearse_board_types[json.loads(x['data']).get('locationtype')]+=1
    location_types={k:dict(collections.Counter(x['data'].get('locationtype') for x in rows if x['location']==k)) for k in stations}
    metrics={'terminal_time_coverage':dict(terminal),'missing_arrivals_by_service':dict(missing_services.most_common()),'pearse_board_types_for_movement_T_services':dict(pearse_board_types),'location_types_by_station':location_types,'run':run,'sqlite_integrity':db.execute('pragma integrity_check').fetchone()[0],'archive':archive,'requests':len(req),'failures':sum(r['error'] is not None for r in req),'errors':dict(collections.Counter(r['error'] for r in req if r['error'])),'services':dict(collections.Counter(s['scope'] for s in services)),'excluded':excluded,'position_observations':len(positions),'position_services':len(pos_services),'board_only_included_services':sorted([list(k) for k in included-pos_services]),'position_polls':len(ticks),'max_successful_position_gap_seconds':max(gaps),'invalid_coordinates':invalid,'unique_coordinates':len(points),'within_50m_of_station_pct':100*sum(v<=50 for v in nearest)/len(nearest),'unchanged_consecutive_position_pct':100*stationary/pairs,'direction_observations':dict(directions),'station_count':len(stations),'passenger_stop_records':len(all_stops),'buffered_intermediate_stops':len(mature),'buffered_arrivals':sum(clock(x['data'].get('arrival')) is not None for x in mature),'buffered_departures':sum(clock(x['data'].get('departure')) is not None for x in mature),'reported_arrival_delay_minutes':summary(delays),'reported_dwell_minutes':summary(dwell),'automatic_flags':dict(auto),'delay_buckets':dict(counts),'routes':[{'origin':a,'destination':b,'services':len(v)} for (a,b),v in sorted(routes.items())],'stations':[{**stations[k],'code':k,**dict(v)} for k,v in station_stats.items()],'hourly_position_observations':dict(sorted(hourly.items()))}
    output.mkdir(parents=True,exist_ok=True)
    (output/'quality.json').write_text(json.dumps(metrics,indent=2))
    plot_data={'metrics':metrics,'points':[{'lat':a,'lon':b,'n':n} for (a,b),n in points.items()]}
    (output/'plot-data.json').write_text(json.dumps(plot_data))
    print(json.dumps({k:v for k,v in metrics.items() if k not in {'stations','hourly_position_observations','location_types_by_station'}},indent=2))
    return metrics
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();audit(a.directory,a.output)
