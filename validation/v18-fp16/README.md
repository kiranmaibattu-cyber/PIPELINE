# v18 FP16 Validation Evidence

- `exact-overlay/vehicle-detections.jpg` is a validation-only overlay built
  from one exact event frame and the object list published in that event.
- `exact-overlay/traffic-live-report.json` contains the four-application live
  RTSP result.
- `exact-overlay/face-live-report.json` records face-sample and crop delivery.
- `exact-overlay/crossing-live-report.json` records durable entry/exit delivery.
- `exact-overlay/hot-reload-live-report.json` records the revision update that
  retained both the container and worker processes.

The production snapshot remains unmodified; bounding boxes remain event
metadata under the existing v18 contract.
