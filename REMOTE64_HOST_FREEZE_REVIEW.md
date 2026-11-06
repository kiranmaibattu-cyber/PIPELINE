# Remote .64 host freeze — recovered evidence

Updated UTC: 2026-11-06T04:48:08.395321+00:00. Image digest remains `sha256:b5a5dc68db4cba9e2b18ec28c61175686189161b25f605b298c1241dbc4957fc`. This is a postmortem checkpoint, not a completed capacity qualification.

The user confirmed that `.64` froze and required a restart. It is now reachable. The machine booted around09:37IST on2026-11-06; the previous journal ends around03:02IST. The earlier loss of SSH was a symptom of the confirmed host freeze. The trigger remains unknown.

## What was running

The interrupted case was `SX-mixedPV-6x8-budget`: six logical cameras,8selectedFPS each, encoded1920×1080/30FPS localhost TCP MPEG-TS replay, scene sampling on every camera. SX enabled scene, person/vehicle presence, person/vehicle counts and entry/exit lines. It did not enable ANPR, face/body/gait identity or fire/smoke. Primary/scene inference used the GPU; NPU busy was0% in the last recovered samples. These observations do not exclude an idle-device/driver issue.

Before the freeze,87runner records and23fixed-budget runs had completed. The interrupted case has no final verdict and must not be counted as another passing measurement. Its local source replay did not rely on the external Ethernet link.

## Last surviving measurements

The per-case compressed monitor has a damaged tail. Its original bytes are preserved; a separate recovered prefix contains100valid records, ending at2026-11-05 21:32:09.059UTC. This is not the exact time of failure.

| Measurement | Last recovered value |
|---|---:|
| CPU package temperature |67°C|
| Maximum package temperature in recovered100records |77°C|
| Container cgroup memory |2,026.95MiB, about1.98GiB|
| Host available RAM |27.12GiB|
| Container CPU |3.297cores out of16|
| Whole-host CPU |23.0%|
| GPU GT active residency |84.14%; this is not engine compute utilization|
| Node NPU busy |0.0%|

All six camera counters were still advancing in the recovered prefix, and pending native admissions were0–1in the final sample. These samples provide no evidence of RAM exhaustion or sustained overheating. The gap between the final surviving sample and the actual freeze prevents ruling out every later transient condition.

Docker preserved the stopped container `e38616288792` / `sentinel-v8-capacity-6-1654387`. It reports `OOMKilled=false`. Its exit255 and FinishedAt04:08UTC were recorded across the host restart; they must not be presented as proof that an application exit255 caused the earlier freeze.88preserved Docker log lines end at21:32:05UTC with normal monitor messages. Last persisted log timestamps do not establish execution ordering through an abrupt host failure.

## Kernel evidence and its limits

No recorded panic, orderly shutdown or suspend explains the21:32event in the reviewed journal window. Pstore is empty. This does not prove that a kernel/driver fault did not occur: a hard freeze may prevent a final record reaching disk.

The previous boot contains earlier warnings:

- GPU boot log: GuC70.53.0recommended,70.36.0loaded.
- NPU power-state IPC timeouts and forced cold boot at05:46,07:16and08:53UTC onNov5.
- An i915 fence-expiration timeout at09:47UTC and earlier/later wakeref workqueue warnings.

Those warnings are diagnostic leads. Their timestamps do not establish the cause of the later21:32freeze. No firmware, kernel, driver or image change has been applied in response.

## Evidence recovery

- The23previously mirrored detailed runs remain available.
- The interrupted temporary capture is now archived locally and every one of its778regular files matches the post-freeze source manifest. It contains2,784valid accepted-package rows,287scene samples and one incomplete trailing package-record line. Last preserved acceptance is21:32:14.829UTC.
- Verification concerns the bytes that survived the hard reset. It cannot recover writes that never reached storage. The reference receiver is not a real durable-agent implementation; this is not a power-loss durability verdict against CV or Management.
- A5,401,323,520-byte archive of the remaining campaign tree was created successfully. ExpectedSHA256: `b43a986c385a6937c16aad666634c53cd85d6435c5ceee88a3d0be4edda3b0d5`. Its manifest covers34,557regular source files.
- That full archive is still copying. The one-time local user service `sentry-evidence-recovery-20261106.service` makes the transfer resumable across chat/terminal exit, with a six-hour runtime bound. It verifies the full archive and every manifest file before reporting success. Only its redundant remote TAR is removed after verification; original evidence remains.
- `.64`currently reports a10Mbps Ethernet link; the local peer reports1000Mbps. Observed copy speed is about1.1MiB/s. This explains slow recovery transfer and limits future live-stream ingestion. The link speed at the time of the freeze was not independently recorded, and this is not a causal explanation for the host freeze.
- Temporary recovery archives currently consume about5GB on `.64`and leave about4.2GiB free in `/home`. No new load tests are running. The background job releases its large redundant remote TAR only after successful verification.

## Current state and next diagnostic boundary

The new boot's actual no-turbo and all recorded RAPL power values match the original pre-test settings. The former supervisor did not execute its final cleanup; reset values were verified after reboot. No inference test has been restarted, and no image, production policy, model, contract/schema or repository push was changed.

Before a large sweep, prepare external kernel/resource capture and a bounded minimal reproduction so the next failure yields information even if the host needs another reset. Verify the current driver/firmware combination and separate decode, primary/tracker, scene and repeated start/stop paths through controlled tests. Do not change model gates or prescribe a driver/firmware fix without evidence. The remaining8/10FPS profile sweep and pre-soak/hour qualification remain incomplete.

Local evidence: `optimization_evidence/2026-11-05-remote64-tlsfix2/recovery/`. See `current-state.json`, `interrupted-scratch-verification.json`, `interrupted-container-state.json`, `interrupted-container.log`, `previous-boot-kernel-filtered.log`, `post-reboot-host-read.txt`, `resumable-archive-status.json`and`background-copy.json`. The complete-copy marker is `full-evidence-verification.json`; do not infer completion from the existence of a partial TAR.


### Recovery completion — 2026-11-06

The full 5,401,323,520-byte evidence archive completed copying and verification at 05:40:58 UTC (11:10:58 IST). All 34,557 manifest files passed, with zero failures. SHA-256: `b43a986c385a6937c16aad666634c53cd85d6435c5ceee88a3d0be4edda3b0d5`. The separate interrupted capture had already passed all 778 file checks. These checks verify surviving post-freeze bytes; damaged or unflushed original records remain explicitly incomplete.

The one-time local copy service finished successfully and is inactive. Only the redundant temporary remote TAR was removed after verification. The original remote campaign tree and local verified archive remain. The original image, models, gates and contract were unchanged.

Data recovery is complete. The remaining inference tests are still incomplete following the confirmed host freeze; no full test sweep was restarted. Before new isolated tests, account for the separate `sentry-cv.service` workload that auto-started after reboot.

Local verification: `optimization_evidence/2026-11-05-remote64-tlsfix2/recovery/full-evidence-verification.json`. Archive: `remote-root-after-freeze.tar` in the same directory.
