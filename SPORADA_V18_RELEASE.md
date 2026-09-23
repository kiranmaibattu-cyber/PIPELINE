# Sporada Secure v18 Candidate

Build date: 2026-09-23

Hardware profile: Intel Core Ultra 285H (`linux/amd64`)

## Local Image

```text
localhost/traffic-pilot-runtime:intel-285h-2026.09.23-v18
sha256:0aa49362a1297aa45699217837e37dd0181a29f6e4a78befb3edfeb40124cccf
```

The PIPELINE mirror reuses its unchanged v2 Intel runtime base and baked
OpenVINO models. The canonical tested standalone image is
`localhost/sporada:intel-285h-2026.09.23-v18` with image ID
`sha256:1d96213756ad26652238aed49b6cf94523c7e9c79c4dbdeb463930aee65bf0ee`.
Neither v18 image has been archived or published.

## Face Selection

- `minimum_quality` remains the usability floor; lower samples are discarded.
- `preferred_quality` defaults to `0.65` and can be set by Management.
- `selection_window_seconds` defaults to `1.5` seconds.
- A preferred sample is emitted immediately.
- If preferred quality is never reached, the best sample at or above the floor
  is emitted when the window expires.
- Candidate event-frame and contextual face-crop JPEGs are captured from the
  same exact original-resolution frame and retained compressed in memory only
  for the bounded window.
- Existing crop context remains two times the detected face extent. No invented
  padding or detector-resolution crop is used.

## Verification

- `82` source and contract tests passed.
- All bundled schemas and desired-state examples validated.
- A real 1920x1080 RTSP run delivered a fallback sample with quality `0.341127`
  and an `86x86` contextual crop, proving that a face below preferred quality
  is not lost.
- A separate run selected quality `0.466132` with a `124x124` crop.
- The final live run verified one identical `sample_id` across crop upload,
  512-dimensional embedding submission and `face_seen` SSE event.
- Artifact acknowledgement survived container restart; only the pending
  embedding was retried, and no raw embedding appeared in SSE.

Small or motion-blurred source faces can still look blurred when enlarged by
Management. Selection improves which available frame is sent; it does not
create source detail that the camera did not capture.
