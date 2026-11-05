"""Test-only reconnect-capable senders for process faults; image unchanged."""
from pathlib import Path
p=Path(__file__).resolve().parents[2]/'scripts/v8_capacity_probe.py';s=p.read_text()
needle='''        process = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-re",'''
extra='''        if os.environ.get("SENTRY_TEST_REUSABLE_REPLAY"):
            suite=json.loads(os.environ.get("SENTRY_TEST_RECORDINGS","[]"))
            fixture=suite[index % len(suite)] if suite else str(args.recording)
            process=subprocess.Popen([
                "python3",str(ROOT / "scripts/isolated_validation/reusable_sender.py"),
                "--port",str(port),"--fixture",fixture,
            ],stdout=subprocess.DEVNULL,stderr=log_stream)
            return [process,log_stream]
'''
if 'if os.environ.get("SENTRY_TEST_REUSABLE_REPLAY"):'not in s:
 assert s.count(needle)==1;s=s.replace(needle,extra+needle);p.write_text(s)
print('Reconnect-capable TEST producer branch verified')
