"""Bounded AWS DART/NTA comparison. S3 raw archive + DynamoDB leases and state."""
import datetime as dt,gzip,hashlib,json,os,time,urllib.error,urllib.parse,urllib.request,uuid
from zoneinfo import ZoneInfo
from pathlib import Path
from dart_trial import BASE,STATIONS,parse_rows,service_key,in_corridor
from gtfs_timetables import archive_feed
from nta_comparison import URL,analyse_feed
from aws.evidence import optional_json,project

# boto3 is provided by the Lambda Python runtime. Import lazily for local testing.
def clients():
    import boto3
    return boto3.client('s3'),boto3.client('dynamodb'),boto3.client('secretsmanager')

def claim(ddb,table,key,now,seconds):
    try:
        ddb.put_item(TableName=table,Item={'pk':{'S':key},'until':{'N':str(int(now+seconds))}},ConditionExpression='attribute_not_exists(pk) OR #u <= :now',ExpressionAttributeNames={'#u':'until'},ExpressionAttributeValues={':now':{'N':str(int(now))}})
        return True
    except Exception as exc:
        if getattr(exc,'response',{}).get('Error',{}).get('Code')=='ConditionalCheckFailedException':return False
        raise

def get_json(s3,bucket,key):return json.loads(s3.get_object(Bucket=bucket,Key=key)['Body'].read())
def put_json(s3,bucket,key,value):s3.put_object(Bucket=bucket,Key=key,Body=json.dumps(value,separators=(',',':')).encode(),ContentType='application/json',ServerSideEncryption='AES256')

def archive_request(s3,bucket,prefix,source,url,headers=None):
    started=time.time();raw=None;status=None;error=None
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'dart-research-collector/2.0',**(headers or {})})
        with urllib.request.urlopen(req,timeout=15) as r:raw=r.read();status=r.status
    except urllib.error.HTTPError as exc:raw=exc.read();status=exc.code;error='HTTP '+str(exc.code)
    except (OSError,urllib.error.URLError) as exc:error=type(exc).__name__ # Never log request headers or secret values.
    path=f"{prefix}/raw/{source}/"+dt.datetime.fromtimestamp(started,dt.timezone.utc).strftime('%Y-%m-%d/%H/')+f'{int(started*1000)}-{uuid.uuid4().hex}'
    meta={'source':source,'url':url,'started':started,'finished':time.time(),'http_status':status,'error':error,'raw_key':None,'sha256':None}
    if raw is not None:
        meta['raw_key']=path+'.gz';meta['sha256']=hashlib.sha256(raw).hexdigest()
        compressed=gzip.compress(raw,mtime=0);meta['bytes_uncompressed']=len(raw);meta['bytes_compressed']=len(compressed)
        s3.put_object(Bucket=bucket,Key=meta['raw_key'],Body=compressed,ContentType='application/octet-stream',ServerSideEncryption='AES256')
    put_json(s3,bucket,path+'.json',meta)
    return meta,raw

def refreshed_index(s3,bucket,prefix):
    anchors=json.loads(Path('docs/dart-api-sample.json').read_text())['stations']['rows'];manifests={};indexes={}
    for source in (['irish-rail','realtime-operators'] if os.environ.get('NTA_ENABLED','true')=='true' else ['irish-rail']):
        target,index=archive_feed(source,Path('/tmp/timetables'),anchors)
        for name in ['feed.zip','manifest.json','rail-index.json']:
            key=f"timetables/{source}/{index['sha256']}/{name}"
            # Content-addressed object keys prevent timetable versions from replacing each other.
            try:s3.head_object(Bucket=bucket,Key=key)
            except Exception as exc:
                if getattr(exc,'response',{}).get('Error',{}).get('Code') not in {'404','NoSuchKey','NotFound'}:raise
                s3.upload_file(str(target/name),bucket,key,ExtraArgs={'ServerSideEncryption':'AES256'})
        indexes[source]=index;manifests[source]={'sha256':index['sha256'],'feed_info':index['feed_info']}
        # Keep warm Lambda disk usage bounded as versions change.
        import shutil
        shutil.rmtree(target)
    manifest={'checked_at':time.time(),'sources':manifests}
    put_json(s3,bucket,prefix+'/timetable-checks/'+str(int(time.time()))+'.json',manifest)
    return indexes.get('realtime-operators',indexes['irish-rail']),manifest

