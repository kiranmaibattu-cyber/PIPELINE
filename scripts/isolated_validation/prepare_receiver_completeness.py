"""Tighten test-only package completeness checks; no wire or image changes.

Applied between cases. Existing receiver processes keep their loaded code.
"""
from pathlib import Path

p = Path(__file__).resolve().parents[2] / 'run/v8-live/mock_delivery_agent.py'
s = p.read_text()
needle = '        digest = self.digest(manifest, ordered_parts)'
addition = '''        # Test assertion: every ID promised by the observation is present.
        supplied_evidence = [item["metadata"]["evidence_id"] for item in manifest["evidence"]]
        supplied_embeddings = [item["embedding_id"] for item in manifest["embeddings"]]
        if len(supplied_evidence) != len(set(supplied_evidence)) or set(supplied_evidence) != evidence_ids:
            raise ValueError("promised evidence missing or duplicated")
        if len(supplied_embeddings) != len(set(supplied_embeddings)) or set(supplied_embeddings) != embedding_ids:
            raise ValueError("promised embedding missing or duplicated")

        digest = self.digest(manifest, ordered_parts)'''
if addition not in s:
    assert s.count(needle) == 1
    s = s.replace(needle, addition)
    p.write_text(s)
print('Test receiver exact promised-ID sets verified; existing running receiver unchanged')
