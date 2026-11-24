"""Produce CloudFormation JSON; keep deploy-time choices as explicit parameters."""
import json
from pathlib import Path

def ref(s):return {'Ref':s}
def sub(s):return {'Fn::Sub':s}
def attr(s,a='Arn'):return {'Fn::GetAtt':[s,a]}

def template():
    resources={
      'Archive':{'Type':'AWS::S3::Bucket','DeletionPolicy':'Retain','UpdateReplacePolicy':'Retain','Properties':{'VersioningConfiguration':{'Status':'Enabled'},'PublicAccessBlockConfiguration':{k:True for k in ['BlockPublicAcls','BlockPublicPolicy','IgnorePublicAcls','RestrictPublicBuckets']},'BucketEncryption':{'ServerSideEncryptionConfiguration':[{'ServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]}}},
      'State':{'Type':'AWS::DynamoDB::Table','DeletionPolicy':'Retain','UpdateReplacePolicy':'Retain','Properties':{'BillingMode':'PAY_PER_REQUEST','AttributeDefinitions':[{'AttributeName':'pk','AttributeType':'S'}],'KeySchema':[{'AttributeName':'pk','KeyType':'HASH'}],'SSESpecification':{'SSEEnabled':True}}},
      'WorkerRole':{'Type':'AWS::IAM::Role','Properties':{'AssumeRolePolicyDocument':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'lambda.amazonaws.com'},'Action':'sts:AssumeRole'}]},'Policies':[{'PolicyName':'collector','PolicyDocument':{'Version':'2012-10-17','Statement':[
        {'Effect':'Allow','Action':['s3:GetObject','s3:PutObject','s3:AbortMultipartUpload'],'Resource':sub('${Archive.Arn}/*')},
        {'Effect':'Allow','Action':'s3:ListBucket','Resource':attr('Archive')},
        {'Effect':'Allow','Action':['dynamodb:GetItem','dynamodb:PutItem','dynamodb:DeleteItem'],'Resource':attr('State')},
        {'Fn::If':['UseNta',{'Effect':'Allow','Action':['secretsmanager:GetSecretValue'],'Resource':ref('NtaSecretArn')},ref('AWS::NoValue')]},
        {'Effect':'Allow','Action':['logs:CreateLogStream','logs:PutLogEvents'],'Resource':sub('arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${AWS::StackName}*:*')},
      ]}}]}},
      'Logs':{'Type':'AWS::Logs::LogGroup','Properties':{'LogGroupName':sub('/aws/lambda/${AWS::StackName}'),'RetentionInDays':14}},
      'Worker':{'Type':'AWS::Lambda::Function','DependsOn':'Logs','Properties':{'FunctionName':ref('AWS::StackName'),'Runtime':'python3.12','Handler':'aws.handler.handler','Role':attr('WorkerRole'),'MemorySize':1024,'Timeout':900,'EphemeralStorage':{'Size':2048},'Code':{'S3Bucket':ref('CodeBucket'),'S3Key':ref('CodeKey')},'Environment':{'Variables':{'BUCKET':ref('Archive'),'TABLE':ref('State'),'PREFIX':ref('TrialPrefix'),'START':ref('StartEpoch'),'END':ref('EndEpoch'),'INDEX_KEY':ref('IndexKey'),'STATIC_CHECKED':ref('StaticChecked'),'NTA_SECRET_ARN':ref('NtaSecretArn'),'NTA_ENABLED':ref('EnableNta')}}}},
      'AsyncPolicy':{'Type':'AWS::Lambda::EventInvokeConfig','Properties':{'FunctionName':ref('Worker'),'Qualifier':'$LATEST','MaximumRetryAttempts':0,'MaximumEventAgeInSeconds':60}},
      'SchedulerRole':{'Type':'AWS::IAM::Role','Properties':{'AssumeRolePolicyDocument':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'scheduler.amazonaws.com'},'Action':'sts:AssumeRole','Condition':{'StringEquals':{'aws:SourceAccount':ref('AWS::AccountId')}}}]},'Policies':[{'PolicyName':'invoke-collector','PolicyDocument':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'lambda:InvokeFunction','Resource':attr('Worker')}]}}]}},
      'Schedule':{'Type':'AWS::Scheduler::Schedule','Properties':{'Name':ref('AWS::StackName'),'State':ref('ScheduleState'),'ScheduleExpression':'rate(2 minutes)','ScheduleExpressionTimezone':'UTC','StartDate':ref('StartIso'),'EndDate':ref('TailEndIso'),'FlexibleTimeWindow':{'Mode':'OFF'},'Target':{'Arn':attr('Worker'),'RoleArn':attr('SchedulerRole'),'Input':'{}','RetryPolicy':{'MaximumRetryAttempts':0,'MaximumEventAgeInSeconds':60}}}},
      'Errors':{'Type':'AWS::CloudWatch::Alarm','Properties':{'AlarmDescription':'Collector invocation failures; inspect S3 latest.json and logs. No notification destination is configured.','Namespace':'AWS/Lambda','MetricName':'Errors','Dimensions':[{'Name':'FunctionName','Value':ref('Worker')}],'Statistic':'Sum','Period':300,'EvaluationPeriods':1,'Threshold':1,'ComparisonOperator':'GreaterThanOrEqualToThreshold','TreatMissingData':'notBreaching'}},
    }
    params={k:{'Type':'String'} for k in ['CodeBucket','CodeKey','NtaSecretArn','TrialPrefix','StartEpoch','EndEpoch','StartIso','TailEndIso','IndexKey','StaticChecked']}
    params['ScheduleState']={'Type':'String','AllowedValues':['DISABLED','ENABLED'],'Default':'DISABLED'}
    params.update({'EnableNta':{'Type':'String','AllowedValues':['true','false'],'Default':'false'},'ReportEmail':{'Type':'String'},'ReportEndIso':{'Type':'String'}})
    params['NtaSecretArn']['Default']=''
    resources['ReportTopic']={'Type':'AWS::SNS::Topic','Properties':{'DisplayName':'DART collection checks'}}
    resources['ReportSubscription']={'Type':'AWS::SNS::Subscription','Properties':{'TopicArn':ref('ReportTopic'),'Protocol':'email','Endpoint':ref('ReportEmail')}}
    resources['ReportTopicPolicy']={'Type':'AWS::SNS::TopicPolicy','Properties':{'Topics':[ref('ReportTopic')],'PolicyDocument':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'cloudwatch.amazonaws.com'},'Action':'sns:Publish','Resource':ref('ReportTopic'),'Condition':{'StringEquals':{'aws:SourceAccount':ref('AWS::AccountId')},'ArnLike':{'aws:SourceArn':sub('arn:${AWS::Partition}:cloudwatch:${AWS::Region}:${AWS::AccountId}:alarm:*')}}}]}}}
    import copy
    report_role=copy.deepcopy(resources['WorkerRole'])
    statements=report_role['Properties']['Policies'][0]['PolicyDocument']['Statement']
    statements[:]=[x for x in statements if 'Fn::If' not in x]
    statements.extend([{'Effect':'Allow','Action':['ce:GetCostAndUsage','cloudwatch:GetMetricStatistics'],'Resource':'*'},{'Effect':'Allow','Action':['sns:Publish','sns:ListSubscriptionsByTopic'],'Resource':ref('ReportTopic')}])
    resources['ReportRole']=report_role
    for ident,suffix,handler,role,expression in [('Report','daily-report','aws.daily_report.handler','ReportRole','cron(0 9 * * ? *)'),('Settlement','settlement','aws.settlement.handler','WorkerRole','cron(0 6 * * ? *)')]:
        name=sub('${AWS::StackName}-'+suffix)
        resources[ident+'Logs']={'Type':'AWS::Logs::LogGroup','Properties':{'LogGroupName':sub('/aws/lambda/${AWS::StackName}-'+suffix),'RetentionInDays':14}}
        props=copy.deepcopy(resources['Worker']['Properties']);props.update(FunctionName=name,Handler=handler,Role=attr(role));props['Environment']['Variables'].update(WORKER_FUNCTION=ref('Worker'),REPORT_TOPIC=ref('ReportTopic'),REPORT_EMAIL=ref('ReportEmail'))
        resources[ident+'Function']={'Type':'AWS::Lambda::Function','DependsOn':ident+'Logs','Properties':props}
        resources[ident+'AsyncPolicy']={'Type':'AWS::Lambda::EventInvokeConfig','Properties':{'FunctionName':ref(ident+'Function'),'Qualifier':'$LATEST','MaximumRetryAttempts':1,'MaximumEventAgeInSeconds':3600}}
        resources[ident+'Schedule']={'Type':'AWS::Scheduler::Schedule','Properties':{'Name':name,'State':ref('ScheduleState'),'ScheduleExpression':expression,'ScheduleExpressionTimezone':'Europe/Dublin','StartDate':ref('StartIso'),'EndDate':ref('ReportEndIso'),'FlexibleTimeWindow':{'Mode':'OFF'},'Target':{'Arn':attr(ident+'Function'),'RoleArn':attr('SchedulerRole'),'Input':'{}','RetryPolicy':{'MaximumRetryAttempts':1,'MaximumEventAgeInSeconds':3600}}}}
        alarm=copy.deepcopy(resources['Errors']);alarm['Properties'].update(AlarmDescription=ident+' job failed; inspect its logs and private archive.',Dimensions=[{'Name':'FunctionName','Value':ref(ident+'Function')}],AlarmActions=[ref('ReportTopic')]);resources[ident+'Errors']=alarm
    resources['Errors']['Properties'].update(AlarmDescription='Collector invocation failed; inspect S3 latest.json and logs.',AlarmActions=[ref('ReportTopic')])
    resources['SchedulerRole']['Properties']['Policies'][0]['PolicyDocument']['Statement'][0]['Resource']=[attr(n) for n in ['Worker','ReportFunction','SettlementFunction']]
    outputs={'Bucket':{'Value':ref('Archive')},'Table':{'Value':ref('State')},'Function':{'Value':ref('Worker')},'Schedule':{'Value':ref('Schedule')}}
    outputs.update({n:{'Value':ref(n)} for n in ['ReportFunction','SettlementFunction','ReportTopic','ReportSchedule','SettlementSchedule']})
    return {'AWSTemplateFormatVersion':'2010-09-09','Description':'Bounded DART collection with next-day settlement and daily cost/data email.','Parameters':params,'Conditions':{'UseNta':{'Fn::Equals':[ref('EnableNta'),'true']}},'Resources':resources,'Outputs':outputs}
if __name__=='__main__':
    Path('aws/template.json').write_text(json.dumps(template(),indent=2)+'\n')
