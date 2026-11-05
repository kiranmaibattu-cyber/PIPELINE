"""Synthetic credential markers only; no actual secrets in diagnostics."""
import json,logging,sys
sys.path.insert(0,'/opt/traffic-pilot/services/worker')
from traffic_pilot_runtime.runtime_logging import SupervisorJsonFormatter
from pipeline.structured_logging import JsonLogFormatter
cases=[]
for name,formatter in [('supervisor',SupervisorJsonFormatter()),('worker',JsonLogFormatter())]:
 for variant in ['message','string-extra','nested-extra']:
  record=logging.LogRecord('qualification',logging.WARNING,__file__,1,'test diagnostic',(),None)
  text='rtsps://dummyuser:QUALIFICATION_PASSWORD@localhost:443/test token=QUALIFICATION_TOKEN'
  if variant=='message':record.msg=text
  elif variant=='string-extra':record.reason=text
  else:record.diagnostic={'source':text,'token':'QUALIFICATION_TOKEN'}
  rendered=formatter.format(record)
  cases.append({'logger':name,'variant':variant,'passed':all(x not in rendered for x in ['QUALIFICATION_PASSWORD','QUALIFICATION_TOKEN']),'rendered':rendered})
print(json.dumps({'scope':'Actual baked formatters with synthetic input; a failure is potential leakage, not evidence of real credentials leaked','cases':cases,'passed':all(x['passed']for x in cases)},indent=2))
