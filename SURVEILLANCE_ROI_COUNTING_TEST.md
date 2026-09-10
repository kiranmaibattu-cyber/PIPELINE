# Surveillance Polygon Counting Live Test

Tested on `rtsp://192.168.1.95:8554/ch9` using local image
`surveillance-edge-runtime:intel-285h-2026.09.10-v3`, image ID
`ea000ce37f9bb6d567cd969b76aad943e4304b835878de3179bdbae82580b41a`.
This candidate is not yet exported or published to GHCR.

Command: `python3 scripts/test_surveillance_roi_live.py`.

- Only people counting enabled, at 8 FPS; VAAPI decode, detector-only runtime.
- 60-second collection with whole-frame, left-half and right-half polygons:
  224 ROI occupancy events, with independent counts for each named polygon.
- All 128 advertised snapshots were fetched through the API and their JPEG
  hashes matched the event source-frame hashes. 96 timeout-driven events had
  no source evidence and no snapshot references, rather than stale images.
- Desired-state revision 2 replaced the polygons with a tiny empty-corner ROI.
  It emitted zero during the next 35-second collection; no old-region events
  had timestamps after the new region's first event.
- Container StartedAt remained unchanged; only the runtime child reloaded.
- The SSE reconnect also returned previously generated events with stable IDs;
  consumers must deduplicate. This test excludes known event IDs and checks
  new-region activation timestamps, not just presence in a reconnected stream.
- Test container stopped and removed. Persistent test state and snapshots remain
  in `run/exact-evidence-roi-1789034981/` with SSE captures, JSON reports and logs.

80 unit tests also passed. They cover inside/outside classification, duplicate
observations, overlapping regions, source-coordinate normalization, zero/timeout
behavior, camera isolation, and simultaneous ROI and line counting. The live
test is functional validation, not a manual ground-truth counting accuracy audit.
