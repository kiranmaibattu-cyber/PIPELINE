# Sporada Secure v16 Release

Release date: 2026-09-22

Hardware profile: Intel Core Ultra 285H (`linux/amd64`)

## Contents

- Applications: ANPR, vehicle counting, vehicle entry/exit counts, pedestrian
  counting, fire/smoke detection, and anonymous face observations.
- Intel placement: VA-API decode, OpenVINO vehicle/plate/face detection on GPU,
  face embedding on NPU, and OCR on the available GPU/NPU devices.
- Models are baked into the workload image. Desired state, camera URL Secrets,
  and persistent `/state` remain deployment mounts.
- Public desired-state, analytics-event, face-delivery, and vehicle-crossing
  contracts are in `delivery/apexfabric-v1/intel-285h/traffic-v16/`.

## v16 Quality Corrections

- Suppresses strongly overlapping cross-class vehicle detections before tracking.
- Selects the best geometric parent vehicle for each detected plate.
- Rejects undersized, blurred, implausibly shaped, and low-confidence plate crops.
- Computes OCR confidence from character logits instead of reusing detector
  confidence, and requires four stable reads before `plate_read` emission.
- Preserves all public event names, payloads, and snapshot assets from the
  versioned contract.

Both face and ANPR crops originate from the original decoded frame. Detector
letterbox coordinates are mapped back before cropping; only the final aligned
face chip and OCR tensor are resized to their model input dimensions.

## Verification

- `75` v16 source and contract tests passed.
- The live gate stream decoded with VA-API and ran at approximately 8 inference
  FPS on the Intel GPU/NPU runtime.
- A recorded traffic comparison showed that v15 emitted six unreliable reads
  from plate boxes as small as `11x7`, `13x4`, and `20x6`; v16 rejected those
  candidates while vehicle counting continued.
- No clearly readable plate crossed the test scene, so positive ANPR accuracy is
  not claimed by this release test.

The local/registry image references and immutable registry digest are recorded
after publication.
