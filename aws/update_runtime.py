"""Deploy runtime fixes without changing an existing run's window or prefix."""
import argparse,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aws.deploy import prepare

def update(receipt,profile):
    import boto3
    r=json.loads(receipt.read_text());package,_=prepare(receipt.parent)
    digest=hashlib.sha256(package.read_bytes()).hexdigest();key='builds/'+r['name']+'/'+digest+'.zip'
    s=boto3.Session(profile_name=profile,region_name=r['region']);cf=s.client('cloudformation');old=cf.describe_stacks(StackName=r['stack'])['Stacks'][0]
    params={p['ParameterKey']:p['ParameterValue'] for p in old['Parameters']}
    if params['TrialPrefix']!=r['prefix']:raise ValueError('Receipt does not identify the active run')
    s.client('s3').upload_file(str(package),params['CodeBucket'],key,ExtraArgs={'ServerSideEncryption':'AES256'})
    cf.update_stack(StackName=r['stack'],UsePreviousTemplate=True,Parameters=[{'ParameterKey':k,**({'ParameterValue':key} if k=='CodeKey' else {'UsePreviousValue':True})} for k in params],Capabilities=['CAPABILITY_IAM'])
    cf.get_waiter('stack_update_complete').wait(StackName=r['stack'],WaiterConfig={'Delay':10,'MaxAttempts':90})
    r['parameters']['CodeKey']=key;r['runtime_sha256']=digest;receipt.write_text(json.dumps(r,indent=2)+'\n')
    print('Runtime updated; original dates and prefix retained.',flush=True)
    lam=s.client('lambda')
    for field,event in [('SettlementFunction',{'action':'preflight'}),('ReportFunction',{'action':'initial'})]:
        response=lam.invoke(FunctionName=r[field],InvocationType='RequestResponse',Payload=json.dumps(event).encode());body=json.loads(response['Payload'].read());print(field,json.dumps(body),flush=True)
        if response.get('FunctionError'):raise RuntimeError('Deployed verification failed')
        r[field+'_verification']=body
    receipt.write_text(json.dumps(r,indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--receipt',type=Path,required=True);p.add_argument('--profile',default='dart');a=p.parse_args();update(a.receipt,a.profile)
