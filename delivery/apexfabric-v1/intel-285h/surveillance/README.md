# Surveillance Edge Runtime - ApexFabric V1 Intel Delivery

This package targets `linux/amd64` on Intel Core Ultra 9 285H. Jetson Orin is
not supported by this delivery.

This delivery follows the ApexFabric V1 image, configuration, model, Secret,
and HTTP contracts. It declares one required extension: a persistent `/state`
mount. The base V1 draft says persistent volumes are not used, but durable face
enrollment, ReID rejoin state, history, and evidence cannot satisfy the
surveillance requirements without one.

## Image

Registry delivery: `ghcr.io/kiranmaibattu-cyber/surveillance-edge-runtime:intel-285h-2026.09.10-v5`.
See the root `SURVEILLANCE_V5_DEPLOYMENT.md` for its immutable digest, mounts and APIs.
`../RELEASE-2026.09.10-v2.md` describes the previous release and its archives.

```text
surveillance-edge-runtime:intel-285h-2026.09.10-v5
```

The image runs as UID/GID `10001`, listens on `0.0.0.0:8080`, contains its
models, and has no management-server dependency. Mount persistent storage at
`/state` for event, gallery, history, crop, and snapshot state.

## Layered Runtime Base

The workload is built from this immutable Intel surveillance runtime base:

```text
apexfabric-intel-surveillance-runtime-base:intel-285h-2026.08.24-v1
sha256:ff3bc61535f4b4289298db44915ba18e2412c2bfa2c66065597dd1af3ea96f5d
```

The base contains Ubuntu, Intel GPU/NPU userspace, VAAPI/FFmpeg, Python,
OpenVINO 2024.6, and the pinned surveillance dependency set. It is a build
parent, not a second workload container. Build the base and current workload with:

```bash
CONTAINER_ENGINE=podman ./scripts/build_surveillance_layered_image.sh
```

The base is approximately 2.444 GB. The final workload adds approximately
280 MB, almost entirely the baked surveillance model set.

## Desired-State Hot Reload

The container watches `/configs/desired_state.json` every two seconds.
Management must increment `revision` when cameras, applications, FPS, zones, or
counting lines change. A valid candidate graph replaces only the internal 8090
worker; the container, public API, persistent gallery, events, and snapshots
remain available. Invalid or older revisions leave the active graph running.

Mount `/configs` as a directory. Kubernetes `subPath` ConfigMap mounts do not
receive atomic ConfigMap updates and therefore are not supported for hot reload.

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

Prepare `configs/desired_state.json` from `desired-state.example.json` and
`secrets/cam-surveillance-01.rtsp` containing the test RTSP URL. Ensure the state
directory is writable by UID/GID 10001, then:

```bash
docker run --rm -p 8080:8080 \
  --device /dev/dri:/dev/dri --device /dev/accel:/dev/accel \
  -v "$PWD/configs:/configs:ro" \
  -v "$PWD/secrets:/run/secrets/apexfabric:ro" \
  -v "$PWD/state:/state" \
  ghcr.io/kiranmaibattu-cyber/surveillance-edge-runtime:intel-285h-2026.09.10-v5
```

The compiler command required by the contract is available in the same image.
`GET /metrics` returns documented JSON, and `GET /events` returns normalized
SSE analytics plus a five-second idle heartbeat. Runtime state and optional
alert snapshots are persisted under `/state/surveillance`. Management can use
`GET /api/face_gallery?detail=1`, `POST /api/enrollment/start`, enrollment
`save`/`cancel`/`retake`, `POST /api/face_group`, and
`DELETE /api/face_gallery/<urlencoded-name>` through the public port. Face
templates live under `/state/surveillance/face_gallery`; persistent ReID state
lives under `/state/surveillance/reid_gallery`.

These legacy mutation endpoints apply only when management sync is disabled.
With sync enabled, use authenticated `/api/management/commands` for enrollment,
gallery replacement and status, as described in `SURVEILLANCE_V5_DEPLOYMENT.md`.

For `people_counting`, omit `config.lines.people_counting` to receive current
occupancy events. Supply one or more normalized counting lines to receive `in`
and `out` crossing events. Intrusion requires at least one normalized polygon in
`config.zones.intrusion`. The image remains approximately 2.72 GB, but routine
releases reuse the stable 2.444 GB base layers.

