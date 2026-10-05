# Sentinel V5.2 Development Image

Local image: `localhost/sentinel-cv-runtime:intel-285h-2026.10.05-v5.2-dev4`

GHCR image: `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.10.05-v5.2-dev4`

Registry digest: `sha256:54cd1b5c3af020749d819daa918fcae528387c788a3d934fca1681a536801e1d`

Build: `bash scripts/build_sentinel_v5_2_image.sh`

This is a separate runtime from V4. It accepts the V5.2 desired-state bindings and reusable geometry, validates the input schema and references, and sends `/api/v5` Event -> Evidence -> Embedding through a durable outbox. The V4 image and contract files remain unchanged. The image includes YOLO26 FP16 detection, plate detector/OCR, SCRFD/AdaFace, TransReID, MOG2/GaitBase, fire/smoke, and SigLIP2. Only configured profiles are initialized. Recheck, action recognition, and policy clips remain deferred by the contract.

## Verified

- V5.2 package validator passes: 5 schemas and 16 representative Event examples.
- V5.2 runtime tests pass (23): desired-state rejection, count/presence/zone episodes, boundary jitter, bounded candidate confirmation, loss/recovery, ANPR read/correction/outcome, face best-candidate selection and cooldown, independent face/body samples, line crossing, timed hazard clear and score gate, scene pressure, fail-closed accelerator/decode configuration, and ordered upload.
- V5.2 inference requests only GPU/NPU. The worker rejects CPU/AUTO device configuration; fire/smoke and OCR no longer select CPU if requested accelerators are absent. VA-API decode is mandatory, and `OV_DECODE_HW=0` raises an error in the packaged image. CPU preprocessing, color conversion, tracking, and serialization still occur.
- Presence candidates require three observations within a bounded gap. Zone transitions require two clear observations beyond a normalized boundary margin. Fire/smoke requires three qualifying evaluated frames within two seconds and ten seconds of evaluated negatives to clear. The edge records decoded-frame receipt time before inference, rather than the later Event creation time. Face/body improvements have a five-second cooldown; camera-health heartbeat is 30 seconds.
- A local HTTP receiver verified authenticated V5 Event -> Evidence -> Embedding upload, acknowledgement, and outbox cleanup. It is not the real Management service.
- Stream interruption now emits camera-unavailable and track-lost, and recovery ends old presences as unknown under the old stream session before opening a new one. Binding-only revisions preserve the session and presence IDs in tests.
- Full V5.2 example desired state validated and translated inside the container.
- traffic1 and ch9 each decoded live as one-camera runs on the target GPU/NPU; `/readyz` became ready and schema-valid V5.2 records were persisted locally.
- traffic1 produced scene, presence, count, zone transition, and body sample records. ch9 additionally produced face and gait samples.
- A bounded 125-second three-camera run (`traffic1`, `traffic2`, `ch9`) with 8-fps targets and 2-second scene sampling reached `/readyz`. The camera workers ran at about 8, 8, and 5.5-6.7 fps respectively after warm-up. Scene sample counts were 53, 54, and 51, with average gaps of about 2.1-2.2 seconds and maximum gaps of 3-4 seconds. The test used full-frame scene, person identity, and vehicle presence bindings on each camera, not the complete example app set.
- All 3,812 queued observations, 1,457 evidence metadata records, and 592 embeddings from that run validated against the V5.2 schemas. All 1,457 JPEGs existed and matched their declared hashes, sizes, and observation links.
- With `dev3`, a bounded 85-second three-camera run on traffic1, traffic2, and ch9 held about 8 fps per worker after warm-up using full-frame scene, person identity, and vehicle presence bindings. It produced 1,747 schema-valid observations, 693 evidence items, and 277 embeddings with valid evidence hashes/links. Scene sampling averaged about 2.1 seconds per camera. This is not the full app set on three cameras.
- With `dev4`, the complete example app set on traffic1 reached `/readyz`, initialized vehicle/plate/fire on GPU, OCR on GPU+NPU, face on GPU+NPU, body/gait on NPU, scene on GPU, and VA-API decode. Its bounded 60-second run produced 410 schema-valid observations, 198 evidence items, and 105 embeddings. No smoke Event was emitted in that window. The observed full-app throughput was about 4-5 fps against an 8-fps target.

## Not Yet Release-Ready

- No real Management V5 ingest endpoint was connected. The live runs used the local outbox; Management acceptance, retention, backpressure, and replay remain unverified.
- Hardware decode was exercised with VA-API frames and software-decode configuration was rejected. The runtime does not yet persist per-model `EXECUTION_DEVICES` attestation or independent media-engine telemetry for an operator audit.
- The full-app single-camera profile did not meet its requested 8 fps. Three-camera and four-camera capacity with that full app set remain unverified.
- The `dev3` full-app run emitted a 0.40-score smoke suspicion on a frame without obvious smoke. `dev4` adds a 0.55 edge emission floor, but hazard precision/recall still needs a labelled video set; raising a threshold is not proof of accuracy.
- The three-camera run accumulated about 121 MB of queued V5.2 records in 125 seconds with no uploader. It also emitted high presence churn: traffic1 had 509 object_present, 686 track_lost, and 465 presence_ended_unknown records; traffic2 had 302, 422, and 270. These streams may have high subject turnover, but production gate/dedup calibration and live inspection are needed before treating these as trustworthy Management Events.
- Source/geometry/model-profile revisions still restart workers; old presences are not explicitly closed across that restart. Binding-only revisions are preserved in unit tests, but live hot-reload acknowledgement is not yet proven. View-change detection is not implemented.
- RTSP source PTS/camera capture time is not available in the current decoder. `observed_at` now uses edge decoded-frame receipt time, which is earlier than processing time but is not camera-origin capture time.
- Vehicle track reassociation remains conservative (new presence after tracker-ID break); person reassociation uses a qualifying body vector. Four-camera capacity, 1-second scene sampling, and scene-pressure behavior under sustained load remain unvalidated.
- V5.2 contract acceptance, exact face/body/gait model weight hashes, and full production-policy calibration remain open.

This development tag may be shared for integration testing. Do not deploy or label it as a conformant production release until these gaps are resolved.
