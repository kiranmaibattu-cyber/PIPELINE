from pathlib import Path
p=Path(__file__).resolve().parents[2]/'scripts/v8_capacity_probe.py';s=p.read_text()
if 'SENTRY_TEST_RECORDINGS' not in s:
 s=s.replace('str(args.recording),','str((json.loads(os.environ["SENTRY_TEST_RECORDINGS"])[index % len(json.loads(os.environ["SENTRY_TEST_RECORDINGS"]))]) if os.environ.get("SENTRY_TEST_RECORDINGS") else args.recording),')
 s=s.replace('    run.append(args.image)','    run.extend(["-v", "/home/admin1/Documents/sentry-validation/fixtures:/fixtures:ro"])\n    run.append(args.image)')
 p.write_text(s)
