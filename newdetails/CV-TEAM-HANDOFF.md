# Sentinel v2 CV implementation handoff

1. Run as UID/GID 10001 and expose `/healthz`, `/readyz`, `/metrics`, and optionally `/events` on port 8080.
2. Read only strictly increasing desired-state revisions and resolve camera streams from the declared secret files.
3. Create stable IDs once and persist the complete outbox record before sending.
4. POST the observation and wait for `created` or `existing`.
5. POST each evidence item as multipart fields `metadata` and `file`; compute SHA-256 over the exact uploaded bytes.
6. Only after evidence acknowledgement, POST its embedding and/or scene record.
7. Treat 200/201 as success, configured transient statuses as retryable, and 409 as an operator-visible dead letter.
8. Never emit raw vectors through SSE or logs, never assign a global person ID, and never call Management databases or LLMs.

Management rejects unknown cameras. Camera registration is therefore a control-plane prerequisite before enabling its CV desired state.
