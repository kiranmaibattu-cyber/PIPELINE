"""Patch TEST sender/receiver only, not any runtime image file."""
from pathlib import Path
r=Path(__file__).resolve().parents[2]
p=r/'scripts/v8_capacity_probe.py';s=p.read_text()
old='"--port", str(port), "--fixture", str(args.recording),'
new='"--port", str(port), "--fixture", (json.loads(os.environ["SENTRY_TEST_RECORDINGS"])[index] if os.environ.get("SENTRY_TEST_RECORDINGS") else str(args.recording)),'
if old in s:s=s.replace(old,new);p.write_text(s)
else:assert new in s,'Unknown marker sender branch'
p=r/'run/v8-live/mock_delivery_agent.py';s=p.read_text()
old='self.gallery_counts.get(key,0) < int(os.environ.get("SENTRY_TEST_GALLERY_LIMIT","2"))'
new='self.gallery_counts.get(key,0) < int(os.environ.get("SENTRY_TEST_SCENE_GALLERY_LIMIT",os.environ.get("SENTRY_TEST_GALLERY_LIMIT","2")) if event_type=="scene_sample" else os.environ.get("SENTRY_TEST_GALLERY_LIMIT","2"))'
if old in s:s=s.replace(old,new);p.write_text(s)
else:assert new in s,'Unknown gallery limit code'
print('Test-only suite marker paths and scene-specific gallery limit verified')
