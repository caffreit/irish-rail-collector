"""Build a network-free HTML explorer from the immutable DART trial archive."""
import argparse,base64,gzip,bisect,collections,datetime as dt,itertools,json,math,sqlite3,statistics,sys
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dart_trial import STATIONS
TZ=ZoneInfo('Europe/Dublin')
TRUNK='HWTHJ KBRCK RAHNY HTOWN KLSTR CTARF CNLLY TARA PERSE GCDK LDWNE SMONT SIDNY BTSTN BROCK SEAPT SHILL DLERY SCOVE GLGRY DLKEY KILNY SKILL WBROK BRAY GSTNS'.split()
PATHS={'Malahide':'MHIDE PMNCK GRGRD'.split()+TRUNK,'Howth':'HOWTH SUTTN BYSDE'.split()+TRUNK}

def seconds(value,allow_midnight=False):
    if not value or (value=='00:00:00' and not allow_midnight): return None
    try:
        h,m,s=map(int,value.split(':'))
        return h*3600+m*60+s if 0<=h<24 and 0<=m<60 and 0<=s<60 else None
    except (ValueError,TypeError): return None

def local_time(day,s):
    return dt.datetime.combine(day,dt.time(s//3600,s//60%60,s%60),TZ).timestamp()

def near_time(value,reference):
    s=seconds(value)
    if s is None or reference is None: return None
    day=dt.datetime.fromtimestamp(reference,TZ).date()
    return min((local_time(day+dt.timedelta(days=i),s) for i in [-1,0,1]),key=lambda t:abs(t-reference))

def asof_delay(snapshots,times,t):
    i=bisect.bisect_right(times,t)-1
    if i<0:return None
    fetched,value=snapshots[i]
    return value if t-fetched<=300 else None

def duration_minutes(end,start):
    return (end-start)/60 if end is not None and start is not None and end>=start else None

def extract(directory):
    db=sqlite3.connect(f'file:{directory / "trial.sqlite3"}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    run=dict(db.execute('select * from run').fetchone())
    services={(s['code'],s['service_date']):dict(s) for s in db.execute('select * from services')}
    # Board evidence resolves a station classified T in movements without discarding the source value.
    board_stops=set(); names={}; directions={}
    for row in db.execute("select code,service_date,location,data from observations where kind='board'"):
        d=json.loads(row['data']); k=(row['code'],row['service_date'])
        if d.get('locationtype') in {'O','S','D'}: board_stops.add((*k,row['location']))
        names[row['location']]=d.get('stationfullname') or row['location'];directions[k]=d.get('direction')
    grouped=collections.defaultdict(list)
    # Use one coherent final non-empty response per service. Location order can change
    # across revisions, so combining historical per-order maxima creates duplicate stops.
    for row in db.execute("""select * from observations where kind='movement' and request_id in
      (select request_id from observations where rowid in
       (select max(rowid) from observations where kind='movement' group by code,service_date))"""):
        d=json.loads(row['data']);grouped[(row['code'],row['service_date'])].append((d,row['observed']))
    trains=[];segments=[];flags=collections.Counter();
    for key,raw in grouped.items():
        service=services[key]
        if service['scope']!='included': continue
        raw.sort(key=lambda p:int(p[0]['locationorder']))
        passenger_locations={(d.get('locationcode') or '').strip() for d,t in raw if d.get('locationtype') in {'O','S','D'}}
        origin=next((d for d,t in raw if d.get('locationtype')=='O'),None)
        if not origin: flags['missing_origin']+=1;continue
        s=seconds(origin.get('scheduleddeparture'),True)
        if s is None: flags['invalid_origin_clock']+=1;continue
        day=dt.date.fromisoformat(key[1]);anchor=min((local_time(day+dt.timedelta(days=i),s) for i in [-1,0,1]),key=lambda v:abs(v-service['first_seen']))
        current_day=dt.datetime.fromtimestamp(anchor,TZ).date(); previous=anchor; stops=[]
        for d,observed in raw:
            typ=d.get('locationtype');loc=(d.get('locationcode') or '').strip()
            # All operational records advance the scheduled clock, including non-passenger points.
            fields={}
            for field in ['arrival','departure']:
                value=None if (typ=='O' and field=='arrival') or (typ=='D' and field=='departure') else seconds(d.get('scheduled'+field))
                if value is None: fields[field]=None;continue
                candidate=local_time(current_day,value)
                if candidate<previous-43200:
                    current_day+=dt.timedelta(days=1);candidate=local_time(current_day,value)
                fields[field]=candidate;previous=candidate
            if loc not in STATIONS: continue
            if typ=='T' and loc in passenger_locations:
                flags['timing_points_shadowed_by_passenger_stop']+=1;continue
            if typ not in {'O','S','D'} and (*key,loc) not in board_stops:
                flags['unresolved_station_type']+=1;continue
            if typ=='T': flags['board_confirmed_timing_point_stop']+=1
            sa,sd=fields['arrival'],fields['departure']
            a=near_time(d.get('arrival'),sa) if typ!='O' else None
            b=near_time(d.get('departure'),sd) if typ!='D' else None
            for field,v in [('arrival',a),('departure',b)]:
                if v is not None and v>observed+120:
                    flags['event_after_fetch']+=1
                    if field=='arrival':a=None
                    else:b=None
            stops.append({'station':loc,'order':int(d['locationorder']),'type':typ,'sa':sa,'sd':sd,'a':a,'d':b,'delay':(a-sa)/60 if a is not None and sa is not None else None,'dwell':duration_minutes(b,a),'autoArrival':d.get('autoarrival'),'autoDeparture':d.get('autodepart'),'fetched':observed})
        if len(stops)<2: continue
        branch='Howth' if any(s['station']=='HOWTH' for s in stops) else 'Malahide' if any(s['station']=='MHIDE' for s in stops) else 'Trunk'
        train={'id':key[0]+' / '+key[1],'code':key[0],'date':key[1],'origin':origin.get('trainorigin'),'destination':origin.get('traindestination'),'branch':branch,'direction':directions.get(key),'stops':stops,'start':anchor}
        trains.append(train)
        for left,right in zip(stops,stops[1:]):
            actual=duration_minutes(right['a'],left['d']); scheduled=duration_minutes(right['sa'],left['sd'])
            if right['a'] is not None and left['d'] is not None and actual is None:flags['negative_section_duration']+=1
            if actual is not None and scheduled is not None:
                segments.append({'train':train['id'],'date':key[1],'branch':branch,'direction':train['direction'],'from':left['station'],'to':right['station'],'t':right['sa'],'extra':actual-scheduled,'actual':actual,'scheduled':scheduled})
    # Reconstruct an as-of forecast for each running train at each position response.
    # Do not use a movement snapshot fetched later than the position response.
    snapshots=collections.defaultdict(list)
    sql="""select o.code,o.service_date,o.request_id,o.data,r.finished from observations o
      join requests r on o.request_id=r.id where o.kind='movement' order by r.finished,o.request_id"""
    for request,group in itertools.groupby(db.execute(sql),lambda r:r['request_id']):
        rows=list(group);first=rows[0];key=(first['code'],first['service_date'])
        if services[key]['scope']!='included': continue
        data=[json.loads(r['data']) for r in rows]
        value=None
        for stoptype,field in [('N','arrival'),('C','departure')]:
            candidates=sorted((d for d in data if d.get('stoptype')==stoptype),key=lambda d:int(d['locationorder']))
            for d in candidates:
                expected,scheduled=seconds(d.get('expected'+field)),seconds(d.get('scheduled'+field))
                if expected is not None and scheduled is not None:
                    value=((expected-scheduled+43200)%86400-43200)/60;break
            if value is not None:break
        snapshots[key].append((first['finished'],value))
    snapshot_times={k:[t for t,v in v] for k,v in snapshots.items()};polls=[]
    sql="""select o.code,o.service_date,o.request_id,o.data,r.finished from observations o
      join requests r on o.request_id=r.id where o.kind='position' order by r.finished,o.request_id"""
    for request,group in itertools.groupby(db.execute(sql),lambda r:r['request_id']):
        rows=list(group);t=rows[0]['finished'];active=0;values=[]
        for row in rows:
            key=(row['code'],row['service_date'])
            if services[key]['scope']!='included' or json.loads(row['data']).get('trainstatus')!='R':continue
            active+=1
            value=asof_delay(snapshots.get(key,[]),snapshot_times.get(key,[]),t)
            if value is not None:values.append(value)
        polls.append({'t':t,'active':active,'covered':len(values),'late':sum(v>5 for v in values),'total':round(sum(max(0,v) for v in values),2)})
    # Choose the largest well-covered daytime forecast-delay peak on the main trial day.
    eligible=[p for p in polls if p['active']>=5 and p['covered']/p['active']>=.8 and dt.datetime.fromtimestamp(p['t'],TZ).date()==dt.date(2026,11,18) and 6<=dt.datetime.fromtimestamp(p['t'],TZ).hour<22]
    peak=max(eligible,key=lambda p:p['total']) if eligible else None
    case=[p for p in polls if peak and peak['t']-5400<=p['t']<=peak['t']+7200]
    result={'run':run,'paths':PATHS,'names':names,'trains':trains,'segments':segments,'polls':polls,'case':case,'peak':peak,'flags':dict(flags),'excluded':[{'code':k[0],'date':k[1],'scope':s['scope']} for k,s in services.items() if s['scope']!='included']}
    db.close();return result

def build(directory,output):
    data=extract(directory);output.mkdir(parents=True,exist_ok=True)
    (output/'explorer-data.json').write_text(json.dumps(data,separators=(',',':')))
    template=Path(__file__).with_name('explorer_template.html').read_text()
    display={k:v for k,v in data.items() if k!='polls'}
    display['trains']=[{**t,'stops':[{k:(round(v,2) if isinstance(v,float) else v) for k,v in stop.items() if k not in {'order','fetched','dwell','autoDeparture'}} for stop in t['stops']]} for t in data['trains']]
    display['segments']=[{k:(round(v,2) if isinstance(v,float) else v) for k,v in seg.items() if k in {'train','from','to','t','extra'}} for seg in data['segments']]
    display['case']=[{k:(round(v,2) if isinstance(v,float) else v) for k,v in poll.items()} for poll in data['case']]
    packed=base64.b64encode(gzip.compress(json.dumps(display,separators=(',',':')).encode(),mtime=0)).decode()
    loader="JSON.parse(await new Response(new Blob([Uint8Array.from(atob('"+packed+"'),c=>c.charCodeAt(0))]).stream().pipeThrough(new DecompressionStream('gzip'))).text())"
    page=template.replace('<script>','<script>\n(async()=>{').replace('const DATA=__DATA__;','const DATA='+loader+';').replace('</script>','})().catch(e=>{document.body.append(\"Unable to load analysis: \"+e.message);console.error(e)});</script>')
    (output/'explorer.html').write_text(page)
    print(json.dumps({'services':len(data['trains']),'sections':len(data['segments']),'polls':len(data['polls']),'flags':data['flags'],'peak':data['peak']},indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();build(a.directory,a.output)
