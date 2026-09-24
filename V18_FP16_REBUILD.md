# v18 FP16 Vehicle Model Rebuild

## Image

The local v18 tag was rebuilt from the unchanged v18 runtime with only the
vehicle/person detector replaced by the compatible YOLO26n FP16 OpenVINO IR.

- Image: `localhost/sporada:intel-285h-2026.09.23-v18`
- Image ID: `0b3f1ab601a71e0296ed91956e7428377a90de22c510c01bd5d9902512dbafed`
- Local digest: `sha256:102b3d6100d11fd345608cd720ccc3ecc806d7168713c28c51d0c824f51f28b8`
- Size: 2,005,415,678 bytes
- Vehicle XML SHA-256: `8c06bc73d0e7538a0a68d18e36d948bd733bf771c48b0e5b53f1bfc05af50a66`
- Vehicle BIN SHA-256: `88f840b4592c13af3146f7924276e416565e2590260b7e89257ad882708ea588`

The previous local INT8 image is preserved as:

`localhost/sporada:intel-285h-2026.09.23-v18-int8-backup`

The tested image is published as:

`ghcr.io/kiranmaibattu-cyber/sporada@sha256:102b3d6100d11fd345608cd720ccc3ecc806d7168713c28c51d0c824f51f28b8`

## Verification

- 83 isolated v18 source tests passed.
- The baked model checksums matched the FP16 source files and OCI labels.
- A recursive checksum comparison against the preserved INT8 v18 found exactly
  two changed filesystem files: `/models/traffic/openvino/vehicle.xml` and
  `/models/traffic/openvino/vehicle.bin`. All other `/opt` and `/models` files
  were byte-identical.
- Direct OpenVINO inference completed on `GPU.0` with input
  `[1,3,640,640]`, output `[1,300,6]`, and finite results.
- A 75-second live `traffic1` run against this exact final image produced 721
  schema-valid SSE events: 359 vehicle-counting, 359 pedestrian-counting, one
  ANPR and two fire/smoke events.
- Snapshot URLs for all four applications returned JPEG bytes. The ANPR event
  retained v18 behavior and exposed an event frame, not separate plate and
  vehicle crops.
- A live face-delivery run produced a 512-dimensional embedding and a 166x166
  face crop. The embedding was delivered only through the internal face-sample
  path and was absent from SSE. An interrupted upload resumed after restart
  with the same event/sample identity.
- A live entry/exit run delivered two acknowledged multipart crossing records,
  one `in` and one `out`, with evidence. Their outbox drained and no crossing
  event was duplicated onto SSE.
- A desired-state revision changed counting from whole-frame mode to explicit
  vehicle and pedestrian polygons. Revision 2 became active without restarting
  either the container or the OpenVINO camera worker.
- No `CL_OUT_OF_RESOURCES`, worker exit, or camera-process restart occurred.

These results verify event generation and delivery paths on the tested streams.
They do not establish ANPR text accuracy, fire/smoke classification accuracy,
face-recognition accuracy, or the maximum multi-camera capacity.

Evidence:

- `validation/v18-fp16/exact-overlay/traffic-live-report.json`
- `validation/v18-fp16/exact-overlay/vehicle-detections.jpg`
- `validation/v18-fp16/exact-overlay/face-live-report.json`
- `validation/v18-fp16/exact-overlay/crossing-live-report.json`
- `validation/v18-fp16/exact-overlay/hot-reload-live-report.json`

This validates the tested single-camera workload. It does not establish the
maximum multi-camera capacity of the image.
