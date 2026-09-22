# Sporada Secure v17 Live Acceptance

Date: 2026-09-22

Image: `localhost/sporada:intel-285h-2026.09.22-v17`

Image ID: `sha256:33c39fa24d7b8d9d13dc937a15db315e71054ffb9da40d73f4c323d312eb11d4`

## Results

| Application | Source | Result |
|---|---|---|
| Vehicle counting | `traffic1` RTSP | 346 schema-valid per-frame events; evidence URL retrieved (278,296 bytes). |
| Pedestrian counting | `traffic1` RTSP | 346 schema-valid per-frame events; evidence URL retrieved (278,296 bytes). |
| ANPR | `traffic1` RTSP | One `plate_read` event for `KA28V2228`; evidence URL retrieved (301,905 bytes). Text was not checked against independent ground truth. |
| Vehicle entry/exit | `traffic1` RTSP | Durable multipart crossings received and acknowledged; tests observed both `in` and `out`, all four current internal vehicle classes, zero remaining outbox records, and no SSE duplicate. |
| Face recognition | Gate-camera RTSP | `face_seen` SSE event, face crop, and 512-dimensional sample reached the simulated Management receiver. The pending sample resumed after container restart without re-uploading its acknowledged crop. No raw embedding appeared in SSE. |
| Fire/smoke | HTTP-served public flame clip | Nine schema-valid `fire_detected`/`smoke_detected` events; evidence URL retrieved (105,566 bytes). |

The live `traffic1` profile also loaded the smoke/fire model on Intel GPU without
errors, but emitted no fire event because that scene contained no qualifying
fire or smoke. A separate low-resolution close-up flame clip produced no alert;
the successful positive test used a broader tabletop-fire scene. This indicates
that model recall still depends on scene composition and needs dataset-level
accuracy evaluation.

## Evidence

- `run/sporada-v17-traffic-apps-1790072079/`
- `run/sporada-v17-face-delivery-1790072344/`
- `run/sporada-v17-fire-stream-1790072637/`
- `run/sporada-v17-crossing-1790071138/`
- `run/sporada-v17-crossing-1790071312/`

These ignored test directories contain reports, SSE captures, metrics, logs,
snapshots, and simulated Management receipts. All test containers were removed.

## Reproduction

```bash
python3 scripts/test_sporada_v17_traffic_apps_live.py
python3 scripts/test_sporada_v17_crossing_live.py
python3 scripts/test_sporada_v17_face_delivery_live.py
python3 scripts/test_sporada_v17_fire_stream_live.py
```

The fire test downloads and locally serves the Wikimedia Commons clip
`Video of tabletop fireplace (or fire pit) burning with removed limiter grid`.
It does not change model thresholds or runtime configuration.
