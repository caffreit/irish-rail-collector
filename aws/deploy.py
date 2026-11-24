"""Prepare locally, then deploy a fresh, disabled-first AWS comparison trial."""
import argparse,datetime as dt,hashlib,json,sys,time,zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aws.template_builder import template
from gtfs_timetables import archive_feed

ROOT=Path(__file__).resolve().parents[1]

def scheduler_time(value):
    return value.astimezone(dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')

def prepare(directory):
    directory.mkdir(parents=True,exist_ok=True)
    package=directory/'collector.zip'
    with zipfile.ZipFile(package,'w',zipfile.ZIP_DEFLATED) as z:
        for name in ['dart_trial.py','gtfs_timetables.py','nta_comparison.py','docs/dart-api-sample.json']:z.write(ROOT/name,name)
        for name in ['__init__.py','handler.py','evidence.py','report_core.py','daily_report.py','settlement.py']:z.write(ROOT/'aws'/name,'aws/'+name)
    path=directory/'template.json';path.write_text(json.dumps(template(),indent=2))
    return package,path

def deploy(args):
    package,template_path=prepare(args.build)
    if not args.apply:
        print(json.dumps({'prepared_package':str(package),'template':str(template_path),'deployed':False},indent=2));return
    if not args.nta_secret_arn and not args.nta_key_file:raise ValueError('Supply an existing NTA secret ARN or a private key file')
    # Refresh public snapshots before any account changes. Never label an old
    # local import as checked at deployment time.
    anchors=json.loads((ROOT/'docs/dart-api-sample.json').read_text())['stations']['rows']
    for source in ['irish-rail','realtime-operators']:
        archive_feed(source,args.timetables,anchors)
    static_checked=time.time()
    # Verify the input snapshots before making any account changes.
    inputs={}
    for source in ['irish-rail','realtime-operators']:
        dirs=list((args.timetables/source).glob('*/rail-index.json'))
        if not dirs:raise ValueError('Archive '+source+' timetable first')
        selected=max(dirs,key=lambda p:p.stat().st_mtime)
        index=json.loads(selected.read_text());feed=selected.parent/'feed.zip'
        digest=hashlib.file_digest(feed.open('rb'),'sha256').hexdigest()
        if digest!=index['sha256']:raise ValueError('Timetable archive hash mismatch')
        inputs[source]=(selected.parent,index)
    import boto3
    session=boto3.Session(profile_name=args.profile,region_name=args.region)
    sts=session.client('sts');account=sts.get_caller_identity()['Account']
    cf,s3,secrets,lam,scheduler=(session.client(s) for s in ['cloudformation','s3','secretsmanager','lambda','scheduler'])
    cf.validate_template(TemplateBody=template_path.read_text())
    suffix=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')
    stack='dart-comparison-'+suffix;codebucket='dart-builds-'+account+'-'+args.region
    # The build bucket is retained and reused; never change existing bucket settings.
    from botocore.exceptions import ClientError
    try:s3.head_bucket(Bucket=codebucket)
    except ClientError as exc:
        if exc.response['Error']['Code'] not in {'404','NoSuchBucket','NotFound'}:raise
        create={'Bucket':codebucket}
        if args.region!='us-east-1':create['CreateBucketConfiguration']={'LocationConstraint':args.region}
        s3.create_bucket(**create)
        s3.put_public_access_block(Bucket=codebucket,PublicAccessBlockConfiguration={k:True for k in ['BlockPublicAcls','BlockPublicPolicy','IgnorePublicAcls','RestrictPublicBuckets']})
    codekey='builds/'+stack+'/collector.zip';s3.upload_file(str(package),codebucket,codekey,ExtraArgs={'ServerSideEncryption':'AES256'})
    secret_arn=args.nta_secret_arn
    if not secret_arn:
        key=args.nta_key_file.read_text().strip()
        if not key:raise ValueError('Empty NTA key file')
        secret_arn=secrets.create_secret(Name=stack+'/nta-api-key',SecretString=key,Description='NTA key for bounded DART comparison')['ARN']
    # A fixed start chosen before stack creation cannot move forward after a failure/retry.
    start=dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=30);end=start+dt.timedelta(hours=24);tail=end+dt.timedelta(hours=2)
    sha=inputs['realtime-operators'][1]['sha256'];indexkey=f'timetables/realtime-operators/{sha}/rail-index.json'
    values={'CodeBucket':codebucket,'CodeKey':codekey,'NtaSecretArn':secret_arn,'TrialPrefix':'trials/'+stack,'StartEpoch':str(start.timestamp()),'EndEpoch':str(end.timestamp()),'StartIso':scheduler_time(start),'TailEndIso':scheduler_time(tail),'IndexKey':indexkey,'StaticChecked':str(static_checked),'ScheduleState':'DISABLED','EnableNta':'true','ReportEmail':'ivan.caffrey@gmail.com','ReportEndIso':scheduler_time(tail+dt.timedelta(days=1))}
    receipt={'stack':stack,'account':account,'region':args.region,'start':start.isoformat(),'discovery_ends':end.isoformat(),'tail_ends':tail.isoformat(),'code_bucket':codebucket,'secret_arn':secret_arn,'status':'creating'}
    receipt_path=args.build/(stack+'.json');receipt_path.write_text(json.dumps(receipt,indent=2))
    print('Creating disabled stack '+stack,flush=True)
    cf.create_stack(StackName=stack,TemplateBody=template_path.read_text(),Parameters=[{'ParameterKey':k,'ParameterValue':v} for k,v in values.items()],Capabilities=['CAPABILITY_IAM'],OnFailure='ROLLBACK',Tags=[{'Key':'Project','Value':'dart-comparison'}])
    cf.get_waiter('stack_create_complete').wait(StackName=stack,WaiterConfig={'Delay':15,'MaxAttempts':80})
    outputs={o['OutputKey']:o['OutputValue'] for o in cf.describe_stacks(StackName=stack)['Stacks'][0]['Outputs']}
    receipt.update(outputs);receipt['status']='uploading';receipt_path.write_text(json.dumps(receipt,indent=2))
    for source,(directory,index) in inputs.items():
        for path in directory.iterdir():
            if path.is_file():s3.upload_file(str(path),outputs['Bucket'],f"timetables/{source}/{index['sha256']}/{path.name}",ExtraArgs={'ServerSideEncryption':'AES256'})
    print('Checking deployed permissions and NTA access before enabling the schedule',flush=True)
    response=lam.invoke(FunctionName=outputs['Function'],InvocationType='RequestResponse',Payload=json.dumps({'action':'preflight'}).encode())
    body=json.loads(response['Payload'].read())
    if response.get('FunctionError') or body.get('status')!='ready':
        receipt.update(status='preflight_failed',preflight=body);receipt_path.write_text(json.dumps(receipt,indent=2));raise RuntimeError('Preflight failed. Schedule remains disabled; see receipt and S3 archive.')
    if time.time()>=start.timestamp():raise RuntimeError('Provisioning passed the chosen start; schedule left disabled. Choose a fresh trial.')
    cf.update_stack(StackName=stack,UsePreviousTemplate=True,Parameters=[{'ParameterKey':k,'ParameterValue':('ENABLED' if k=='ScheduleState' else v)} for k,v in values.items()],Capabilities=['CAPABILITY_IAM'])
    cf.get_waiter('stack_update_complete').wait(StackName=stack,WaiterConfig={'Delay':10,'MaxAttempts':80})
    actual=scheduler.get_schedule(Name=outputs['Schedule'])
    if actual['State']!='ENABLED':raise RuntimeError('Schedule failed to enable')
    receipt.update(status='scheduled',preflight=body);receipt_path.write_text(json.dumps(receipt,indent=2))
    print(json.dumps(receipt,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');p.add_argument('--profile');p.add_argument('--region',default='eu-west-1');p.add_argument('--nta-key-file',type=Path);p.add_argument('--nta-secret-arn');p.add_argument('--timetables',type=Path,default=Path('data/timetables'));p.add_argument('--build',type=Path,default=Path('data/aws-build'));args=p.parse_args()
    deploy(args)
