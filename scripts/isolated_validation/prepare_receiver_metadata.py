"""Extend TEST receiver audit records; wire requests/acknowledgements unchanged."""
from pathlib import Path
p=Path(__file__).resolve().parents[2]/'run/v8-live/mock_delivery_agent.py';s=p.read_text()
needle='                "observed_at": observation["observed_at"],'
addition='''                "observed_at": observation["observed_at"],
                "config_revision": observation.get("config_revision"),
                "source_frame_id": observation.get("source_frame_id"),
                "stream_session_id": observation.get("stream_session_id"),
                "use_case_id": observation.get("use_case_id"),'''
if addition not in s:
 assert needle in s;s=s.replace(needle,addition);p.write_text(s)
print('Receiver audit metadata verified; wire protocol unchanged')
