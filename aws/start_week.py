"""Start a bounded Irish Rail-only week in the existing retained archive stack.

Run once with --apply. The receipt records the unique prefix and exact stop dates.
Re-running an already successful deployment starts a NEW week; daily runtime jobs
are independently idempotent. This command never removes existing archive data.
"""
import argparse,datetime as dt,hashlib,json,sys,time
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aws.deploy import ROOT,prepare,scheduler_time
from gtfs_timetables import archive_feed


def run(args):
    package,template=prepare(args.build)
    if not args.apply:
        print(json.dumps({'package':str(package),'template':str(template),'deployed':False}));return
    import boto3
    session=boto3.Session(profile_name=args.profile,region_name=args.region)
    cf,s3,lam,scheduler=(session.client(n) for n in ['cloudformation','s3','lambda','scheduler'])
    old=cf.describe_stacks(StackName=args.stack)['Stacks'][0]
    if old['StackStatus'] not in {'CREATE_COMPLETE','UPDATE_COMPLETE','UPDATE_ROLLBACK_COMPLETE'}:raise ValueError('Stack is not stable')
    oldparams={p['ParameterKey']:p['ParameterValue'] for p in old['Parameters']}
    outputs={p['OutputKey']:p['OutputValue'] for p in old['Outputs']}
    anchors=json.loads((ROOT/'docs/dart-api-sample.json').read_text())['stations']['rows']
    folder,index=archive_feed('irish-rail',Path('data/timetables'),anchors)
    checked=time.time();sha=index['sha256'];indexkey=f'timetables/irish-rail/{sha}/rail-index.json'
    for path in folder.iterdir():
        if path.is_file():s3.upload_file(str(path),outputs['Bucket'],f'timetables/irish-rail/{sha}/{path.name}',ExtraArgs={'ServerSideEncryption':'AES256'})
    suffix=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ');name='dart-week-'+suffix;prefix='weeks/'+name
    codekey='builds/'+name+'/collector.zip';s3.upload_file(str(package),oldparams['CodeBucket'],codekey,ExtraArgs={'ServerSideEncryption':'AES256'})
    start=dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=30)
    def dates(start):
        end=start+dt.timedelta(days=7);tail=end+dt.timedelta(hours=2)
        reportend=dt.datetime.combine(end.astimezone(ZoneInfo('Europe/Dublin')).date()+dt.timedelta(days=1),dt.time(10),ZoneInfo('Europe/Dublin'))
        return {'StartEpoch':str(start.timestamp()),'EndEpoch':str(end.timestamp()),'StartIso':scheduler_time(start),'TailEndIso':scheduler_time(tail),'ReportEndIso':scheduler_time(reportend)}
    values={'CodeBucket':oldparams['CodeBucket'],'CodeKey':codekey,'NtaSecretArn':'','TrialPrefix':prefix,'IndexKey':indexkey,'StaticChecked':str(checked),'EnableNta':'false','ReportEmail':args.email,'ScheduleState':'DISABLED',**dates(start)}
    receipt={'name':name,'stack':args.stack,'prefix':prefix,'region':args.region,'email':args.email,'status':'updating_disabled','parameters':values,**outputs}
    path=args.build/(name+'.json')
    def save():path.write_text(json.dumps(receipt,indent=2)+'\n')
    def update():
        cf.update_stack(StackName=args.stack,TemplateBody=template.read_text(),Parameters=[{'ParameterKey':k,'ParameterValue':v} for k,v in values.items()],Capabilities=['CAPABILITY_IAM'])
        cf.get_waiter('stack_update_complete').wait(StackName=args.stack,WaiterConfig={'Delay':10,'MaxAttempts':90})
    save();print('Updating existing stack, all schedules disabled: '+name,flush=True);update()
    outputs={p['OutputKey']:p['OutputValue'] for p in cf.describe_stacks(StackName=args.stack)['Stacks'][0]['Outputs']};receipt.update(outputs);save()
    print('Email subscription created; checking deployed Irish Rail access.',flush=True)
    def invoke(function,event):
        response=lam.invoke(FunctionName=function,InvocationType='RequestResponse',Payload=json.dumps(event).encode());body=json.loads(response['Payload'].read())
        if response.get('FunctionError'):raise RuntimeError(json.dumps(body))
        return body
    preflight=invoke(outputs['Function'],{'action':'preflight'})
    if preflight.get('status')!='ready':raise RuntimeError('Preflight failed: '+json.dumps(preflight))
    start=dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=60);values.update(dates(start),ScheduleState='ENABLED');receipt.update(status='enabling',preflight=preflight);save();update()
    for namekey in ['Schedule','ReportSchedule','SettlementSchedule']:
        schedule=scheduler.get_schedule(Name=outputs[namekey])
        if schedule['State']!='ENABLED':raise RuntimeError(namekey+' did not enable')
    receipt.update(status='scheduled',parameters=values,start=start.isoformat(),discovery_ends=dt.datetime.fromtimestamp(float(values['EndEpoch']),dt.timezone.utc).isoformat(),tail_ends=values['TailEndIso']);save()
    print('Enabled. Receipt: '+str(path),flush=True)
    # Wait at most one minute, and obtain a real collection result before reporting success.
    while time.time()<start.timestamp():time.sleep(min(5,start.timestamp()-time.time()))
    receipt['first_collection']=invoke(outputs['Function'],{})
    receipt['latest']=json.loads(s3.get_object(Bucket=outputs['Bucket'],Key=prefix+'/latest.json')['Body'].read())
    if not receipt['latest'].get('requests'):raise RuntimeError('No actual source requests archived')
    receipt['initial_report']=invoke(outputs['ReportFunction'],{'action':'initial'})
    receipt['status']='running_verified';save()
    print(json.dumps({k:v for k,v in receipt.items() if k not in {'latest','parameters'}},indent=2),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');p.add_argument('--profile',default='dart');p.add_argument('--region',default='eu-west-1');p.add_argument('--stack',default='dart-comparison-20261121205725');p.add_argument('--email',default='ivan.caffrey@gmail.com');p.add_argument('--build',type=Path,default=Path('data/aws-build'));run(p.parse_args())
