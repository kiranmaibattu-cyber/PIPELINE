"""Test the whole image through an untrusted TLS relay of an existing camera.

Only this test listener uses an ephemeral certificate. The upstream RTSPS
connection uses normal certificate validation. Private keys are never saved
in reports. No image/host/deployment settings are changed by the test.
"""
import argparse
import json
from pathlib import Path
import select
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    support = args.output / "test-support"
    support.mkdir(exist_ok=True)
    original = repo / "model_updates/2026-11-05-qaft-int8/validation/test-support"
    for name in ["capacity_probe.py", "receiver.py"]:
        shutil.copy2(original / name, support / name)
    threads, connections = [], []
    stop = threading.Event()
    counters = {"tls_connections": 0, "upstream_bytes": 0, "downstream_bytes": 0}
    errors = []
    with tempfile.TemporaryDirectory(prefix="sentry-live-tls-") as temporary:
        root = Path(temporary)
        cert, key = root / "server.crt", root / "server.key"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", "1", "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert),
        ], check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        upstream_context = ssl.create_default_context()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        listener.settimeout(.2)
        url = f"rtsps://127.0.0.1:{listener.getsockname()[1]}/traffic1"

        def forward(connection):
            try:
                with context.wrap_socket(connection, server_side=True) as client:
                    counters["tls_connections"] += 1
                    with socket.create_connection(("gpu-box.burmilla-bass.ts.net", 443), timeout=8) as plain:
                        with upstream_context.wrap_socket(plain, server_hostname="gpu-box.burmilla-bass.ts.net") as upstream:
                            connections.extend([client, upstream])
                            client.settimeout(3)
                            upstream.settimeout(3)
                            while not stop.is_set():
                                readable = [s for s in [client, upstream] if s.pending()]
                                if not readable:
                                    readable, _, _ = select.select([client, upstream], [], [], .2)
                                for source in readable:
                                    data = source.recv(65536)
                                    if not data:
                                        return
                                    target = upstream if source is client else client
                                    target.sendall(data)
                                    counters["upstream_bytes" if source is client else "downstream_bytes"] += len(data)
            except (OSError, ssl.SSLError) as exc:
                errors.append(type(exc).__name__)

        def accept():
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                thread = threading.Thread(target=forward, args=(connection,), daemon=True)
                threads.append(thread)
                thread.start()

        accept_thread = threading.Thread(target=accept, daemon=True)
        accept_thread.start()
        try:
            probe = Path(__file__).with_name("camera_tls_probe.py").resolve()
            base = ["podman", "run", "--rm", "--network=host", "--cpus=1", "--entrypoint", "python3", "-v", f"{probe}:/tmp/probe.py:ro"]
            for mode in ["strict", "legacy-compatible"]:
                command = base + ["-e", f"SENTINEL_CAMERA_TLS_MODE={mode}", args.image, "/tmp/probe.py", "--url", url]
                result = subprocess.run(command, capture_output=True, text=True, timeout=20)
                (args.output / f"relay-{mode}.stderr.log").write_text(result.stderr)
                data = json.loads(result.stdout.splitlines()[-1])
                (args.output / f"relay-{mode}.json").write_text(json.dumps(data, indent=2) + "\n")
                assert result.returncode == 0
                if mode == "strict":
                    assert data["certificate_error_flags"] & 1 and not data["rtp_received"]
                else:
                    assert data["rtp_received"] and data["validation_flags"] == 0
            command = [
                "python3", str(Path(__file__).with_name("opt80_compare.py")),
                "--root", str(args.output), "--case", "native-selfsigned-1x4",
                "--image", args.image, "--source-url", url, "--unmarked",
                "--cameras", "1", "--fps", "4", "--seconds", "30",
                "--startup-cpus", "2",
            ]
            (args.output / "native-command.json").write_text(json.dumps(command, indent=2) + "\n")
            with (args.output / "native-live.log").open("w") as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=300)
            counters["native_driver_returncode"] = result.returncode
        finally:
            stop.set()
            listener.close()
            for connection in connections:
                try:
                    connection.close()
                except OSError:
                    pass
            accept_thread.join(1)
            for thread in threads:
                thread.join(1)
            (args.output / "relay-summary.json").write_text(json.dumps({
                "scope": "Actual source behind ephemeral untrusted TLS relay; upstream TLS verified",
                "counters": counters, "connection_close_types": errors,
                "private_keys_retained": False,
            }, indent=2) + "\n")


if __name__ == "__main__":
    main()
