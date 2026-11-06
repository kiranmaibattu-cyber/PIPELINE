# Coverage of the current isolated campaign

The ledger remains a282-scenario catalog; it is not a claim of282completed tests.

Last verified remote snapshot:2026-11-05 21:31UTC,87recorded native runs and23workload-budget runs. .64 became unreachable around21:32UTC. Later execution and cleanup are unverified.

`PASS` applies only to the recorded native/API assertion. `PARTIAL/PASS_COMPONENT_ONLY` means covered injected-state or preprocessing assertions passed against baked code; it is not whole-pipeline qualification. `PARTIAL/NOT_QUALIFIED` means some relevant evidence exists but catalog variants remain unverified. `FAIL` identifies a reproduced violation. `BLOCKED/UNVERIFIED` identifies requested verification unavailable after loss of remote contact. `NOT_RUN` confers no coverage. Model accuracy and downstreamManagement are outside this execution boundary.

Current counts: {'PARTIAL': 152, 'PASS': 10, 'NOT_RUN': 108, 'FAIL': 10, 'BLOCKED': 2}

See [PIPELINE_TEST_LEDGER.csv](PIPELINE_TEST_LEDGER.csv) for exact IDs, scopes and evidence locations and [the report](REMOTE64_TLSFIX2_VALIDATION_REPORT.md) for measurements. Remote paths refer to preserved evidence; some completed galleries were SHA256-verified and relocated to the local evidence mirror, with receipts. Failure cases stay failed even if other variants passed. No additional-runtime-variant PASS is inferred from an enabled app or unrelated output.
