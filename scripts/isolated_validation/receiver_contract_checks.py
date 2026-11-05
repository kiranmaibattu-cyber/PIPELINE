"""CPU-only test-receiver checks using a real captured image package."""
from pathlib import Path
import copy, hashlib, importlib.util, json, tempfile

base = Path('/checks')
spec = importlib.util.spec_from_file_location('receiver', base / 'mock_delivery_agent.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
manifest = json.loads((base / 'sample/manifest.json').read_text())
parts = {item['part_name']: (base / 'sample' / (item['part_name'] + '.jpg')).read_bytes()
         for item in manifest['evidence']}
results = []
with tempfile.TemporaryDirectory() as folder:
    folder = Path(folder)
    store = module.Store(Path('/opt/traffic-pilot/contracts/sentry-v8'), folder / 'accepted.jsonl', folder / 'status.json')
    first = store.accept(manifest, parts)
    duplicate = store.accept(manifest, parts)
    results.append({'case': 'valid-package', 'passed': first[0] == 201 and first[1]['status'] == 'durable'})
    results.append({'case': 'identical-retry', 'passed': duplicate[0] == 200 and duplicate[1]['status'] == 'duplicate' and duplicate[1]['sequences'] == first[1]['sequences']})
    def rejected(name, m, p):
        try:
            status, body = store.accept(m, p)
            results.append({'case': name, 'passed': status >= 400, 'status': status})
        except Exception as e:
            results.append({'case': name, 'passed': True, 'reason': type(e).__name__ + ': ' + str(e)[:140]})
    m = copy.deepcopy(manifest); m['evidence'] = []
    rejected('promised-image-missing', m, {})
    m = copy.deepcopy(manifest); m['embeddings'] = []
    rejected('promised-vector-missing', m, parts)
    p = dict(parts); key = next(iter(p)); p[key] = p[key][:-1]
    rejected('truncated-image', manifest, p)
    m = copy.deepcopy(manifest); m['evidence'][0]['metadata']['observation_id'] = '00000000-0000-4000-8000-000000000000'
    rejected('wrong-image-parent', m, parts)
    m = copy.deepcopy(manifest); m['embeddings'][0]['evidence_id'] = '00000000-0000-4000-8000-000000000000'
    rejected('wrong-vector-evidence-link', m, parts)
    p = dict(parts); key = next(iter(p)); p[key] = p[key] + b'changed'
    m = copy.deepcopy(manifest)
    m['evidence'][0]['metadata']['size_bytes'] = len(p[key])
    m['evidence'][0]['metadata']['sha256'] = 'sha256:' + hashlib.sha256(p[key]).hexdigest()
    status, body = store.accept(m, p)
    results.append({'case': 'same-id-different-bytes', 'passed': status == 409 and body['error']['code'] == 'package_id_conflict'})
print(json.dumps({'scope': 'Test receiver conformance using one real CV scene package, CPU only; not production agent durability.',
    'representative_acknowledgements': {'first_acceptance': {'http_status': first[0], 'body': first[1]},
        'identical_retry': {'http_status': duplicate[0], 'body': duplicate[1]}},
    'cases': results, 'passed': all(x['passed'] for x in results)}, indent=2))