def execute(s3,ddb,secrets,context):
    bucket,table=os.environ['BUCKET'],os.environ['TABLE'];prefix=os.environ['PREFIX'];now=time.time()
    start,end=float(os.environ['START']),float(os.environ['END']);tail=end+7200
    if now<start:return {'status':'not_started'}
    if now>=tail:return {'status':'complete','ends':end}
    if not claim(ddb,table,prefix+'|worker',now,930):return {'status':'busy'}
    item=ddb.get_item(TableName=table,Key={'pk':{'S':prefix+'|state'}},ConsistentRead=True).get('Item')
    state=json.loads(item['data']['S']) if item else {'services':{},'boards':{},'static_checked':float(os.environ['STATIC_CHECKED']),'index_key':os.environ['INDEX_KEY']}
    results=[]
    try:
        if now<end and now-state['static_checked']>=21600:
            index,manifest=refreshed_index(s3,bucket,prefix)
            state.update(static_checked=time.time(),index_key=f"timetables/{'realtime-operators' if os.environ.get('NTA_ENABLED','true')=='true' else 'irish-rail'}/{index['sha256']}/rail-index.json",timetables=manifest)
        else:index=get_json(s3,bucket,state['index_key'])
        # Claim before fetching, persist independently of the rest of the invocation.
        # Every NTA request, including a failure, consumes the shared minimum interval.
        if os.environ.get('NTA_ENABLED','true')=='true' and time.time()<end and claim(ddb,table,'nta-rate-limit',time.time(),65):
            secret=secrets.get_secret_value(SecretId=os.environ['NTA_SECRET_ARN'])['SecretString']
            meta,raw=archive_request(s3,bucket,prefix,'nta',URL,{'x-api-key':secret})
            results.append(meta)
            if raw is not None and meta['error'] is None:
                try:
                    parsed=analyse_feed(raw,index)
                    parsed['timetable_index_key']=state['index_key'];parsed['timetable_checked_at']=state.get('static_checked')
                    put_json(s3,bucket,prefix+'/nta-scoped/'+str(int(time.time()*1000))+'.json',parsed)
                    meta['counts']=parsed['counts']
                except (ValueError,KeyError,TypeError):meta['parse_error']='Invalid or changed NTA response schema'
        def enough():return context.get_remaining_time_in_millis()>25000 and time.time()<tail
        def ir(method,**params):
            meta,raw=archive_request(s3,bucket,prefix,'irishrail',BASE+method+'?'+urllib.parse.urlencode(params));results.append(meta)
            if raw is None or meta['error']:return []
            try:
                parsed=parse_rows(raw);meta['row_count']=len(parsed);return parsed
            except Exception as exc:
                meta['parse_error']=type(exc).__name__;return []
        def discover(row,active=False):
            code,date=service_key(row);key=code+'|'+date;s=state['services'].setdefault(key,{'code':code,'date':date,'first_seen':time.time(),'last_movement':0,'scope':'unresolved'})
            s['last_seen']=time.time()
            if active:s['last_active']=time.time()
            if row.get('trainstatus')=='R':s['last_running']=time.time()
        if time.time()<end and enough():
            positions=ir('getCurrentTrainsXML_WithTrainType',TrainType='D')
            for row in positions:discover(row,True)
            for station in sorted(STATIONS,key=lambda s:state['boards'].get(s,0)):
                if not enough() or time.time()>=end:break
                if time.time()-state['boards'].get(station,0)<1800:continue
                rows=ir('getStationDataByCodeXML_WithNumMins',StationCode=station,NumMins=90)
                state['boards'][station]=time.time()
                for row in rows:
                    if (row.get('traintype') or '').strip().upper()=='DART':discover(row)
        for key,s in sorted(state['services'].items(),key=lambda p:p[1]['last_movement']):
            if not enough():break
            # Follow known journeys for up to two hours after disappearance; never extend the trial deadline.
            interval=120 if time.time()-s.get('last_active',0)<300 else 300
            if time.time()-s['last_seen']>7200 or time.time()-s['last_movement']<interval:continue
            rows=ir('getTrainMovementsXML',TrainId=s['code'],TrainDate=dt.date.fromisoformat(s['date']).strftime('%d %b %Y'))
            s['last_movement']=time.time()
            if rows:
                if any(service_key(r)!=(s['code'],s['date']) for r in rows):raise ValueError('Irish Rail response identity mismatch')
                # Preserve undocumented states instead of dropping them as out of scope.
                types={r.get('locationtype') for r in rows}
                s['scope']='corridor' if in_corridor(rows) else 'outside' if {'O','D'}<=types else 'unresolved'
                request=results[-1]
                evidence_key=prefix+'/service-evidence/'+s['date']+'/'+s['code']+'.json'
                projected=project(rows,request,optional_json(s3,bucket,evidence_key))
                put_json(s3,bucket,evidence_key,{'code':s['code'],'service_date':s['date'],'scope':s['scope'],'first_seen':s['first_seen'],'last_running':s.get('last_running'),**projected})
        local_date=dt.datetime.now(ZoneInfo('Europe/Dublin')).date().isoformat()
        if time.time()<end and state.get('plan_date_key')!=local_date+'|'+state['index_key']:
            put_json(s3,bucket,prefix+'/plan-references/'+local_date+'/'+index['sha256']+'.json',{'index_key':state['index_key'],'captured_at':time.time()})
            state['plan_date_key']=local_date+'|'+state['index_key']
        # Settlement reads its own S3 evidence; old service identities need not grow
        # the DynamoDB state item indefinitely. Raw and derived S3 records remain.
        state['services']={k:v for k,v in state['services'].items() if time.time()-v['last_seen']<=172800}
        status={'observed_at':time.time(),'start':start,'discovery_ends':end,'tail_ends':tail,'phase':'collecting' if time.time()<end else 'final_movements','services':len(state['services']),'requests':results,'timetable_index_key':state['index_key']}
        put_json(s3,bucket,prefix+'/polls/'+dt.datetime.fromtimestamp(now,dt.timezone.utc).strftime('%Y-%m-%d/')+str(int(now*1000))+'.json',status)
        put_json(s3,bucket,prefix+'/latest.json',status)
        ddb.put_item(TableName=table,Item={'pk':{'S':prefix+'|state'},'data':{'S':json.dumps(state,separators=(',',':'))}})
        # Failed endpoint responses stay archived but also fail the invocation for CloudWatch alarms.
        if any(r.get('error') or r.get('parse_error') for r in results):raise RuntimeError('One or more archived requests failed; see poll manifest')
        return {'status':status['phase'],'services':len(state['services']),'requests':len(results)}
    finally:
        ddb.delete_item(TableName=table,Key={'pk':{'S':prefix+'|worker'}})

