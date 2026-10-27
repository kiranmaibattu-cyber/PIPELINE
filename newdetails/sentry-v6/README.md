# Sentry V6 CV contract package

V6 is a complete edge/Management boundary. Desired state, observations,
evidence metadata, embeddings and acknowledgements all carry
`schema_version: "6.0"`, and delivery uses `/api/v6/ingest/...` routes.

V6 is not wire-compatible with 5.2. Any persisted 5.2 outbox must be drained
with the previous runtime before upgrading; partially acknowledged records
must never be silently relabelled as V6.

This directory is self-contained for CV-team implementation and validation. It
includes:

- `sentry-v6-contract.yaml`: complete edge/management boundary and runtime behavior.
- `desired-state.schema.json` and `desired-state.example.json`.
- `observation.schema.json` and `observation-examples.json`.
- `evidence.schema.json` and `evidence.example.json`.
- `embedding.schema.json` and `acknowledgement.schema.json`.
- `validate_contract.py`: schema and representative boundary validation.
- `release.yaml`: runtime-image publication status.

Run the contract checks from this directory with:

```bash
python3 validate_contract.py
```

The runtime also defines camera fault isolation. A configured camera that
cannot decode becomes `unavailable`, retries in its own supervisor, and leaves
healthy cameras processing. `/readyz` reports the runtime ready when shared
models, the inference scheduler, and recovery loops are operational; it does
not require every camera to be healthy.

The recent management changes are compatible consumers of this behavior:
camera-health state uses the existing `healthy`, `degraded`, and `unavailable`
vocabulary; stale health heartbeats are shown as unavailable; and the dashboard
shows the reason and last reliable observation for the affected camera.

Only an image whose labels declare contract and wire schema `6.0`, and whose
digest is recorded in `release.yaml`, may claim V6 conformance.
