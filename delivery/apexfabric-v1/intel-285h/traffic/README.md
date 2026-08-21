# Traffic Edge Runtime - ApexFabric V1 Intel Delivery

This package targets `linux/amd64` on Intel Core Ultra 9 285H. Jetson Orin is
not supported by this delivery.

## Image

```text
traffic-edge-runtime:intel-285h-2026.08.21-v4
```

The image runs as UID/GID `10001`, listens on `0.0.0.0:8080`, contains its
models, and has no Redis or management-server dependency. Mount persistent
storage at `/state` so event JSONL files and alert snapshots survive Pod
replacement and can be served through `/events` and `/snapshots/...`.

## Baked Models

| Path under `/models/traffic` | Version | SHA-256 | Device |
|---|---|---|---|
| `openvino/vehicle.xml` | `vehicle_openvino:v1` | `ba402c4eabe93c1c4ad9b7ea70b95fbb0a0d374b7ec777dc26992f428778f5b9` | GPU |
| `openvino/vehicle.bin` | `vehicle_openvino:v1` | `e8513382b2cca7cc4fb9257a19b0f28156537a1b2e15074fe47fabc64ededf26` | GPU |
| `openvino/license_plate.xml` | `license_plate_openvino:v1` | `d9ff01e97241d2d0132b960d6bd6c12659a09474b3a005991aa633adba0c94aa` | NPU |
| `openvino/license_plate.bin` | `license_plate_openvino:v1` | `df60a0a27cd91e5cbfe0305f5bce1f8a588acd0b031c3fa2bd4a0c379cc23dda` | NPU |
| `openvino/ocr.xml` | `ocr_openvino:v1` | `98cdc203e544f3908e8b47da85cb98e771d18de16e877781f7db76dd8feb63ed` | GPU/NPU |
| `openvino/ocr.bin` | `ocr_openvino:v1` | `96147eba58867c42ba6040f9446a8936d4eecfed14e3e7c2ab43db6bc3f49dff` | GPU/NPU |

Runtime compatibility: OpenVINO 2026.2, Intel GPU Level Zero/OpenCL, and Intel
NPU Level Zero userspace.

## Camera Geometry

The desired-state `config` object accepts normalized camera geometry. Points use
`[x, y]` values from `0.0` to `1.0`.

`wrong_way` requires `config.lines.wrong_way`, and `illegal_parking` requires
`config.zones.illegal_parking`. `vehicle_counting` and `pedestrian_counting`
accept optional lines. When a line is omitted, each stable unique track is
counted once. When management supplies a line, the runtime emits directional
line-crossing counts. Normalized geometry is scaled to the decoded frame size.
`anpr` accepts an optional `config.zones.anpr` plate ROI.
The runtime emits events only for apps listed on each camera; selecting ANPR
and vehicle counting does not implicitly enable pedestrian counting.

## Acceptance

Create `secrets/cam-traffic-01.rtsp` containing a fake/test RTSP URL, then:

```bash
docker run --rm -p 8080:8080 \
  --device /dev/dri:/dev/dri --device /dev/accel:/dev/accel \
  -v "$PWD/desired-state.example.json:/configs/desired_state.json:ro" \
  -v "$PWD/secrets:/run/secrets/apexfabric:ro" \
  -v "$PWD/state:/state" \
  traffic-edge-runtime:intel-285h-2026.08.21-v4
```

The compiler command required by the contract is available in the same image.
`GET /metrics` returns documented JSON, and `GET /events` returns normalized
SSE analytics plus a five-second idle heartbeat. Runtime state and optional
alert snapshots are persisted under `/state/traffic`. Traffic alert snapshots
include event frames and, when bboxes are available, object crops. ANPR events
save a license-plate crop and the parent vehicle crop. Vehicle events expose a
`vehicle_ref` built from camera, runtime session, and vehicle track ID, which
management uses to join ANPR, wrong-way, and illegal-parking events. Wrong-way
events save the vehicle crop and include plate text and a plate crop whenever
OCR evidence is available for that track. The separate plate crop is a zoomed
view in the same vehicle evidence bundle. The estimated image size is 1.93 GB
before `docker save` archive overhead.

The metrics payload is defined by `metrics.schema.json`; analytics events are
defined by `analytics-event.schema.json` and demonstrated by
`analytics-event.example.json`.

The local/Git LFS delivery archive is `image-2026.08.21-v4.tar`. Verify and
load it from this directory with:

```bash
sha256sum -c image-2026.08.21-v4.sha256
docker load -i image-2026.08.21-v4.tar
```
