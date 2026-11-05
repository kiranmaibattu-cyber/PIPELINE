# Current Opt79 capacity — clarified and boundary-tested

For the tested Intel Ultra 9 285H host, 1080p busy crowd replay, all production bindings and scene search on every camera:

| Requested FPS per camera | Largest tested count maintaining FPS | Count also meeting p95 freshness ≤2 seconds | Next count tested |
|---|---:|---:|---|
| 8 FPS | **4 cameras** | **4 cameras** | Five missed FPS and freshness |
| 10 FPS | **3 cameras** | **2 cameras** | Four missed FPS; three maintained FPS but sometimes delivered old frames |

These are measured limits for this image/workload and the documented turbo-off / PL1 45 W / PL2 65 W condition. The original one-hour validation was two cameras at8FPS. New boundary runs used120-second steady holds; these counts are not a hardware maximum or a24/7 guarantee. Image, models, V8 contract and production gates were unchanged.

## New boundary evidence

All runs retain two-second active scene sampling, frame evidence and embeddings. Marked repeats measure source first-PES transmission to complete package acceptance at the agent API; camera capture/encoding and post-API work are excluded.

| Run | Matched inference FPS per camera | Rate criterion | Scene p95 source→API | Freshness |
|---|---:|---|---:|---|
| 5×8, original | 7.28–7.52 | FAIL | Not measured | — |
| 3×10, original | 9.97–10.30 | PASS | Not measured | — |
| 5×8, marked | 7.44–7.89 | FAIL | 5.284–8.728 s | FAIL |
| 3×10, marked | 9.98–10.25 | PASS | 0.663–3.079 s | FAIL |
| 2×10, marked | 9.99–10.01 | PASS | 0.416–0.426 s | PASS |

Three cameras at10FPS maintained the frame rate, but two had scene p95 latency around3.08/2.61 seconds. The two-camera10FPS marked test passed with p95 about0.426/0.416 seconds. Five cameras at8FPS averaged about7.28–7.52FPS in the unmarked run and failed again with markers. Earlier six-camera8FPS and eight-camera10FPS tests also failed their requested per-camera rates. This closes the previously missing neighbouring camera counts.

## Why the DL Streamer reference reports20 cameras at10FPS

The reference's200FPS is the primary detector total. Its reported240 combined FPS additionally counts40 smoke/fire inferences; it does not mean every secondary model processes every camera at10FPS. Checked local reference code and its recorded command show:

| Work | Reported20-camera reference | Current production image |
|---|---|---|
| Face detection | `FINT=43` in recorded benchmark | Every third eligible admitted frame |
| Secondary branch |1FPS admitted,7/10 used =0.7FPS/camera | Per-use-case/per-subject policy demand |
| Secondary pixels |GPU resize to640×360 before download | Original-resolution evidence/crops and gait background work |
| Scene encoder |Every seventh processed secondary frame, nominally about0.1FPS/camera |Every2seconds when active,0.5FPS/camera |
| Output |`fakesink`; secondary consumer discards payload |V8 observations, JPEGs, vectors and agent-API handoff |

Those settings perform substantially different work. The current integration also has remaining scheduling/copy/preprocessing costs; the tests do not establish that all of the performance gap is necessary to preserve the gates. Reusing DL Streamer elements has not reproduced the reference's20-camera performance under our full production workload. Further optimization must preserve the agreed policy rather than silently lowering sampling or dropping evidence.

## Evidence and status

Permanent evidence, the five new case directories and updated chronological handoff are on `admin1@192.168.1.64:/home/admin1/Documents/sentry-validation/`. Read `capacity-boundary-final/results.json` and `CAPACITY_RESULT.md`. The original123-case archive remains a historical snapshot; the active ledger now contains128 cases. No runtime changes or pushes were made. Temporary test CPU settings were restored after each controller; application services remain stopped for isolation. Existing UTC mapping and other image defects from the main report remain unresolved.
