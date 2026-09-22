# Sporada Secure v16 six-application contract

CV-team entry point: [`CV-PIPELINE-HANDOFF.md`](CV-PIPELINE-HANDOFF.md).

This is an additive revision of v14. It preserves zone-based
`vehicle_counting` and introduces the distinct `vehicle_entry_exit_counts`
application. The control plane, not the edge image, owns cumulative daily
entry/exit totals.

The entry/exit line is an ordered pair of normalized points, `a` then `b`.
Looking from A toward B, the left side is IN and the right side is OUT. Reversing
the endpoint order swaps the physical sides. The UI labels A, B, IN, and OUT
while the line is drawn so operators can verify the orientation before deploy.

Contract files:

- `image-contract.yaml`
- `desired-state.schema.json`
- `analytics-event.schema.json`
- `face-artifact-response.schema.json`
- `face-sample.schema.json`
- `vehicle-crossing.schema.json`
- `vehicle-crossing-response.schema.json`
- `event.examples.json` and `vehicle-crossing.example.json`

Examples are illustrative and must validate against their corresponding schema.
The unchanged v14 face schemas continue to apply.
