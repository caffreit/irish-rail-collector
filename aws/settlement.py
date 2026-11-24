"""Next-day source fetches, kept separate from the live collector's evidence."""
import datetime as dt,json,os,time,urllib.parse
from zoneinfo import ZoneInfo
from aws.handler import claim,archive_request,get_json,put_json
from aws.evidence import optional_json,project
from aws.report_core import planned_journeys
from dart_trial import BASE,parse_rows,service_key

def list_keys(s3,bucket,prefix):
    return [o['Key'] for p in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=prefix) for o in p.get('Contents',[])]

def reference_indexes(s3,bucket,prefix,date):
    refs=[get_json(s3,bucket,k)['index_key'] for k in list_keys(s3,bucket,prefix+'/plan-references/'+date+'/')]
    return sorted(set(refs)) or [os.environ['INDEX_KEY']]

def execute(s3,ddb,date,context):
    bucket,table,prefix=os.environ['BUCKET'],os.environ['TABLE'],os.environ['PREFIX']
    day=dt.date.fromisoformat(date);tz=ZoneInfo('Europe/Dublin');start=max(float(os.environ['START']),dt.datetime.combine(day,dt.time(),tz).timestamp());end=min(float(os.environ['END']),dt.datetime.combine(day+dt.timedelta(days=1),dt.time(),tz).timestamp())
    if start>=end:return {'status':'outside_collection_window','date':date}
    if not claim(ddb,table,prefix+'|settlement|'+date,time.time(),930):return {'status':'busy','date':date}
    try:
        targets={}
        for indexkey in reference_indexes(s3,bucket,prefix,date):targets.update(planned_journeys(get_json(s3,bucket,indexkey),start,end))
        # Include services discovered with this operating date, even if not in the timetable.
        for key in list_keys(s3,bucket,prefix+'/service-evidence/'+date+'/'):
            code=key.rsplit('/',1)[-1][:-5];targets.setdefault(code+'|'+date,{'code':code,'date':date})
        failures=[];completed=0;skipped=0
        for ident,t in sorted(targets.items()):
            if context.get_remaining_time_in_millis()<30000:failures.append('Time budget exhausted before '+ident);break
            key=prefix+'/settlement-evidence/'+date+'/'+t['code']+'-'+t['date']+'.json'
            previous=optional_json(s3,bucket,key)
            if previous and previous.get('rows'):skipped+=1;continue
            meta,raw=archive_request(s3,bucket,prefix+'/settlement','irishrail',BASE+'getTrainMovementsXML?'+urllib.parse.urlencode({'TrainId':t['code'],'TrainDate':dt.date.fromisoformat(t['date']).strftime('%d %b %Y')}))
            rows=[]
            if raw is not None and not meta['error']:
                try:
                    rows=parse_rows(raw)
                    if any(service_key(r)!=(t['code'],t['date']) for r in rows):raise ValueError('Unexpected service identity')
                except Exception as exc:meta['parse_error']=type(exc).__name__;rows=[]
            live=optional_json(s3,bucket,prefix+'/service-evidence/'+t['date']+'/'+t['code']+'.json')
            put_json(s3,bucket,key,{'code':t['code'],'service_date':t['date'],'reporting_date':date,'backfill':True,**project(rows,meta,live)})
            if not rows or meta.get('error') or meta.get('parse_error'):failures.append(ident)
            else:completed+=1
            time.sleep(.25)
        result={'date':date,'checked_at':time.time(),'targets':len(targets),'completed':completed,'already_completed':skipped,'failures':failures,'phase':'settlement','note':'Later backfill; never counts as live discovery.'}
        put_json(s3,bucket,prefix+'/settlements/'+date+'.json',result)
        if failures:raise RuntimeError('Settlement incomplete; see saved manifest')
        return result
    finally:ddb.delete_item(TableName=table,Key={'pk':{'S':prefix+'|settlement|'+date}})

def handler(event,context):
    import boto3
    if event.get('action')=='preflight':
        s3,ddb=boto3.client('s3'),boto3.client('dynamodb');key=os.environ['PREFIX']+'|settlement-preflight'
        if not claim(ddb,os.environ['TABLE'],key,time.time(),60):return {'status':'busy'}
        try:
            index=get_json(s3,os.environ['BUCKET'],os.environ['INDEX_KEY'])
            keys=list_keys(s3,os.environ['BUCKET'],os.environ['PREFIX']+'/plan-references/')
            return {'status':'ready','rail_trips':len(index['trips']),'timetable_references':len(keys)}
        finally:ddb.delete_item(TableName=os.environ['TABLE'],Key={'pk':{'S':key}})
    date=event.get('date') or (dt.datetime.now(ZoneInfo('Europe/Dublin')).date()-dt.timedelta(days=1)).isoformat()
    return execute(boto3.client('s3'),boto3.client('dynamodb'),date,context)
