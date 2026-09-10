# Illegal Parking Plate Evidence

Candidate: `traffic-edge-runtime:intel-285h-2026.09.10-v2`.

Selecting `illegal_parking` now explicitly requires vehicle tracking, plate
detection, and OCR in the graph. Management can select only illegal parking;
it does not need to enable ANPR. The parking ROI and dwell rule are unchanged.
No separate ANPR events are enabled by these dependencies.

A parking event contains the event frame and vehicle crop, plus a plate crop
when detected. Confirmed OCR text is included when available. Vehicle and plate
evidence are linked through the event ID and `vehicle_ref`.

`payload.plate_status` is one of:

- `recognized`: the track has OCR text (this is not a ground-truth accuracy claim).
- `detected_unreadable`: a plate crop exists but no confirmed text is available.
- `not_visible`: no retained or current plate crop is available; the parking
  alert still fires.

`payload.plate_evidence` describes the plate **snapshot**:

- `basis`: `event_frame` or `earlier_track_frame`.
- `observed_at`: time that plate evidence was observed at the edge.
- `frame_id`: originating frame number.
- `vehicle_ref`: the vehicle track shared with the parking event.

If the plate disappears before the dwell trigger, the runtime can attach an
earlier crop from the same track. This cache is active only on parking-enabled
cameras, expires after 300 seconds without a plate sighting, and is limited to
128 entries of at most 256 KiB each. It is discarded on worker restart.
Earlier plate crops are explicitly marked rather than presented as simultaneous
with the event frame. OCR remains track-stabilized and may represent an earlier
observation even when the crop is from the event frame.

All advertised snapshots are written under the existing persistent traffic
state volume and accessed through `/snapshots/...`. Management should use the
provided asset URLs and keep the vehicle reference when storing the event.

Validation includes graph dependency tests, an event with a plate that disappears
before the trigger, an alert without any plate, and a live parking-only test on
`traffic1` using `scripts/test_parking_plate_live.py`. Live test artifacts are
retained under ignored `run/exact-evidence-parking-<epoch>/` directories.
The live test validates event types, evidence presence, and API retrieval, not
OCR transcription accuracy or detector precision on distant vehicles.

## Combined Live Test: 2026-09-10

Command: `python3 scripts/test_parking_plate_live.py --with-anpr --duration 120`.
The candidate image ran on `traffic1` with a full-frame parking ROI and both
apps enabled. Results: 25 parking events, 7 ANPR events, and 96 advertised
JPEG assets successfully fetched through the API. All 25 parking events had
event-frame, vehicle, and plate crops; 4 included confirmed OCR text and 12
used explicitly labelled earlier-track plate crops. Four vehicle references
linked separate parking and ANPR events. Not every parking vehicle produced
an ANPR event within the observation window.

Evidence: `run/exact-evidence-parking-1789030784/report.json`,
`matched_events.json`, `events.sse`, and persistent `state/` snapshots.
The test container was stopped and removed; artifacts were retained.
