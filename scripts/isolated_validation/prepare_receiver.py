from pathlib import Path
p=Path(__file__).resolve().parents[2]/"run/v8-live/mock_delivery_agent.py"
s=p.read_text().replace("import threading", "import threading\nimport time\nfrom datetime import datetime, timezone")
s=s.replace("        self.evidence_bytes = 0", "        self.started = time.monotonic()\n        self.dropped_responses = set()\n        self.gallery_counts = {}\n        self.evidence_bytes = 0")
s=s.replace("                \"sequences\": sequences,\n            }\n            with self.log_path.open", "                \"sequences\": sequences,\n                \"accepted_at\": datetime.now(timezone.utc).isoformat(),\n                \"accepted_mono\": time.monotonic(),\n                \"kind\": observation.get(\"kind\"),\n            }\n            key = (camera_id,event_type,observation.get(\"kind\"))\n            if self.gallery_counts.get(key,0) < 2:\n                self.gallery_counts[key]=self.gallery_counts.get(key,0)+1\n                directory=self.log_path.parent/\"gallery\"/package_id\n                directory.mkdir(parents=True,exist_ok=True)\n                (directory/\"manifest.json\").write_text(json.dumps(manifest))\n                for name,data in ordered_parts:\n                    (directory/(name+\".jpg\")).write_bytes(data)\n            with self.log_path.open")
needle="            status, payload = self.store.accept(manifest, parts)"
replacement="""            fault=json.loads(os.environ.get('SENTRY_TEST_AGENT_POLICY','{}'))
            elapsed=time.monotonic()-self.store.started
            active=fault.get('start',10)<=elapsed<fault.get('end',30)
            mode=fault.get('mode')
            if active and mode in ('503','429','400','401'):
                status=int(mode); payload={'error':{'code':'injected_'+mode,'message':'qualification fault','retryable':status in (503,429)}}
            else:
                status, payload = self.store.accept(manifest, parts)
                if active and mode=='slow':time.sleep(float(fault.get('delay',2)))
                if active and mode=='drop_ack' and manifest['package_id'] not in self.store.dropped_responses:
                    self.store.dropped_responses.add(manifest['package_id'])
                    self.connection.close()
                    return"""
assert needle in s;s=s.replace(needle,replacement)
p.write_text(s)
