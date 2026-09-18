# Combined Traffic and Face Runtime v10

The v10 candidate extends the existing Sporada Secure runtime. It preserves
ANPR, vehicle counting, pedestrian counting, fire/smoke detection, RTSP
recovery, persistent snapshots, and desired-state live updates, and adds the
optional `face_recognition` application.

## Edge responsibility

The edge detects and tracks people with the existing detector, detects and
aligns faces on the Intel iGPU, creates 512-dimensional AdaFace vectors on the
Intel NPU, applies quality/cooldown gates, and stores exact-frame evidence.
It does not enroll people, name identities, search a central gallery, cluster
unknown people, or make a recognition decision.

Raw embeddings are written only to the durable face-sample outbox and sent to
the authenticated management endpoint. Normal `/events` messages contain a
`sample_id`, model/embedding-space identifiers, quality, bounding box, and
snapshot references, but never the vector.

## Mounts

- `/configs/desired_state.json` (read-only): camera and app assignments.
- `/configs/face-management.json` (read-only, optional): HTTPS management
  receiver settings. Without it, samples remain queued locally.
- `/run/secrets/apexfabric/*.rtsp` (read-only): camera source secrets.
- `/run/secrets/apexfabric/management-token` (read-only): bearer token.
- `/run/secrets/apexfabric/management-ca.pem` (read-only, private CA only).
- `/state` (read-write persistent volume): events, snapshots, face evidence,
  and the durable face-sample outbox.
- `/dev/dri` and `/dev/accel`: Intel iGPU and NPU devices.

## Management receiver

For each accepted sample, the uploader first sends both JPEG artifacts:

`PUT /v1/edges/{edge_id}/artifacts/{sha256}`

The receiver must return `{"sha256":"..."}` only after the bytes are durable.
It then sends the payload validated by `face-sample.schema.json`:

`POST /v1/edges/{edge_id}/face-samples`

The receiver must return `{"sample_id":"face-..."}`. Until both responses are
acknowledged, the record remains under `/state/face_samples/outbox/<camera>/`
and is retried after network or management outages.

Management owns enrollment, identity matching, search, person history,
retention, deletion, encryption at rest, authorization, and audit logging.

## Hardware placement

- Decode: VAAPI iGPU.
- Vehicle/person and plate detection: iGPU.
- OCR: existing GPU/NPU placement.
- Smoke/fire: existing GPU placement.
- SCRFD face detection: iGPU.
- AdaFace embedding: NPU.

Model identity is fixed as `adaface-ir101-int8-v1` with embedding space
`adaface-ir101-int8-v1:512:bgr-aligned-112`. Management must never compare
vectors from incompatible embedding spaces.

## Verified candidate

- Base image ID: `1a7a4208cefe3567d03e9d2222a7711530b359861a579d936fb079af50f76c6d`.
- Workload image ID: `504d5c7a20e2f27688cb1d42b0c6afca91c7a5686d2f2428b5dd2766ee018234`.
- Workload digest: `sha256:79b34fd10c2a679cf9e3761081f1f65348b652df31aadb0c08af6e0bd0c0f0ea`.
- Saved archive SHA-256: `17ba9c29c0afe7fa67af1da2acc8ae98bb77bcc461c32b0ad1bb8674c7ea1e54`.
- Authoritative source and archive location: `/home/admin1/Documents/PIPELINE`.
- Automated tests: 33 passed.
- Live `traffic1` run: 450 vehicle count events, 450 pedestrian count
  events, 7 plate events, and 34 face events passed the public event schema.
- The 34 corresponding face samples contained 512-dimensional vectors and
  68 checksum-linked JPEG artifacts. No analytics face event contained a raw
  vector.
- A live revision 1 to revision 2 app change kept the same container PID,
  applied the new graph, and left the replacement worker healthy.
- The test container was stopped and removed after verification.
