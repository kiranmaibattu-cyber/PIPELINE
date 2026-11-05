from pathlib import Path
p=Path(__file__).resolve().parents[2]/'run/v8-live/mock_delivery_agent.py';s=p.read_text()
if 'SENTRY_TEST_RESTORE_ACKS' not in s:
 s=s.replace('        self.embedding_count = 0','''        self.embedding_count = 0
        # Optional restart fixture: preserve accepted ID/digest/sequence knowledge.
        # This is an API test receiver, not the production delivery agent.
        if os.environ.get("SENTRY_TEST_RESTORE_ACKS") and self.log_path.exists():
            for line in self.log_path.read_text().splitlines():
                try:
                    record=json.loads(line)
                    if "package_digest" not in record: continue
                    self.packages[record["package_id"]]=(record["package_digest"],record["sequences"])
                    self.next_sequence=max(self.next_sequence,max(x["sequence"] for x in record["sequences"])+1)
                except (ValueError,KeyError): pass''')
 s=s.replace('                "accepted_at":','                "package_digest": digest,\n                "accepted_at":')
 p.write_text(s)
p=Path(__file__).resolve().parents[2]/'scripts/isolated_validation/probe_case.py';s=p.read_text()
if 'case_actions import' not in s:
 s=s.replace('sys.argv=[sys.argv[0]]+rest','from case_actions import Actions\nactions=Actions(root,rest)\nsys.argv=[sys.argv[0]]+rest')
 s=s.replace("finally:\n tmp=root/'CURRENT.new'", "finally:\n actions.close()\n tmp=root/'CURRENT.new'")
 p.write_text(s)
