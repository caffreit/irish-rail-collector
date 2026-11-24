"""Cache immutable trial evidence for a reproducible offline coverage audit."""
import argparse,concurrent.futures,json
from pathlib import Path

def download(receipt_path,profile,directory):
    import boto3
    from botocore.config import Config
    receipt=json.loads(receipt_path.read_text());s=boto3.Session(profile_name=profile,region_name=receipt['region']);s3=s.client('s3',config=Config(max_pool_connections=24));bucket=receipt['Bucket'];prefix='trials/'+receipt['stack'];directory.mkdir(parents=True,exist_ok=True)
    def keys(prefix):
        return [o['Key'] for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=prefix) for o in page.get('Contents',[])]
    def cache(key):
        path=directory/'objects'/key
        if not path.exists():
            path.parent.mkdir(parents=True,exist_ok=True);body=s3.get_object(Bucket=bucket,Key=key)['Body'].read();tmp=path.with_suffix(path.suffix+'.download');tmp.write_bytes(body);tmp.replace(path)
        return path
    def batch(ks):
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:return list(pool.map(cache,sorted(set(ks))))
    poll_paths=batch(keys(prefix+'/polls/'));polls=[json.loads(p.read_text()) for p in poll_paths]
    print('Cached '+str(len(polls))+' poll manifests',flush=True)
    indexkeys={p['timetable_index_key'] for p in polls};batch(indexkeys)
    batch(keys(prefix+'/timetable-checks/'))
    rawkeys={r['raw_key'] for p in polls for r in p['requests'] if r['source']=='irishrail' and r.get('raw_key')}
    batch(rawkeys);print('Cached '+str(len(rawkeys))+' Irish Rail response bodies',flush=True)
    nta_paths=batch(keys(prefix+'/nta-scoped/'));print('Cached '+str(len(nta_paths))+' NTA scoped snapshots',flush=True)
    state=s.client('dynamodb').get_item(TableName=receipt['Table'],Key={'pk':{'S':'state'}},ConsistentRead=True).get('Item')
    (directory/'state.json').write_text(state['data']['S'] if state else '{}')
    (directory/'receipt.json').write_text(json.dumps(receipt,indent=2))
    inventory={'bucket':bucket,'prefix':prefix,'polls':len(polls),'irishrail_bodies':len(rawkeys),'nta_snapshots':len(nta_paths),'timetable_indexes':sorted(indexkeys)}
    (directory/'inventory.json').write_text(json.dumps(inventory,indent=2));print(json.dumps(inventory,indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--receipt',type=Path,required=True);p.add_argument('--profile',default='dart');p.add_argument('--directory',type=Path,default=Path('data/coverage-audit'));a=p.parse_args();download(a.receipt,a.profile,a.directory)
