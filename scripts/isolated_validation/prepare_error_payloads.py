"""Keep injected TEST receiver error responses inside the V8 error schema."""
from pathlib import Path
r=Path(__file__).resolve().parents[2];p=r/'run/v8-live/mock_delivery_agent.py';s=p.read_text()
old="status=int(mode); payload={'error':{'code':'injected_'+mode,'message':'qualification fault','retryable':status in (503,429)}}"
new="status=int(mode); payload={'error':{'code':{'503':'durability_unavailable','429':'agent_busy','400':'invalid_package','401':'invalid_package'}[mode],'message':'qualification fault','retryable':status in (503,429)}}"
if old in s:s=s.replace(old,new);p.write_text(s)
else:assert new in s,'Unknown error injector'
import json,jsonschema
schema=json.loads((r/'newdetails/sentry-v8/delivery-agent-error.schema.json').read_text())
for status,code in [(503,'durability_unavailable'),(429,'agent_busy'),(400,'invalid_package'),(401,'invalid_package')]:jsonschema.validate({'error':{'code':code,'message':'qualification fault','retryable':status in(503,429)}},schema)
print('All injected error payloads validated;401 is an intentionally unexpected status, not a new authentication requirement')
