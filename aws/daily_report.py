"""Daily account-cost and DART data report, with explicit degraded/unknown states."""
import datetime as dt,json,os,time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from zoneinfo import ZoneInfo
from aws.handler import claim,get_json,put_json
from aws.evidence import optional_json
from aws.settlement import list_keys,reference_indexes
from aws.report_core import planned_journeys,assess,health

TZ=ZoneInfo('Europe/Dublin')

def cost_summary(ce,s3,bucket,prefix,day,now):
    cache=prefix+'/billing/'+now.astimezone(TZ).date().isoformat()+'.json'
    cached=optional_json(s3,bucket,cache)
    if cached:
        cached['report_day_cost']=cached['daily'].get(day.isoformat())
        return cached
    today=now.astimezone(TZ).date();month=today.replace(day=1);start=min(month,day.replace(day=1))
    response=ce.get_cost_and_usage(TimePeriod={'Start':start.isoformat(),'End':(today+dt.timedelta(days=1)).isoformat()},Granularity='DAILY',Metrics=['UnblendedCost'])
    values={r['TimePeriod']['Start']:r['Total']['UnblendedCost']['Amount'] for r in response['ResultsByTime']}
    value={'checked_at':time.time(),'currency':'USD','daily':values,'report_day_cost':values.get(day.isoformat()),'month_to_date':str(sum(Decimal(v) for k,v in values.items() if k>=month.isoformat())),'estimated':any(r.get('Estimated',False) for r in response['ResultsByTime']),'scope':'Whole AWS account, not isolated collector cost; billing lags usage.'}
    put_json(s3,bucket,cache,value);return value

