# Sentry V8: CV runtime and delivery-agent handoff

## Purpose

V8 removes durable-delivery ownership from the CV image. The CV runtime
produces observations, evidence, and embeddings. The delivery-agent sidecar
owns durability, sequencing, retries, skips, eviction, credentials, and the
existing V7 control-plane ingest protocol.

This split fixes the V7 failure mode where many camera processes and an
uploader wrote the same SQLite database. The CV container must never mount
`/state`, open the delivery database, allocate a stream sequence, or call a
control-plane ingest endpoint.

## Pod boundary

Each deployment is one pod containing:

```text
CV runtime --HTTP over Unix socket--> delivery agent --> HTTPS control plane
                                            |
                                  deployment-specific PVC
```

The containers share only `/run/sentinel-delivery`, backed by an `emptyDir`.
The agent listens on `/run/sentinel-delivery/agent.sock`. Only the agent mounts
the deployment PVC at `/state`. Camera URL secrets and accelerator devices are
mounted only into the CV runtime. The deployment token, box credential, site
CA, placement generation, and ingest URL are mounted only into the agent.

One physical box may host several deployment pods. Every pod has a separate
agent, SQLite database, sequence stream, credentials, and PVC. Never run the
same deployment on two boxes concurrently.

## CV-team interface

The CV runtime receives:

```text
SENTINEL_DELIVERY_AGENT_SOCKET=/run/sentinel-delivery/agent.sock
```

It submits `multipart/form-data` to `POST /v1/packages` over HTTP on that Unix
socket:

- `manifest`: JSON matching `delivery-agent-package.schema.json`.
- `evidence_N`: one file part for every manifest evidence entry.

The manifest contains one observation, zero or more evidence objects, and zero
or more embeddings. Their wire payloads remain schema `7.0`. Array order is
stable and determines sequence allocation after the observation.

The CV runtime chooses a stable UUID `package_id` before its first attempt and
submits it through exactly one deployment-scoped handoff scheduler shared by
all camera workers. A camera worker must not create its own retry queue or
background uploader. It uses bounded memory and issues at most four simultaneous
handoffs. Exact queue limits, event tiers, reserved capacity, and coalescing
keys are CV production-policy decisions rather than wire-contract fields.

The contract requires that optional or replaceable work never starve
non-replaceable facts. The CV production policy must coalesce or drop optional
work before rejecting non-replaceable work, must never cancel an in-flight
handoff, and must report rejected work without claiming it was produced.

For a retryable network error, 30-second request timeout, `429`, or `503`, the
scheduler makes at most six total attempts using the exact same `package_id`,
manifest, and evidence bytes. Delays before attempts 2–6 are exactly 250 ms,
500 ms, 1 s, 2 s, and 4 s. It does not retry `409`, another non-retryable 4xx,
or a response whose `retryable` field is false. When the scheduler reaches its
package/byte bound, it rejects new work and reports delivery backpressure. When
all six attempts fail, it reports `delivery_handoff_exhausted`, releases the
in-memory package, and must not claim that package was produced.

The scheduler treats the following responses as final success:

- `201 durable` for the first durable commit; or
- `200 duplicate` for an identical committed package.

Both are final success. A `409` means the same package ID was reused for
different bytes and is a fatal producer error. `429` means the outbox cannot
accept new work; no sequence was allocated. `503` means the agent could not
make the package durable; no sequence was allocated. The bounded scheduler
above handles retries and reports delayed or dropped production explicitly.

The CV runtime may forget a package only after `durable` or `duplicate`. It
must not keep a second disk outbox.

The package digest is exactly SHA-256 over:

1. canonical manifest bytes produced by `json.dumps(manifest, sort_keys=True,
   separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")`;
2. then, for each manifest evidence entry in array order, one NUL byte, the
   UTF-8 `part_name`, one NUL byte, and the raw evidence bytes.

Multipart boundaries, headers, and transport framing are excluded. This digest
is the content identity behind `package_id`; the same ID with another digest is
`409 package_id_conflict`.

A package is “produced” only after the CV receives `201 durable` or `200
duplicate`. Before that it is merely in flight and remains in producer memory.
If the agent committed but the response or CV process was lost, the agent still
delivers its durable copy. If the agent had not committed before the CV crash,
the package may be lost, no sequence was allocated, and the runtime must not
claim it as produced. This is the intentional consequence of having no second
CV disk outbox.

The agent rejects duplicate evidence part names, missing or additional
multipart parts, incorrect evidence sizes or hashes, children belonging to a
different observation, evidence not listed by the observation, and embeddings
that do not reference package evidence. The acknowledgement contains exactly
the observation, all evidence in manifest order, then all embeddings in
manifest order, with contiguous sequences and no extra or missing entry.

## Delivery-agent requirements

The agent validates deployment identity, payload schemas, evidence size and
hashes, and parent/child references. It writes evidence to a temporary file,
fsyncs it, atomically renames it, fsyncs the directory, and commits the package
and all allocated sequences in one SQLite transaction. It returns success only
after all of those operations complete.

There is exactly one agent process and one serialized SQLite writer connection
per deployment. SQLite uses WAL, `synchronous=FULL`, foreign keys, short
transactions, and a bounded busy timeout. No other process or container may
write or directly inspect the live database for application behavior.

