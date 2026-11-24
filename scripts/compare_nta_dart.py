"""Separate NTA DART assessment. Never treats other-operator records as DART."""
import argparse,collections,json,statistics,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.audit_coverage import audit,epoch,around
from nta_comparison import field

def compare(directory):
    coverage,evidence,indexes,polls,movements=audit(directory)
    files=sorted((directory/'objects'/json.loads((directory/'inventory.json').read_text())['prefix']/'nta-scoped').glob('*.json'))
    counts=collections.Counter();relationships=collections.Counter();fields=collections.Counter();services=set();delay_values=[];diffs=[];age_differences=[];matched_polls=0;schedule_mismatches=0;event_pairs=[];index_trip_count=0
    parsed=[(int(p.stem)/1000,json.loads(p.read_text())) for p in files]
    for stamp,data in parsed:
        index=indexes[data['timetable_index_key']]
        for ent in data['corridor_entities']:
            counts[ent['kind']]+=1;entity=ent['entity'];payload=field(entity,'trip_update') or entity.get('vehicle',{});descriptor=payload.get('trip',{})
            services.add((ent['train_code_candidate'],ent.get('service_date')))
            relationships[str(field(descriptor,'schedule_relationship'))]+=1
            fields.update(payload.keys())
            for stop in field(payload,'stop_time_update',[]) or []:
                for kind in ['arrival','departure']:
                    event=stop.get(kind,{})
                    if 'delay' in event:delay_values.append(float(event['delay']))
    for pi,poll in enumerate(polls):
        req=next((r for r in poll['requests'] if r['source']=='nta' and not r.get('error')),None)
        if not req or not parsed:continue
        stamp,data=min(parsed,key=lambda pair:abs(pair[0]-req['finished']))
        if abs(stamp-req['finished'])>30:continue
        matched_polls+=1;index=indexes[data['timetable_index_key']]
        for ent in data['corridor_entities']:
            update=field(ent['entity'],'trip_update')
            if not update:continue
            date=ent.get('service_date');code=ent['train_code_candidate']
            if not date:continue
            date=date[:4]+'-'+date[4:6]+'-'+date[6:8];key=(code,date);snap=movements[pi].get(key)
            if not snap:continue
            timestamp=float(update.get('timestamp',data['header'].get('timestamp',0)))
            skew=snap['finished']-timestamp
            if not 0<=skew<=30:continue
            trip=index['trips'].get(str(ent['gtfs_trip_id']))
            if not trip:continue
            by_sequence={int(s['stop_sequence']):s for s in trip['stops']}
            source={}
            for row in snap['rows']:
                loc=row['locationcode'].strip();existing=source.get(loc)
                if existing is None or row['locationtype'] in {'O','S','D'}:source[loc]=row
            for stop in field(update,'stop_time_update',[]) or []:
                sid=field(stop,'stop_id');mapping=index['station_mapping'].get(str(sid));seq=field(stop,'stop_sequence');planned=by_sequence.get(int(seq)) if seq is not None else None
                if not mapping or not planned:continue
                ir=source.get(mapping['code'])
                if not ir:continue
                for kind in ['arrival','departure']:
                    event=stop.get(kind,{})
                    if not event or ir.get(kind):continue # Compare future predictions, not already-reported actuals.
                    schedule=planned.get(kind+'_time');irschedule=ir.get('scheduled'+kind);irexpected=ir.get('expected'+kind)
                    if not schedule or not irschedule or not irexpected:continue
                    ref=epoch(date,schedule);irref=around(irschedule,ref)
                    if irref is None or abs(irref-ref)>1:schedule_mismatches+=1;continue
                    nta=float(event['time']) if event.get('time') is not None else ref+float(event['delay']) if event.get('delay') is not None else None
                    irtime=around(irexpected,ref)
                    if nta is None or irtime is None or nta<timestamp+120:continue
                    difference=irtime-nta;diffs.append(difference);age_differences.append(skew)
                    if len(event_pairs)<12:event_pairs.append({'code':code,'date':date,'station':mapping['code'],'event':kind,'source_skew_seconds':round(skew,2),'nta_delay_seconds':nta-ref,'irishrail_delay_seconds':irtime-ref,'irishrail_minus_nta_seconds':difference})
    core=[t for t in coverage['journeys'] if t['cohort']=='departures_in_window'];nta_core=[t for t in core if (t['code'],t['service_date'].replace('-','')) in services]
    absdiff=sorted(abs(v) for v in diffs)
    out={'scope':'NTA DART only; raw bus data and any future bus collector remain separate','snapshots':len(files),'entity_kinds':dict(counts),'unique_gtfs_train_dates':len(services),'scheduled_core_journeys':len(core),'scheduled_core_seen_in_nta':len(nta_core),'scheduled_core_not_seen_in_nta':[t['code']+' / '+t['service_date'] for t in core if (t['code'],t['service_date'].replace('-','')) not in services],'schedule_relationships':dict(relationships),'update_fields':dict(fields),'delay_values':len(delay_values),'delay_values_whole_minutes':sum(v%60==0 for v in delay_values),'same_poll_snapshots':matched_polls,'prediction_pairs':len(diffs),'schedule_mismatch_pairs_excluded':schedule_mismatches,'future_prediction_comparison':{'median_absolute_difference_seconds':statistics.median(absdiff) if absdiff else None,'p95_absolute_difference_seconds':absdiff[int(.95*(len(absdiff)-1))] if absdiff else None,'within_30_seconds':sum(v<=30 for v in absdiff),'within_60_seconds':sum(v<=60 for v in absdiff),'median_source_skew_seconds':statistics.median(age_differences) if age_differences else None},'examples':event_pairs,'limitations':['Repeated predictions are correlated; this compares content, not independent accuracy.','Pairs require a matching static scheduled clock, same service/date and future stop, and source/fetch skew at most 30 seconds.','No independent position, passenger count, or verified delay cause is supplied in these DART entities.','A separate Vehicles endpoint probe found no DART positions in one live snapshot; this is not proof about all times.']}
    (directory/'nta-comparison.json').write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2));return out
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--directory',type=Path,default=Path('data/coverage-audit'));a=p.parse_args();compare(a.directory)
