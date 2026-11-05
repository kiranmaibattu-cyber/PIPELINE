from pathlib import Path
p=Path(__file__).resolve().parents[2]/'scripts/v8_capacity_probe.py';s=p.read_text()
needle='''        process = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-re",'''
replacement='''        if os.environ.get("SENTRY_TEST_MARKER_REPLAY"):
            process = subprocess.Popen([
                "python3", str(ROOT / "scripts/isolated_validation/marker_sender.py"),
                "--port", str(port), "--fixture", str(args.recording),
                "--logdir", os.environ["SENTRY_TEST_MARKER_REPLAY"],
            ], stdout=subprocess.DEVNULL, stderr=log_stream)
            return [process, log_stream]
        process = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-re",'''
assert needle in s
s=s.replace(needle,replacement);p.write_text(s)
p=Path(__file__).resolve().parents[2]/'run/v8-live/mock_delivery_agent.py';s=p.read_text().replace('self.gallery_counts.get(key,0) < 2','self.gallery_counts.get(key,0) < int(os.environ.get("SENTRY_TEST_GALLERY_LIMIT","2"))');p.write_text(s)