def build(s3,ddb,ce,cw,now,initial=False):
    bucket,prefix,table=os.environ['BUCKET'],os.environ['PREFIX'],os.environ['TABLE'];start,end=float(os.environ['START']),float(os.environ['END'])
    local=now.astimezone(TZ);day=local.date() if initial else local.date()-dt.timedelta(days=1)
    window_start=max(start,dt.datetime.combine(day,dt.time(),TZ).timestamp());window_end=min(end,now.timestamp(),dt.datetime.combine(day+dt.timedelta(days=1),dt.time(),TZ).timestamp())
    latest=optional_json(s3,bucket,prefix+'/latest.json');state_item=ddb.get_item(TableName=table,Key={'pk':{'S':prefix+'|state'}},ConsistentRead=True).get('Item');state=json.loads(state_item['data']['S']) if state_item else {}
    # Poll summaries contain counts and compressed raw-body sizes. Do not re-fetch raw feeds.
    poll_objects=[]
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=prefix+'/polls/'):
        poll_objects.extend(page.get('Contents',[]))
    summary_keys=[o['Key'] for o in poll_objects if window_start-900<=float(o['Key'].rsplit('/',1)[-1][:-5])/1000<window_end]
    with ThreadPoolExecutor(max_workers=12) as pool:polls=list(pool.map(lambda k:get_json(s3,bucket,k),summary_keys))
    requests=[r for p in polls for r in p.get('requests',[]) if window_start<=r['started']<window_end]
    request_failures=sum(bool(r.get('error') or r.get('parse_error')) for r in requests)
    bytes_saved=sum(r.get('bytes_compressed',0) for r in requests if r.get('raw_key'));responses=sum(bool(r.get('raw_key')) for r in requests)
    position_times=sorted(r['started'] for r in requests if 'getCurrentTrainsXML' in r.get('url',''))
    boundaries=[window_start,*position_times,window_end] if window_start<window_end else []
    largest_gap=max((b-a for a,b in zip(boundaries,boundaries[1:])),default=0)
    warnings=[]
    if largest_gap>600:warnings.append(f'Largest running-list collection gap was {largest_gap/60:.1f} minutes')
    indexkeys=reference_indexes(s3,bucket,prefix,day.isoformat());journeys={};coverage_available=True
    if not list_keys(s3,bucket,prefix+'/plan-references/'+day.isoformat()+'/'):warnings.append('No day-specific timetable reference was saved; deployment timetable is used provisionally')
    try:
        for indexkey in indexkeys:
            for k,v in planned_journeys(get_json(s3,bucket,indexkey),window_start,window_end).items():
                if k in journeys and journeys[k]!=v:raise ValueError('Conflicting archived timetable variants for '+k)
                journeys[k]=v
    except Exception as exc:
        coverage_available=False;warnings.append('Timetable comparison unavailable: '+str(exc))
    live={};settled={}
    dates=sorted({t['date'] for t in journeys.values()})
    for date in dates:
        for key in list_keys(s3,bucket,prefix+'/service-evidence/'+date+'/'):
            v=get_json(s3,bucket,key);live[v['code']+'|'+v['service_date']]=v
    settlement_keys=list_keys(s3,bucket,prefix+'/settlement-evidence/'+day.isoformat()+'/')
    for key in settlement_keys:
        v=get_json(s3,bucket,key)
        if v.get('rows') and not v['request'].get('error') and not v['request'].get('parse_error'):settled[v['code']+'|'+v['service_date']]=v
    coverage=assess(journeys,live,settled)
    settlement=optional_json(s3,bucket,prefix+'/settlements/'+day.isoformat()+'.json')
    if not initial and window_start<window_end and not settlement:warnings.append('Next-day settlement check has not produced a manifest')
    elif settlement and settlement.get('failures'):warnings.append('Next-day settlement has failed or unfinished requests')
    cost=None;cost_error=None
    try:cost=cost_summary(ce,s3,bucket,prefix,day,now)
    except Exception as exc:cost_error=type(exc).__name__
    if cost and cost.get('report_day_cost') is None:cost_error='No billing value returned for reporting day'
    execution_errors=0
    try:
        metrics=cw.get_metric_statistics(Namespace='AWS/Lambda',MetricName='Errors',Dimensions=[{'Name':'FunctionName','Value':os.environ['WORKER_FUNCTION']}],StartTime=dt.datetime.fromtimestamp(max(start,now.timestamp()-86400),dt.timezone.utc),EndTime=now,Period=3600,Statistics=['Sum'])
        execution_errors=int(sum(p['Sum'] for p in metrics.get('Datapoints',[])))
    except Exception as exc:warnings.append('Execution-error metric unavailable: '+type(exc).__name__)
    warnings+=health(now.timestamp(),start,end,latest,coverage,request_failures,cost_error,execution_errors,state.get('static_checked'))
    if window_start<window_end and not requests:warnings.append('No source requests are recorded for the reporting window')
    all_keys=list_keys(s3,bucket,prefix+'/service-evidence/')
    phase='not_started' if now.timestamp()<start else 'collecting' if now.timestamp()<end else 'final_movements' if now.timestamp()<end+7200 else 'finished'
    return {'created_at':now.isoformat(),'day':day.isoformat(),'initial':initial,'phase':phase,'window_start':window_start,'window_end':window_end,'partial_day':window_start>dt.datetime.combine(day,dt.time(),TZ).timestamp() or window_end<dt.datetime.combine(day+dt.timedelta(days=1),dt.time(),TZ).timestamp(),'largest_position_gap_seconds':largest_gap,'request_count':len(requests),'raw_responses':responses,'compressed_raw_bytes':bytes_saved,'request_failures':request_failures,'cumulative_service_identities':len(all_keys),'coverage':coverage,'coverage_available':coverage_available,'latest_poll_at':latest.get('observed_at') if latest else None,'cost':cost,'cost_error':cost_error,'warnings':warnings,'status':'NEEDS ATTENTION' if warnings else 'ALL OK','settlement':settlement,'expected_end':end,'tail_end':end+7200}

