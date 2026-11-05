# Sentry V8 CV contract package

Status: ready for CV-team and delivery-agent implementation; no image is
qualified or released yet.

V8 keeps the V7 observation, evidence, embedding, and control-plane ingest
wire protocol. It changes ownership of durable delivery:

- The CV runtime produces one complete logical package and submits it over a
  Unix socket.
- A control-plane-owned delivery-agent sidecar is the only process allowed to
  write the per-deployment SQLite outbox or `/state` PVC.
- The delivery agent allocates sequences and calls the existing
  `/api/v7/ingest/*` endpoints.
- Deployment and box credentials are visible only to the delivery agent.

Each Kubernetes deployment pod has one CV container, one delivery-agent
sidecar, one Unix-socket `emptyDir`, and one deployment-specific PVC. Multiple
deployments on a box therefore have independent agents, sequences, and PVCs.
Within each deployment, all camera workers share one bounded handoff scheduler;
per-camera retry queues are forbidden. The contract requires optional work not
to starve non-replaceable facts; exact tiers, queue limits, reservations, and
coalescing keys belong to the CV production policy. Edge outbox age is measured
from durable local acceptance, while `observed_at` remains the event and
control-plane retention timeline.

Important files:

- `sentry-v8-contract.yaml`: complete responsibility boundary.
- `delivery-agent-package.schema.json`: local package manifest.
- `delivery-agent-acknowledgement.schema.json`: durable local acknowledgement.
- `delivery-agent-status.schema.json`: stable delivery health/status response.
- `delivery-agent-error.schema.json`: bounded machine-readable local errors.
- `delivery-agent-package.example.json`: multipart handoff example.
- `DELIVERY_AGENT_IMAGE_HANDOFF.md`: CV-team and platform-team implementation
  requirements and qualification gates.
- The V7 wire schemas and examples are copied here unchanged for a
  self-contained handoff.
- `validate_contract.py`: static and relational checks.

Run:

```bash
python3 solution-packs/sentry-v8/validate_contract.py
```

V8 does not require Redis. Redis may be used for disposable caches, but never
for authoritative outbox state, sequence allocation, or acknowledgement state.