def handler(event,context):
    if event.get('action')=='preflight':
        s3,ddb,secrets=clients()
        index=get_json(s3,os.environ['BUCKET'],os.environ['INDEX_KEY'])
        if os.environ.get('NTA_ENABLED','true')!='true':
            meta,raw=archive_request(s3,os.environ['BUCKET'],os.environ['PREFIX']+'/preflight','irishrail',BASE+'getCurrentTrainsXML_WithTrainType?TrainType=D')
            if meta['error'] or raw is None:raise RuntimeError('Irish Rail preflight failed')
            parsed=parse_rows(raw)
            return {'status':'ready','source':'irishrail','running_list_rows':len(parsed),'rail_trips':len(index['trips'])}
        key=secrets.get_secret_value(SecretId=os.environ['NTA_SECRET_ARN'])['SecretString']
        if not claim(ddb,os.environ['TABLE'],'nta-rate-limit',time.time(),65):
            return {'status':'rate_limited','retry_after_seconds':65}
        meta,raw=archive_request(s3,os.environ['BUCKET'],os.environ['PREFIX'],'nta',URL,{'x-api-key':key})
        if meta['error'] or raw is None:raise RuntimeError('NTA preflight failed; see archived request')
        parsed=analyse_feed(raw,index)
        put_json(s3,os.environ['BUCKET'],os.environ['PREFIX']+'/preflight.json',parsed)
        return {'status':'ready','rail_trips':len(index['trips']),'nta_counts':parsed['counts']}
    return execute(*clients(),context)
