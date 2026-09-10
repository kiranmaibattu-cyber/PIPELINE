# Surveillance Exact-Frame Evidence

Local candidate image: `surveillance-edge-runtime:intel-285h-2026.09.10-v2`.

The camera worker encodes its source frame before inference and observation
publication. Observations reference that immutable JPEG using a unique stream
session, frame number, capture time, original dimensions, and SHA-256 hash.
The capture time is the edge's frame receipt time, not the source recording date.

Observation-triggered events inherit this evidence and the observation's bounding
box. Snapshot generation reads that exact JPEG, verifies its hash, copies it to
persistent storage, and crops using the original coordinates. It never reads
the latest preview or the latest live boxes. The full-frame evidence is an
unannotated source JPEG; its hash equals the published evidence hash.

The worker cache is bounded to 64 frames and 64 MiB per camera, whichever limit
is reached first. JPEG encoding and temporary storage add work to the pipeline;
this release has not established maximum camera capacity under production load.
On eviction, missing data, or hash mismatch, the event reports
`payload.evidence_status = "evidence_unavailable"`. No newer frame is substituted.
Committed snapshots remain under the mounted `/state/surveillance/snapshots`.
Temporary caches are cleared when a replacement internal runtime starts.

For intrusion, identity, face recognition, and line-crossing events, the evidence
is the triggering observation's frame. Whole-frame occupancy retains its existing
track/TTL semantics: a nonzero aggregate references its latest contributing
observation; a timeout-only zero transition has no triggering frame and reports
unavailable evidence. Enrollment and identity-merge operations also may have no
camera observation. These are not claims of exact per-frame occupancy.

Public `payload.evidence` contains:

```json
{
  "stream_session_id": "unique-per-camera-worker",
  "camera_id": "ch9-evidence",
  "frame_id": 120,
  "captured_at": 1789010000.0,
  "frame_wh": [2560, 1440],
  "sha256": "source-jpeg-sha256",
  "bbox": [20, 10, 60, 70],
  "track_id": 7
}
```

The private temporary cache path is not exposed. Aggregate events omit the
individual track and bbox. Snapshot assets retain their existing API format.
File write time may follow observation time; content identity is established
by the frame reference and hash, not by matching filesystem timestamps.

## Verification

- `tests/test_frame_evidence.py` checks delayed reads, restarted frame counters,
  eviction, bounded bytes, and changed content.
- `tests/surveillance_evidence_container.py` runs inside the actual image and
  verifies that a later frame cannot change an earlier event's saved JPEG or
  person crop. It also verifies no fallback after eviction.
- `scripts/test_surveillance_evidence_live.py` uses ch9, compares downloaded
  snapshots against worker hashes, checks persistent URLs across restart,
  changes apps without restarting the container, and enables identity models.

Live artifacts are retained in `run/exact-evidence-<epoch>/` and ignored by Git.
These checks cover the edge's public API, not management-server storage or UI.

Validated on ch9 on 2026-09-10 with image digest
`sha256:8f71418350680acce21c4c233d2f087bd85027eea6b13101579e8ba506fe7923`.
Reports: `run/exact-evidence-1789026862/`.

| Stage | Events with matching source-frame hashes |
| --- | ---: |
| Initial intrusion and counting | 101 |
| After container restart | 112 |
| Live switch to counting only | 77 |
| Live switch enabling identity apps | 102 |

All 392 checked events had available evidence and downloadable assets. The
identity stage included 21 identity, 24 intrusion, and 57 counting events.
No enrolled-face recognition event was exercised because this isolated test
volume had no enrolled gallery. A separate actual-image regression verifies
the common observation-to-event propagation and exact crop bytes.
104 live person crops were also independently re-encoded from the event's
stored source frame and original bbox, with byte-for-byte matches.