def text_report(r):
    def local(t):return dt.datetime.fromtimestamp(t,TZ).strftime('%d %b %H:%M')
    c=r['coverage'];cost=r['cost'];money=('$'+format(Decimal(cost['report_day_cost']),'.4f')) if cost and cost.get('report_day_cost') is not None else 'unavailable'
    lines=[f"DART daily check: {r['status']}",f"Date: {r['day']} (Dublin time; {'partial day' if r['partial_day'] else 'full day'})",f"Collector: {r['phase']}",'',f"Recorded account cost for this day: {money} USD",f"Recorded account month-to-date: ${Decimal(cost['month_to_date']):.4f} USD" if cost else 'Cost figures unavailable','Costs cover the whole AWS account, use UTC billing dates and are estimates. Billing can lag by hours.','',f"Saved: {r['raw_responses']:,} source responses, {r['compressed_raw_bytes']/1048576:.2f} MiB compressed raw data",f"Source request failures: {r['request_failures']}",f"Largest running-list collection gap: {r['largest_position_gap_seconds']/60:.1f} minutes",f"Journey identities stored in this weekly run: {r['cumulative_service_identities']}"]
    if r['coverage_available']:lines += [f"Timetable departures in reporting window: {c['scheduled']}",f"Seen in live collection: {c['live_seen']}; exact timetable-pattern matches: {c['strict_live_matches']}",f"Recovered only by next-day check: {c['settlement_only']}; still unseen: {len(c['missing'])}",f"Missing final arrival after available backfill: {len(c['missing_terminal'])}"]
    else:lines.append('Timetable coverage: unavailable; do not assume complete.')
    lines+=['',f"Latest collector update: {local(r['latest_poll_at'])}" if r['latest_poll_at'] else 'Latest collector update: missing',f"Collection stop: {local(r['expected_end'])}; final movement checks stop: {local(r['tail_end'])}",'','Checks:']
    lines += ['- '+s for s in r['warnings']] if r['warnings'] else ['- Everything looks okay for the checks completed. This is not a guarantee that every source time is accurate.']
    if c['missing']:lines+=['Unseen journeys: '+', '.join(c['missing'][:20])]
    if c['missing_terminal']:lines+=['Missing final arrivals: '+', '.join(c['missing_terminal'][:20])]
    lines+=['','Original records and next-day backfill are kept separately.','This report is saved in the private S3 archive as well as sent by email.']
    return '\n'.join(lines)

def handler(event,context):
    import boto3
    s3,ddb=boto3.client('s3'),boto3.client('dynamodb');bucket,prefix,table=os.environ['BUCKET'],os.environ['PREFIX'],os.environ['TABLE'];now=dt.datetime.now(dt.timezone.utc);initial=event.get('action')=='initial';day=now.astimezone(TZ).date() if initial else now.astimezone(TZ).date()-dt.timedelta(days=1);ident=('initial-' if initial else '')+day.isoformat();sentkey=prefix+'|report-sent|'+ident
    if ddb.get_item(TableName=table,Key={'pk':{'S':sentkey}},ConsistentRead=True).get('Item'):return {'status':'already_published','report':ident}
    if not claim(ddb,table,prefix+'|report-lock|'+ident,time.time(),930):return {'status':'busy'}
    try:
        r=build(s3,ddb,boto3.client('ce',region_name='us-east-1'),boto3.client('cloudwatch'),now,initial)
        text=text_report(r);put_json(s3,bucket,prefix+'/daily-reports/'+ident+'.json',r)
        s3.put_object(Bucket=bucket,Key=prefix+'/daily-reports/'+ident+'.txt',Body=text.encode(),ContentType='text/plain',ServerSideEncryption='AES256')
        sns=boto3.client('sns');subscribers=sns.list_subscriptions_by_topic(TopicArn=os.environ['REPORT_TOPIC'])['Subscriptions']
        confirmed=any(s['Protocol']=='email' and s['Endpoint']==os.environ['REPORT_EMAIL'] and s['SubscriptionArn']!='PendingConfirmation' for s in subscribers)
        if not confirmed:return {'status':'email_confirmation_pending','report':ident,'health':r['status']}
        response=sns.publish(TopicArn=os.environ['REPORT_TOPIC'],Subject=('DART '+('initial check' if initial else 'daily report')+': '+r['status']+' - '+r['day']),Message=text)
        ddb.put_item(TableName=table,Item={'pk':{'S':sentkey},'message_id':{'S':response['MessageId']},'published_at':{'N':str(time.time())}})
        return {'status':'published','report':ident,'health':r['status'],'message_id':response['MessageId']}
    finally:ddb.delete_item(TableName=table,Key={'pk':{'S':prefix+'|report-lock|'+ident}})
