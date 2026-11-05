"""Post-run source-only compressed-video arrival rate; never decodes frames."""
import argparse
import json
from pathlib import Path
import signal
import subprocess
import threading
import time

p = argparse.ArgumentParser()
p.add_argument('--sources-json', required=True)
p.add_argument('--output', required=True)
args = p.parse_args()
results = []
for url in json.loads(args.sources_json):
    samples = []
    start = time.monotonic()
    process = subprocess.Popen(['stdbuf', '-oL', 'ffprobe', '-v', 'error',
        '-rtsp_transport', 'tcp', '-rw_timeout', '8000000', '-select_streams', 'v:0',
        '-show_packets', '-show_entries', 'packet=pts_time,dts_time,size',
        '-of', 'csv=p=0', url], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)
    def read():
        for line in process.stdout:
            if line.strip():
                samples.append({'monotonic': time.monotonic(), 'packet': line.strip()})
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = start + 22
    while process.poll() is None and time.monotonic() < deadline:
        threading.Event().wait(.2)
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait()
    reader.join(timeout=3)
    error = process.stderr.read()
    steady = [x for x in samples if x['monotonic'] - start >= 5]
    fps = ((len(steady) - 1) / (steady[-1]['monotonic'] - steady[0]['monotonic'])
           if len(steady) > 1 else None)
    results.append({'source': url, 'compressed_video_packets_per_second': fps,
        'packets': len(samples), 'steady_packets': len(steady), 'samples': samples,
        'returncode_after_intentional_stop': process.returncode, 'stderr': error[-1000:],
        'scope': 'Later source-only compressed video packet arrival; no decoding, not simultaneous CV/source attribution.'})
Path(args.output).write_text(json.dumps(results, indent=2))
print(json.dumps([(x['source'], x['compressed_video_packets_per_second']) for x in results]))
