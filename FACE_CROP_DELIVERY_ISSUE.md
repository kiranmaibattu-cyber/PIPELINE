# Face Crop Delivery Gap

Status: resolved in the local v14 candidate on 2026-09-21; management must
implement the paired v14 artifact and sample endpoints before deployment.

## Problem

Some face embeddings reach management without a corresponding face crop. The
edge creates `event_frame` and `face_crop` before it emits the embedding, so an
accepted embedding without an image is normally a delivery/correlation failure,
not a local crop-generation failure.

The v13 uploader POSTed only JSON with an edge-relative `face_crop_url`. The v14
uploader instead sends exact crop bytes to the authenticated artifact endpoint,
persists its checksum acknowledgement, and only then submits the embedding.
SSE remains independent and is not used to populate the Faces UI.

## Implemented v14 transaction

1. Upload the JPEG with authentication, checksum, retries, and idempotency.
2. Have management durably store it and return a central artifact ID/checksum.
3. Submit or finalize the embedding record against that artifact ID.
4. Remove the edge outbox record only after both are acknowledged.

The edge implementation is complete. Production integration still requires the
management receiver described by `delivery/apexfabric-v1/intel-285h/traffic-v14`.
Raw embeddings remain absent from analytics events and logs.

The padded side-profile crop change is independent: it improves image content
but does not make crop delivery durable.
