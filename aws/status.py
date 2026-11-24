"""Read actual AWS trial manifests and state; never read API secret values."""
import argparse,collections,concurrent.futures,datetime as dt,json,time
from pathlib import Path

def inspect(receipt_path,profile,output):
    import boto3
    r=json.loads(receipt_path.read_text());s=boto3.Session(profile_name=profile,region_name=r['region']);s3=s.client('s3');ddb=s.client('dynamodb');bucket=r['Bucket'];prefix=r.get('prefix','trials/'+r['stack'])
    def read(key):return json.loads(s3.get_object(Bucket=bucket,Key=key)['Body'].read())
    latest=read(prefix+'/latest.json');keys=[]
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=prefix+'/polls/'):
        keys.extend(o['Key'] for o in page.get('Contents',[]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:polls=list(pool.map(read,keys))
    counters=collections.defaultdict(collections.Counter);success=collections.defaultdict(list);errors=[]
    for p in polls:
        for req in p['requests']:
            key=req['source'];counters[key]['requests']+=1
            failed=bool(req.get('error') or req.get('parse_error'))
            counters[key]['failures']+=failed
            if failed:errors.append({k:req.get(k) for k in ['source','started','http_status','error','parse_error']})
            else:success[key].append(req['started'])
    state_raw=ddb.get_item(TableName=r['Table'],Key={'pk':{'S':prefix+'|state' if 'prefix' in r else 'state'}},ConsistentRead=True).get('Item')
    state=json.loads(state_raw['data']['S']) if state_raw else {}
    # The trial prefix contains fewer than 1,000 scoped snapshots in a normal 24-hour run,
    # but paginate anyway so a restart or prolonged investigation cannot hide the latest.
    nta=[]
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=prefix+'/nta-scoped/'):
        nta.extend(o['Key'] for o in page.get('Contents',[]))
    nta_latest=read(max(nta)) if nta else None
    clocks={source:{'first':min(ts),'latest':max(ts),'largest_success_gap_seconds':max([b-a for a,b in zip(sorted(ts),sorted(ts)[1:])],default=0)} for source,ts in success.items()}
    result={'checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'latest_age_seconds':time.time()-latest['observed_at'],'latest':latest,'poll_manifests':len(polls),'requests':{k:dict(v) for k,v in counters.items()},'successful_response_times':clocks,'services_by_scope':dict(collections.Counter(v['scope'] for v in state.get('services',{}).values())),'nta_latest_counts':nta_latest.get('counts') if nta_latest else None,'nta_latest_header':nta_latest.get('header') if nta_latest else None,'nta_latest_corridor_entities':len(nta_latest['corridor_entities']) if nta_latest else 0,'static_checked_at':state.get('static_checked'),'timetable_index_key':state.get('index_key'),'errors':errors,'discovery_ends':r['discovery_ends'],'tail_ends':r['tail_ends']}
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in {'latest','errors'}},indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--receipt',type=Path,required=True);p.add_argument('--profile',default='dart');p.add_argument('--output',type=Path,default=Path('data/aws-status.json'));a=p.parse_args();inspect(a.receipt,a.profile,a.output)
