"""Content-addressed GTFS archives and a DART timetable index. Standard library only."""
import argparse,csv,datetime as dt,hashlib,io,json,math,shutil,urllib.request,zipfile
from pathlib import Path
from zoneinfo import ZoneInfo
from dart_trial import STATIONS

SOURCES={
 'irish-rail':'https://www.transportforireland.ie/transitData/Data/GTFS_Irish_Rail.zip',
 'realtime-operators':'https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip',
}

def rows(archive,name):
    if name not in archive.namelist():return
    with archive.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f,encoding='utf-8-sig'))

def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def distance(a,b):
    lat,lon=a;lat2,lon2=b
    return math.hypot((lat-lat2)*111320,(lon-lon2)*111320*math.cos(math.radians(lat)))

def service_active(index,service,date):
    key=date.strftime('%Y%m%d');exceptions=index['exceptions'].get(service,{})
    if key in exceptions:return exceptions[key]=='1'
    c=index['calendar'].get(service)
    return bool(c and c['start_date']<=key<=c['end_date'] and c[date.strftime('%A').lower()]=='1')

def gtfs_seconds(value):
    h,m,s=map(int,value.split(':'))
    if h<0 or not 0<=m<60 or not 0<=s<60:raise ValueError('Invalid GTFS time')
    return h*3600+m*60+s

def index_zip(path,station_rows):
    anchors={s['StationCode'].strip():(float(s['StationLatitude']),float(s['StationLongitude'])) for s in station_rows if s['StationCode'].strip() in STATIONS}
    with zipfile.ZipFile(path) as z:
        for required in ['agency.txt','routes.txt','trips.txt','stop_times.txt','stops.txt']:
            if required not in z.namelist():raise ValueError('Missing GTFS table: '+required)
        agencies={s['agency_id']:s for s in rows(z,'agency.txt')}
        rail={k for k,s in agencies.items() if 'irish rail' in s['agency_name'].lower() or 'iarnród' in s['agency_name'].lower()}
        routes={s['route_id']:s for s in rows(z,'routes.txt') if s['agency_id'] in rail}
        if not routes:raise ValueError('No Irish Rail routes in feed')
        dart={k for k,s in routes.items() if s['route_short_name'].strip().upper()=='DART'}
        if not dart:raise ValueError('No explicitly labelled DART routes')
        trips={s['trip_id']:{**s,'stops':[]} for s in rows(z,'trips.txt') if s['route_id'] in routes}
        stopids=set()
        for s in rows(z,'stop_times.txt'):
            if s['trip_id'] in trips:
                trips[s['trip_id']]['stops'].append(s);stopids.add(s['stop_id'])
        stops={s['stop_id']:s for s in rows(z,'stops.txt') if s['stop_id'] in stopids}
        mapping={}
        for k,s in stops.items():
            candidates=sorted((distance((float(s['stop_lat']),float(s['stop_lon'])),v),code) for code,v in anchors.items())
            if candidates and candidates[0][0]<=400:
                mapping[k]={'code':candidates[0][1],'distance_m':round(candidates[0][0],1),'method':'nearest_archived_irishrail_station_within_400m'}
        for t in trips.values():
            t['stops'].sort(key=lambda r:int(r['stop_sequence']))
            passenger=[s for s in t['stops'] if s.get('pickup_type')!='1' or s.get('drop_off_type')!='1']
            t['scope']='non_dart'
            if t['route_id'] in dart:
                t['scope']='corridor' if passenger and all(s['stop_id'] in mapping for s in passenger) else 'outside_or_unmapped'
                if t.get('trip_short_name','').upper().startswith('BUS'):t['scope']='replacement_bus'
        exceptions={}
        for s in rows(z,'calendar_dates.txt'):exceptions.setdefault(s['service_id'],{})[s['date']]=s['exception_type']
        return {'sha256':sha256(path),'feed_info':list(rows(z,'feed_info.txt')),'agencies':agencies,'routes':routes,'trips':trips,'stops':stops,'station_mapping':mapping,'calendar':{s['service_id']:s for s in rows(z,'calendar.txt')},'exceptions':exceptions}

def archive_feed(source,root,station_rows,import_path=None):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    headers={};now=dt.datetime.now(dt.timezone.utc).isoformat()
    temporary=root/(source+'.download')
    if import_path:shutil.copyfile(import_path,temporary)
    else:
        req=urllib.request.Request(SOURCES[source],headers={'User-Agent':'dart-research-collector/2.0'})
        with urllib.request.urlopen(req,timeout=180) as r,open(temporary,'wb') as f:
            headers={k:r.headers.get(k) for k in ['ETag','Last-Modified','Date']};shutil.copyfileobj(r,f)
    index=index_zip(temporary,station_rows);target=root/source/index['sha256'];target.mkdir(parents=True,exist_ok=True)
    if not (target/'feed.zip').exists():temporary.replace(target/'feed.zip')
    else:temporary.unlink()
    manifest={'source':source,'url':SOURCES[source],'archived_at_utc':now,'downloaded_by_this_command':not bool(import_path),'headers':headers,'sha256':index['sha256'],'feed_info':index['feed_info'],'attribution':'National Transport Authority, CC BY 4.0; data provided as is.'}
    (target/'manifest.json').write_text(json.dumps(manifest,indent=2))
    (target/'rail-index.json').write_text(json.dumps(index,separators=(',',':')))
    receipt=root/source/(dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json');receipt.write_text(json.dumps(manifest,indent=2))
    return target,index

def plan(index,date):
    day=date.strftime('%Y%m%d')
    for feed in index.get('feed_info',[]):
        if (feed.get('feed_start_date') and day<feed['feed_start_date']) or (feed.get('feed_end_date') and day>feed['feed_end_date']):
            raise ValueError('Requested date is outside the archived feed validity; do not use current GTFS as historical truth')
    return [{'trip_id':k,'train_code':t.get('trip_short_name'),'service_date':date.isoformat(),'route_id':t['route_id'],'scope':t['scope'],'stops':t['stops']} for k,t in index['trips'].items() if t['route_id'] in {r for r,v in index['routes'].items() if v['route_short_name'].upper()=='DART'} and service_active(index,t['service_id'],date)]

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',choices=SOURCES,required=True);p.add_argument('--directory',type=Path,default=Path('data/timetables'));p.add_argument('--import-file',type=Path);p.add_argument('--date',type=dt.date.fromisoformat,default=dt.datetime.now(ZoneInfo('Europe/Dublin')).date());p.add_argument('--stations',type=Path,default=Path('docs/dart-api-sample.json'));a=p.parse_args()
    station_rows=json.loads(a.stations.read_text())['stations']['rows']
    target,index=archive_feed(a.source,a.directory,station_rows,a.import_file)
    journeys=plan(index,a.date);(target/('planned-'+a.date.isoformat()+'.json')).write_text(json.dumps(journeys,indent=2))
    print(json.dumps({'archive':str(target),'feed_info':index['feed_info'],'rail_trips':len(index['trips']),'dart_trips_on_date':len(journeys),'corridor_trips_on_date':sum(t['scope']=='corridor' for t in journeys),'scopes':{s:sum(t['scope']==s for t in journeys) for s in sorted({t['scope'] for t in journeys})}},indent=2))
if __name__=='__main__':main()
