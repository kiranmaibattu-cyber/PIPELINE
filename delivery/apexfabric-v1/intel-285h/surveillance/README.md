# Surveillance Edge Runtime - ApexFabric V1 Intel Delivery

This package targets `linux/amd64` on Intel Core Ultra 9 285H. Jetson Orin is
not supported by this delivery.

## Image

```text
surveillance-edge-runtime:intel-285h-2026.08.20
```

The image runs as UID/GID `10001`, listens on `0.0.0.0:8080`, contains its
models, and has no management-server or persistent-volume dependency.

## Baked Models

| Path under `/models/surveillance` | Version | SHA-256 | Device |
|---|---|---|---|
| `yolo11s_int8.xml` | `yolo11s_int8:v1` | `144dd32b47465ec39d7f0cd6df954775908b740b50fab486ec7f3728a12fd1c0` | GPU |
| `yolo11s_int8.bin` | `yolo11s_int8:v1` | `4b961452248adf8c5461ea1729a6ccd2468be9aca97ace866565a0f4ea4549c0` | GPU |
| `transreid_ssl_int8.xml` | `transreid_ssl_int8:v1` | `63384268f74a2882cfe1201740680ed692bf39336207c844eb6fe4578354ad2b` | NPU |
| `transreid_ssl_int8.bin` | `transreid_ssl_int8:v1` | `365b5f0cf216cb2a5c80f3901c27528489c6e9b1bb91ab7b20a525a6c76a224e` | NPU |
| `adaface_ir101_int8.xml` | `adaface_ir101_int8:v1` | `68db3a49803714ec6796f0b9f60a24fa29be0a9d9efa51a76f2dd9bc7e3f959d` | GPU |
| `adaface_ir101_int8.bin` | `adaface_ir101_int8:v1` | `2e294f66be557edbc14de38f79b8f42c4e52b87bec87e360a3f2fbc7f7242383` | GPU |
| `gaitbase_int8.xml` | `gaitbase_int8:v1` | `1e7c9fe736a3cba8557c1c2492b0e330900c9dd4153587550f1437881618c32e` | NPU |
| `gaitbase_int8.bin` | `gaitbase_int8:v1` | `e5ef031524b80c00f990bc3e356d25f2b9c0ddce59b4177bb49b438a7ddceba8` | NPU |
| `yolov8n_seg_int8.xml` | `yolov8n_seg_int8:v1` | `7b2062a77597c43354e735bce706b3651a21fe489cfd8b346b0cf5f175059f38` | GPU |
| `yolov8n_seg_int8.bin` | `yolov8n_seg_int8:v1` | `61c058136ef150c3bdd6b43196df0c3066830e4162a2caef79b92b897f3c21e1` | GPU |

The five Buffalo-S ONNX assets and their digests are recorded in the baked
`/opt/pipeline/edge_runtime/model_registry/models.yaml`. Runtime compatibility:
OpenVINO 2024.6, Intel GPU Level Zero/OpenCL, and Intel NPU Level Zero userspace.

## Acceptance

Create `secrets/cam-surveillance-01.rtsp` containing a fake/test RTSP URL, then:

```bash
docker run --rm -p 8080:8080 \
  --device /dev/dri:/dev/dri --device /dev/accel:/dev/accel \
  -v "$PWD/desired-state.example.json:/configs/desired_state.json:ro" \
  -v "$PWD/secrets:/run/secrets/apexfabric:ro" \
  surveillance-edge-runtime:intel-285h-2026.08.20
```

The compiler command required by the contract is available in the same image.
`GET /metrics` returns documented JSON, and `GET /events` returns normalized
SSE analytics plus a five-second idle heartbeat. Runtime state and optional
alert snapshots are ephemeral under `/tmp/apexfabric`; no copied 8090
administrative endpoint is exposed. The estimated image size is 2.72 GB before
`docker save` archive overhead.

The metrics payload is defined by `metrics.schema.json`; analytics events are
defined by `analytics-event.schema.json`.

## GitHub Archive Parts

The complete local `image.tar` is `2.72 GB`, above GitHub Free/Pro's per-file
Git LFS limit. The repository therefore carries the exact archive as these
current-build parts:

```text
image.tar.part-aa
image.tar.part-ab
image.parts.sha256
```

Reconstruct and verify it inside this directory:

```bash
sha256sum -c image.parts.sha256
cat image.tar.part-* > image.tar
sha256sum -c image.sha256
docker load -i image.tar
```
