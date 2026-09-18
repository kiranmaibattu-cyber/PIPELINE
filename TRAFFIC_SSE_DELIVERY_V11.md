# Traffic SSE Delivery v11

Traffic v11 changes only event transport and journal retention. Existing ANPR,
vehicle counting, pedestrian counting, fire/smoke, face-sample extraction,
models, hardware placement, RTSP recovery, snapshots, and desired-state reload
behavior remain present.

## Analytics SSE

- `GET /events` starts at the current journal EOF for every connection.
- Historical records are not replayed after reconnect or container restart.
- Every analytics message contains `id: <event_id>`.
- Idle connections receive `: heartbeat` every 15 seconds.
- Delivery is at-most-once. A disconnect can lose an event; it cannot cause the
  runtime to replay the mounted journal.

This is intentionally different from the face-sample management outbox. Face
samples and their evidence remain queued until the authenticated management API
acknowledges them.

## Journal

- Active path: `/state/events/analytics.jsonl`.
- Rotation occurs before an append would exceed 536870912 bytes.
- Retained predecessor: `/state/events/analytics.jsonl.1`.
- At most two journal data files are retained and their combined size stays
  below 1 GiB.
- A separate lock file coordinates writers from multiple camera processes.
- Connected readers drain the old file descriptor and then continue at offset
  zero of the new active file.

## Verified Image

- Image: `localhost/traffic-pilot-runtime:intel-285h-2026.09.18-v11`
- Image ID: `6fedc723514157d862457e8cd2f228dedd5b513a92b8c1b1c6679b80d0eee382`
- Digest: `sha256:f44351770de9b2d61ba791004b117c292be1b466b33d431e2aac224473ae8f39`
- Archive SHA-256: `617401b7c77d134fa72f68abeecad570e1e525cb171ef98e9a88a3c2197a8a7c`

The automated suite passed 38 tests. A real HTTP client skipped a historical
record, received a new event with matching SSE and payload IDs, stayed connected
through a rename-based rotation, received the post-rotation event, and observed
the 15-second heartbeat. A new connection replayed zero bytes. A full live run
against `rtsp://192.168.1.95:8554/traffic1` loaded all five applications,
processed at approximately 5 inference FPS, and confirmed that the first live
SSE ID was absent from 696 IDs already present before connection.
