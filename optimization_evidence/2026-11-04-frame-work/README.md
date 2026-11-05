# Bounded work reuse — 2026-11-04

Final local candidate: `localhost/sentry-v8-opt71-policy-work-reuse`.

Changes: reuse identical scene embeddings/JPEGs for overlapping scene bindings; skip gait inference only after identical history collection when existing output gates guarantee rejection. Models, tracker, decode, V8 schema and policy thresholds unchanged.

See `Containerfile.opt71` for the exact overlay. No registry push.

## Evidence

- `tests.txt`: 57 passed; `image-tests.txt`: five new checks inside image.
- `contract-validation.txt`, `embedded-validation.txt`: both passed.
- `opt71-crowd-2x10/`: final busy two-camera120s pass;170gaitcallsavoided,20gaitembeddingscaptured.
- `opt69-shared-scene/`: shared binding probe;39scene/39JPEG reuses; cachecode unchangedinopt71.
- `opt70-failed/`: refactorNameError, restoredvariable and addedeventregression beforeopt71.
- `opt67-baseline-native-crash/`: unchanged baselineworkerexit-11; not a valid performancecomparison.
- `jpeg-benchmark.json`: broadcacheoverheadreason; singletoncachingwasremoved.
- `opencv-existing-runtime.txt`: existingruntimealreadysetsoneOpenCVthread. No newthreadbenefitclaim.

Limits: sharedhost, shortreplays, localmockdeliveryagent, no maximumcameraqualification or overall speedup claim. ROI conversionstilldisabled; nativecrashrecovery, asynchronoussecondaryexecution, nativefaceadmission andstatefulgaitpreprocessing requireseparatework. Fullrecord in repository `OPTIMIZATION_HANDOFF.md` sections19–20.