The initial release uploads one object at a time in strict sequence order.
For a package it sends observation, evidence in manifest order, then embeddings
in manifest order. A `200 accepted` or `200 duplicate` is stored locally before
the next sequence is sent. If storing that acknowledgement is temporarily
blocked, the agent retries the local transaction without issuing another HTTP
request whose successful response it already knows.

The agent uses the unchanged V7 endpoints, headers, hashing rules, fencing,
skip reasons, and acknowledgement schema. Control-plane receipts remain the
authoritative remote idempotency record.

Future bounded parallel upload is allowed only after qualification. HTTP
requests may finish out of order, but local acknowledgement, deletion, and
contiguous progress must commit in sequence order.

## Health and status

CV `/readyz` reports configuration, camera supervisors, models, and inference
scheduler health. Delivery-agent `/readyz` reports that the Unix socket accepts
packages and the outbox is writable. Loss of control-plane connectivity does
not make local durability unready; it makes delivery status degraded/offline.

`GET /v1/status` exposes no credentials and includes:

- outbox bytes and cap;
- pending record and package counts;
- oldest pending age and sequence;
- last durable local package time;
- last successful remote upload time;
- contiguous and received watermarks;
- connection state and bounded last-error code.

The pod is Ready only when both containers are Ready. Management separately
surfaces stalled delivery even while local capture remains available.

`GET /v1/status` matches `delivery-agent-status.schema.json`. CV `/healthz` and
`/readyz` own configuration, models, inference, and camera state only. Outbox,
upload, watermark, and delivery-error fields belong only to the agent status.
Management may show both, but must not infer camera health from delivery health
or delivery health from camera health.

All local error bodies match `delivery-agent-error.schema.json`. In particular,
conflicting bytes return `409 package_id_conflict`, a full outbox returns `429
outbox_full`, local concurrency pressure returns `429 agent_busy`, and a
durability failure returns `503 durability_unavailable`. Codes and messages are
bounded and `retryable` is explicit.

The Unix socket is owned by UID/GID `10001:10001` with mode `0660`. The agent
allows at most four simultaneous handoffs, requires `Content-Length`, rejects
requests above 64 MiB before multipart parsing, and applies a 30-second handoff
timeout. These bounds protect memory; they do not add another queue. Accepted
handoffs still converge on the one serialized SQLite writer.

## Migration from V7

The initial V8 release scope is new V8 deployments. It must not automatically
mount or reinterpret an existing V7 deployment PVC. Upgrading an existing V7
deployment is a separate, explicit control-plane operation: stop the runtime,
take a backup, reconcile every local sequence with `GET
/api/v7/ingest/stream`, retry existing objects idempotently, treat `duplicate`
as acknowledged, and atomically mark the new agent store active. It must never
fabricate skips for payloads that still exist.

New V8 deployments start with an empty agent-owned store. The CV runtime has no
V7 database migration code.

The importer is control-plane release tooling, not part of the CV image and not
part of the normal agent startup path. Existing V7 deployments must remain
stopped until that separately shipped importer supports their exact V7 SQLite
schema, creates a recoverable backup, reconciles the remote stream, imports
without renumbering, fsyncs the new store, and atomically marks completion. An
unsupported or interrupted import leaves the original and backup untouched and
must prevent the V8 pod from starting. New V8 deployments require no importer.

The seven-day edge-outbox retention clock starts at the agent's durable
`outbox_created_at`, not the camera-provided `observed_at`. `observed_at`
remains authoritative for event timelines and control-plane retention, but it
must never cause a newly durable local package to expire immediately because a
camera clock was wrong or an observation arrived late.

## Qualification gates

Release qualification must demonstrate:

1. Ten concurrent camera producers cannot create SQLite lock failures.
2. Repeating an identical package returns `duplicate` with the same sequences.
3. A package commit is all-or-nothing across observation, evidence, embeddings,
   files, and sequence allocation.
4. Killing the CV container does not damage or stop the agent outbox.
5. Killing the agent at every fsync/commit boundary recovers without a used
   sequence lacking a record.
6. Network loss continues durable local capture up to the configured cap and
   reconnect drains in order.
7. `accepted` and `duplicate` acknowledgements both delete data only through
   the contiguous watermark.
8. Offline eviction persists `skip_pending` before deleting bytes and later
   submits skips in order.
9. Credential rotation takes effect after restarting the deployment pod.
10. Two deployments on one box both start at sequence 1 and never share files,
    credentials, watermarks, or database locks.
11. Fifty-camera admission is tested separately from the recommended qualified
    camera count; the hard configuration cap is not a performance claim.
12. Redis absent: local persistence and remote ingest continue to work.
13. Fifty simultaneous camera producers still use one shared scheduler: no
    per-camera retry queue appears, retained work never exceeds eight packages
    or 256 MiB, active handoffs never exceed four, and retry exhaustion occurs
    after the documented six attempts and five delays.

## Control-plane release steps

1. Publish and pin the delivery-agent image by digest.
2. Register a qualified V8 CV image with `contract_version=8.0`,
   `wire_schema_version=7.0`, `multi_box_delivery=7.0`, and
   `delivery_agent_protocol=1.0`.
3. Generate the two-container pod, Unix-socket volume, agent-only PVC mount,
   and agent-only credential injection.
4. Run the V7-to-V8 PVC importer only for explicitly selected stopped
   deployments.
5. Verify `/readyz`, `/v1/status`, stream progress, and outbox cleanup before
   scaling the deployment.