## Management Outputs

Release `2026.09.10-v5` adds saved enrollment chip uploads
and explicit chip-to-template links. It is now published to GHCR.
Management still approves candidates through `gallery.replace`; enrollment save
does not activate recognition. Live previews are not part of candidate uploads.

An opt-in management synchronization implementation is included in v5,
including the polygon counting addition. It uses a
mounted `management-sync.example.json` and Secrets, while desired-state JSON
continues to select cameras and apps. See the root `MANAGEMENT_IDENTITY_SYNC.md`
for command/record schemas, receiver APIs, staged enrollment, versioned galleries,
durable uploads, and remaining production limitations. The v2 tag remains unchanged.

### Polygon Counting (Source Update)

The ROI counting addition is built and live-tested locally in
`surveillance-edge-runtime:intel-285h-2026.09.10-v3`; it is not in the published
`2026.09.10-v2` image. See `desired-state.roi-counting.example.json`.
Set `config.zones.people_counting` to polygons with unique names and normalized
coordinates. Each ROI emits `people_count_event` with
`payload: {"mode": "roi_occupancy", "zone": "lobby", "count": 3}` when its
tracked occupancy changes. A person is inside when their bounding-box
bottom-centre is inside the polygon. Overlapping ROIs count independently.
An observed exit removes the person immediately; missing tracks expire after
two seconds. Zero is emitted after an ROI empties, and the initial count is
emitted after the camera first supplies a tracked observation.

With both ROIs and lines, polygon occupancy and line-crossing events coexist.
With neither, full-frame occupancy remains unchanged. Intrusion polygons do
not restrict counting. Snapshot evidence uses the latest contributing source
observation; timeout-only changes have no snapshot, rather than stale evidence.
Occupancy is track-based across the short retention window, not an instantaneous
per-frame detector total. Change the desired-state revision to apply new ROIs.

| Application | Event type | Main payload fields |
|---|---|---|
| ReID | `identity_event` | `global_id`, `person_ref`, distance/new/cross-camera status |
| ReID | `cross_camera_identity_event` | survivor `global_id`, dropped ID, reason |
| Face recognition | `face_recognized_event` | `employee_id`, `dist`, group, `global_id`, `person_ref` |
| Face enrollment | `face_enrolled_event` | name, vector count, pose coverage |
| Face recognition | `unauthorised_event` | employee/group/distance and correlated evidence |
| Intrusion | `intrusion_event` | zone, local track (`who`), `global_id`, `person_ref`, evidence |
| People counting | `people_count_event` | occupancy `count`, or line `direction` and `tally` |

Evidence references point into the mounted `/state/surveillance` volume and
are also retrievable from the container API. A tracked-person alert can include
`snapshot_assets.frame` and `snapshot_assets.person_crop`; each asset contains
its `/snapshots/...` URL and media type after SSE normalization.

The metrics payload is defined by `metrics.schema.json`; analytics events are
defined by `analytics-event.schema.json`.

## Local Offline Archive Parts

The complete local `image-2026.09.10-v2.tar` is approximately `2.71 GB`, above GitHub
Free/Pro's per-file
Git LFS limit. The exact current image is saved locally as these versioned parts,
but the new binaries are excluded from Git. Pull this release through GHCR or
transfer the local archives separately:

```text
image-2026.09.10-v2.tar.part-aa
image-2026.09.10-v2.tar.part-ab
image-2026.09.10-v2.parts.sha256
```

Reconstruct and verify it inside this directory:

```bash
sha256sum -c image-2026.09.10-v2.parts.sha256
cat image-2026.09.10-v2.tar.part-* > image-2026.09.10-v2.tar
sha256sum -c image-2026.09.10-v2.sha256
docker load -i image-2026.09.10-v2.tar
```

Tracked observations propagate exact frame evidence to the event callback.
`payload.evidence` identifies the source camera, stream session, frame number,
capture time (Unix seconds), dimensions, and source JPEG checksum.
`payload.evidence_status` reports whether a snapshot was attached. Expired
evidence is not replaced with an unrelated latest frame. See the root
`SURVEILLANCE_FRAME_EVIDENCE.md` for cache limits and aggregate-count semantics.
