"""Per-service evidence projections; original raw bodies are never overwritten."""
import json

def optional_json(s3,bucket,key):
    try:return json.loads(s3.get_object(Bucket=bucket,Key=key)['Body'].read())
    except Exception as exc:
        if getattr(exc,'response',{}).get('Error',{}).get('Code') in {'NoSuchKey','404','NotFound'}:return None
        raise

def project(rows,request,previous=None):
    old=previous or {};events=dict(old.get('events',{}));patterns=list(old.get('patterns',[]))
    ps=sorted((r for r in rows if r.get('locationtype') in {'O','S','D'}),key=lambda r:int(r['locationorder']))
    origin=next((r for r in ps if r['locationtype']=='O'),None)
    if origin and any(r['locationtype']=='D' for r in ps):
        pattern={'departure':origin.get('scheduleddeparture'),'stops':[r['locationcode'].strip() for r in ps]}
        if pattern not in patterns:patterns.append(pattern)
    for r in rows:
        if r.get('locationtype')=='T':continue
        for kind in ['arrival','departure']:
            if (r.get('locationtype')=='O' and kind=='arrival') or (r.get('locationtype')=='D' and kind=='departure'):continue
            if r.get(kind) is not None and r[kind]!='':
                events[r['locationcode'].strip()+'|'+kind]={'value':r[kind],'automatic':r.get('autoarrival' if kind=='arrival' else 'autodepart'),'location_type':r.get('locationtype'),'observed_at':request['finished'],'raw_key':request['raw_key'],'midnight_review':r[kind]=='00:00:00'}
    return {'rows':rows,'request':request,'events':events,'patterns':patterns}
