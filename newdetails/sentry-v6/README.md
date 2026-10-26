# Sentry V6 CV contract package

V6 is a narrow behavioral revision of the V5.2 contract. It does not add or
change desired-state, observation, evidence, embedding, or acknowledgement
fields. The wire schema remains `5.2`, while the desired-state contract marker
is `sentry-v6` and the package schema identity is `6.0`.

This directory is self-contained for CV-team implementation and validation. It
includes:

- `sentry-v6-contract.yaml`: complete edge/management boundary and runtime behavior.
- `desired-state.schema.json` and `desired-state.example.json`.
- `observation.schema.json` and `observation-examples.json`.
- `evidence.schema.json` and `evidence.example.json`.
- `embedding.schema.json` and `acknowledgement.schema.json`.
- `validate_contract.py`: schema and representative boundary validation.
- `release.yaml`: runtime-image publication status.

Run the contract checks from the repository root with:

```bash
python3 solution-packs/sentry-v6/validate_contract.py
```

The only runtime contract change is readiness and camera fault isolation. A
configured camera that cannot decode must become `unavailable`, retry in its
own supervisor, and leave healthy cameras processing. `/readyz` reports the
runtime ready when its shared models, inference scheduler, and recovery loops
are operational; it does not require every camera to be healthy.

The recent management changes are compatible consumers of this behavior:
camera-health state uses the existing `healthy`, `degraded`, and `unavailable`
vocabulary; stale health heartbeats are shown as unavailable; and the dashboard
shows the reason and last reliable observation for the affected camera.

No runtime image currently claims V6 conformance. Publish and verify a new,
immutable image and digest before registering a V6 release in the catalog.
