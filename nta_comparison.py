"""NTA JSON snapshots: archive everything; classify only through versioned GTFS IDs."""
import collections,datetime as dt,json
from gtfs_timetables import service_active

URL='https://api.nationaltransport.ie/gtfsr/v2/gtfsr?format=json'

def field(value,name,default=None):
    camel=name.split('_')[0]+''.join(s.title() for s in name.split('_')[1:])
    return value.get(name,value.get(camel,default))

def analyse_feed(raw,index):
    feed=json.loads(raw)
    if not isinstance(feed,dict) or not isinstance(feed.get('header'),dict) or not isinstance(feed.get('entity',[]),list):raise ValueError('Unexpected GTFS realtime JSON shape')
    counts=collections.Counter();selected=[];unmatched=[]
    for entity in feed.get('entity',[]):
        if not isinstance(entity,dict):raise ValueError('Invalid GTFS realtime entity')
        payload=field(entity,'trip_update') or entity.get('vehicle')
        if not payload:
            counts['alert' if entity.get('alert') else 'other']+=1;continue
        descriptor=payload.get('trip',{});tripid=field(descriptor,'trip_id');trip=index['trips'].get(str(tripid))
        if trip is None:
            # Could be another operator, an added journey or a changed timetable ID.
            # Never assume an unknown ID is a bus or discard it from raw storage.
            counts['unmatched_trip']+=1
            if len(unmatched)<100:unmatched.append({'entity_id':entity.get('id'),'trip_id':tripid,'route_id':field(descriptor,'route_id')})
            continue
        scope=trip['scope'];counts[scope]+=1
        if scope!='corridor':continue
        date=field(descriptor,'start_date');active=None
        if date:
            try:active=service_active(index,trip['service_id'],dt.datetime.strptime(date,'%Y%m%d').date())
            except ValueError:active=False
        selected.append({'entity':entity,'gtfs_trip_id':tripid,'train_code_candidate':trip.get('trip_short_name'),'service_date':date,'scheduled_service_active':active,'schedule_relationship':field(descriptor,'schedule_relationship'),'kind':'trip_update' if field(entity,'trip_update') else 'vehicle'})
    return {'header':feed['header'],'counts':dict(counts),'corridor_entities':selected,'unmatched_sample':unmatched,'timetable_sha256':index['sha256'],'feed_info':index['feed_info'],'note':'Train-code candidates come from GTFS trip_short_name; confirm service date and stopping pattern against Irish Rail. Unmatched entities remain in raw archive.'}
