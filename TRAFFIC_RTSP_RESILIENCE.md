# Traffic Pilot v9 RTSP Resilience

Image: `localhost/traffic-pilot-runtime:intel-285h-2026.09.17-v9`

## Changes

- RTSP resolution probing uses TCP transport, a 30-second FFprobe read timeout,
  and three attempts with exponential backoff.
- The FFmpeg decoder uses TCP transport and the supported 30-second RTSP
  `timeout` option. FFprobe separately uses `rw_timeout`.
- A failed camera process is restarted independently with per-camera backoff
  from 5 to 60 seconds. One unavailable camera no longer terminates the fleet
  worker or the solution container.
- Existing Intel placement is unchanged: vehicle and plate detectors use the
  iGPU, and OCR keeps its configured GPU/NPU execution.

## Verification

- Repository tests: `PYTHONPATH=. pytest -q` -> 28 passed.
- In-image RTSP probe against `rtsp://192.168.1.95:8554/traffic1` returned
  `1920x1080` using TCP.
- Podman live smoke test used the same RTSP source. After 20 seconds the
  container was still running, `/healthz` returned `{"ok":true}`, the active
  plan contained one camera, and the decoder logged reconnect attempts instead
  of stopping the container.
- The v9 retry implementation is included in the current PIPELINE v10 runtime
  under `edge_runtime/solution_packs/traffic/runtime_v11/`. The current saved
  delivery is `delivery/apexfabric-v1/intel-285h/traffic-v11/image-2026.09.18-v11.tar`.

The RTSP retry logic cannot make an unreachable camera available; it preserves
the deployment and keeps retrying until the camera returns.
