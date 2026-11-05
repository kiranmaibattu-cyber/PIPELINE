"""Finite actual-image TLS regression matrix using ephemeral test certificates."""
from contextlib import contextmanager
import argparse
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time


def openssl(*args):
    subprocess.run(["openssl", *map(str, args)], check=True, capture_output=True)


@contextmanager
def server(certificate, key, response=404, encrypted=True):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    sock.settimeout(.2)
    stop, events = threading.Event(), []

    def handle(connection):
        try:
            with (context.wrap_socket(connection, server_side=True) if encrypted else connection) as stream:
                events.append("tls_accepted" if encrypted else "tcp_accepted")
                stream.settimeout(2)
                while True:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        part = stream.recv(4096)
                        if not part:
                            return
                        data += part
                    lines = data.decode(errors="replace").splitlines()
                    events.append(lines[0].split(" ", 1)[0])
                    sequence = next((line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("cseq:")), "1")
                    method = lines[0].split(" ", 1)[0]
                    status = (
                        "200 OK" if response == 404 and method == "OPTIONS"
                        else "404 Not Found" if response == 404
                        else "401 Unauthorized"
                    )
                    stream.sendall((f"RTSP/1.0 {status}\r\nCSeq: {sequence}\r\nPublic: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN\r\nContent-Length: 0\r\n\r\n").encode())
        except (OSError, ssl.SSLError):
            pass

    def accept():
        while not stop.is_set():
            try:
                connection, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=handle, args=(connection,), daemon=True).start()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield f"{'rtsps' if encrypted else 'rtsp'}://127.0.0.1:{sock.getsockname()[1]}/test", events
    finally:
        stop.set()
        sock.close()
        thread.join(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    probe = Path(__file__).with_name("camera_tls_probe.py").resolve()
    records = []
    with tempfile.TemporaryDirectory(prefix="sentry-tls-qualification-") as temporary:
        root = Path(temporary)
        ca, ca_key = root / "ca.crt", root / "ca.key"
        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=Ephemeral Camera Test CA", "-addext", "basicConstraints=critical,CA:TRUE", "-keyout", ca_key, "-out", ca)
        certs = {}
        for name, identity, days in [("valid", "IP:127.0.0.1", 1), ("bad-hostname", "DNS:wrong.invalid", 1), ("expired", "IP:127.0.0.1", 0)]:
            key, csr, cert, extension = [root / (name + suffix) for suffix in [".key", ".csr", ".crt", ".ext"]]
            openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=Camera Test", "-keyout", key, "-out", csr)
            extension.write_text(f"subjectAltName={identity}\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n")
            openssl("x509", "-req", "-in", csr, "-CA", ca, "-CAkey", ca_key, "-CAcreateserial", "-days", days, "-extfile", extension, "-out", cert)
            certs[name] = cert, key
        trust = root / "trust"
        trust.mkdir(mode=0o755)
        shutil.copy2(ca, trust / "office-ca.crt")
        invalid = root / "invalid"
        invalid.mkdir(mode=0o755)
        (invalid / "invalid.crt").write_text("not PEM")
        time.sleep(1.1)  # The zero-day leaf must be expired, not merely expiring now.

        def run(name, leaf="valid", mounted=None, mode="single", explicit_file=False, response=404, encrypted=True, public_url=None, tls_mode="strict"):
            with server(*certs[leaf], response=response, encrypted=encrypted) as (url, events):
                command = ["podman", "run", "--rm", "--network=host", "--cpus=1", "--pids-limit=128", "--entrypoint", "python3", "-v", f"{probe}:/tmp/camera-tls-probe.py:ro"]
                if tls_mode is not None:
                    command += ["-e", f"SENTINEL_CAMERA_TLS_MODE={tls_mode}"]
                if mounted:
                    command += ["-v", f"{mounted}:/run/secrets/sentinel/camera-ca:ro"]
                if explicit_file:
                    command += ["-e", "SENTINEL_CAMERA_CA_FILE=/run/secrets/sentinel/camera-ca/office-ca.crt"]
                command += [args.image, "/tmp/camera-tls-probe.py", "--url", public_url or url, "--mode", mode]
                result = subprocess.run(command, text=True, capture_output=True, timeout=30)
                (args.output / f"{name}.stderr.log").write_text(result.stderr)
                data = json.loads(result.stdout.splitlines()[-1]) if result.stdout else {}
                assert result.returncode == 0, (name, result.stderr)
                if name in {"untrusted", "shared-untrusted"}:
                    passed = data.get("certificate_error_flags", 0) & 1 and data.get("reason") == "tls_unknown_ca"
                elif name == "bad-hostname":
                    passed = data.get("certificate_error_flags", 0) & 2
                elif name == "expired":
                    passed = data.get("certificate_error_flags", 0) & 8
                elif name == "bad-leader":
                    passed = data.get("bad_leader_configuration_rejected") and data.get("peer_progress_after_failure", 0) > 0
                elif name == "invalid-pem":
                    passed = data.get("reason") == "tls_configuration_error" and "configuration_error" in data
                elif name == "unauthorized":
                    passed = data.get("reason") == "source_authentication_failed" and "OPTIONS" in events
                elif public_url:
                    passed = data.get("rtp_received") and not data.get("certificate_error_flags")
                else:
                    passed = not data.get("certificate_error_flags") and "OPTIONS" in events and "DESCRIBE" in events
                if mode == "shared":
                    passed = passed and data.get("peer_state_playing") and data.get("peer_progress_after_failure", 0) > 0
                if "validation_flags" in data:
                    expected_flags = 127 if tls_mode == "strict" else 0
                    passed = passed and data["validation_flags"] == expected_flags and data["configured_before_source_started"]
                record = {"case": name, "passed": bool(passed), "result": data, "server_events": list(events)}
                records.append(record)
                (args.output / "results.json").write_text(json.dumps({"image": args.image, "scope": "Native TLS/RTSP connection only; synthetic server returns404 deliberately, no media accuracy/capacity claim", "cases": records}, indent=2) + "\n")
                print(json.dumps({"case": name, "passed": bool(passed), "reason": data.get("reason"), "certificate_flags": data.get("certificate_error_flags")}), flush=True)
                assert passed, record

        run("legacy-default", tls_mode=None)
        run("legacy-bad-hostname", leaf="bad-hostname", tls_mode=None)
        run("legacy-expired", leaf="expired", tls_mode=None)
        run("legacy-shared", tls_mode=None, mode="shared")
        run("untrusted")
        run("mounted-ca", mounted=trust)
        run("explicit-ca-file", mounted=trust, explicit_file=True)
        run("bad-hostname", leaf="bad-hostname", mounted=trust)
        run("expired", leaf="expired", mounted=trust)
        run("invalid-pem", mounted=invalid)
        run("shared-trusted", mounted=trust, mode="shared")
        run("shared-untrusted", mode="shared")
        run("bad-leader", mounted=invalid, mode="bad-leader")
        run("unauthorized", response=401, encrypted=False)
        run("public-with-extra-ca", mounted=trust, public_url="rtsps://gpu-box.burmilla-bass.ts.net:443/traffic1")
    print(json.dumps({"passed": len(records), "failed": 0, "ephemeral_private_keys_deleted": True}))


if __name__ == "__main__":
    main()
