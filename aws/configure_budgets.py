"""Create or verify the user's two account-wide, notification-only AWS budgets."""
import argparse,datetime as dt,json
from pathlib import Path
from decimal import Decimal

def configure(profile,email,output):
    import boto3
    from botocore.exceptions import ClientError
    session=boto3.Session(profile_name=profile,region_name='us-east-1')
    account=session.client('sts').get_caller_identity()['Account'];client=session.client('budgets')
    receipt={'account':account,'email':email,'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'budgets':[],'hard_cap':False}
    costs={k:True for k in ['IncludeTax','IncludeSubscription','IncludeRecurring','IncludeOtherSubscription','IncludeUpfront','IncludeSupport','IncludeCredit','IncludeRefund','IncludeDiscount']}
    costs.update(UseBlended=False,UseAmortized=False)
    for period,amount in [('MONTHLY','10'),('DAILY','5')]:
        name=f'Account-{period.lower()}-{amount}USD'
        notifications=[{'Notification':{'NotificationType':'ACTUAL','ComparisonOperator':'GREATER_THAN','Threshold':t,'ThresholdType':'PERCENTAGE'},'Subscribers':[{'SubscriptionType':'EMAIL','Address':email}]} for t in [50,80,100]]
        now=dt.datetime.now(dt.timezone.utc);start=now.replace(hour=0,minute=0,second=0,microsecond=0)
        if period=='MONTHLY':start=start.replace(day=1)
        wanted={'BudgetName':name,'BudgetLimit':{'Amount':amount,'Unit':'USD'},'TimeUnit':period,'BudgetType':'COST','CostFilters':{},'CostTypes':costs,'TimePeriod':{'Start':start}}
        try:existing=client.describe_budget(AccountId=account,BudgetName=name)['Budget']
        except ClientError as exc:
            if exc.response['Error']['Code']!='NotFoundException':raise
            client.create_budget(AccountId=account,Budget=wanted,NotificationsWithSubscribers=notifications)
            existing=None
        if existing is not None:
            if ((Decimal(existing['BudgetLimit']['Amount'])!=Decimal(amount) or existing['BudgetLimit']['Unit']!='USD') or existing['TimeUnit']!=period or existing.get('CostFilters') or existing.get('FilterExpression')):
                raise ValueError('Refusing to overwrite a conflicting existing budget: '+name)
            present=client.describe_notifications_for_budget(AccountId=account,BudgetName=name).get('Notifications',[])
            for desired in notifications:
                n=desired['Notification']
                match=next((v for v in present if all(v.get(k)==v2 for k,v2 in n.items())),None)
                if not match:client.create_notification(AccountId=account,BudgetName=name,**desired)
                else:
                    subs=client.describe_subscribers_for_notification(AccountId=account,BudgetName=name,Notification=n).get('Subscribers',[])
                    if not any(s['SubscriptionType']=='EMAIL' and s['Address']==email for s in subs):
                        client.create_subscriber(AccountId=account,BudgetName=name,Notification=n,Subscriber={'SubscriptionType':'EMAIL','Address':email})
        actual=client.describe_budget(AccountId=account,BudgetName=name)['Budget']
        verified=[]
        for desired in notifications:
            n=desired['Notification'];subs=client.describe_subscribers_for_notification(AccountId=account,BudgetName=name,Notification=n).get('Subscribers',[])
            if not any(s['SubscriptionType']=='EMAIL' and s['Address']==email for s in subs):raise RuntimeError('Budget subscriber verification failed')
            verified.append(n)
        receipt['budgets'].append({'name':name,'period':actual['TimeUnit'],'limit':actual['BudgetLimit'],'cost_types':actual.get('CostTypes'),'time_period':actual['TimePeriod'],'verified_alerts':verified,'created':existing is None})
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(receipt,indent=2,default=str))
    print(json.dumps(receipt,indent=2,default=str))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--profile',default='dart');p.add_argument('--email',required=True);p.add_argument('--output',type=Path,default=Path('data/aws-budget-receipt.json'));a=p.parse_args();configure(a.profile,a.email,a.output)
