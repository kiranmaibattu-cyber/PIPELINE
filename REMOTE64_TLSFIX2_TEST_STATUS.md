# Remote64 TLSfix2 testing — ongoing

The frozen published image is being tested in the isolated `.64` host. Tests are **not complete**. See [handoff](OPTIMIZATION_HANDOFF.md) and the remote campaign root `/home/admin1/Documents/sentry-validation-v8-tlsfix2-20261105`.

Image digest: `sha256:b5a5dc68db4cba9e2b18ec28c61175686189161b25f605b298c1241dbc4957fc`. Runtime/models/production gates/contract unchanged. Stock startup hit105°C and was excluded; ongoing results use recorded no-turbo45W/65W profile, not unrestricted stock power.

The current short controls show different limits for frame processing, scene output cadence and all-use-case workloads. No hardware maximum or sustained capacity is claimed yet. UTC timestamps, rejected-sample state handling and nested log redaction have reproduced failures. A five-camera native startup abort is also preserved.

| Completed short run | Inference FPS, worst camera | Scene p95, worst camera | Narrow performance result |
|---|---:|---:|---|
| SA-1x8-power45w-r2 | 7.90 | 343 ms | Pass |
| SA-1x5-power45w-r3 | 4.98 | 343 ms | Pass |
| SA-1x10-power45w-r2 | 9.87 | 369 ms | Pass |
| SA-2x5-power45w-r2 | 4.96 | 349 ms | Pass |
| SA-4x5-power45w-r2 | 5.00 | 429 ms | Pass |
| SA-8x5-power45w-r2 | 4.82 | 8604 ms | Fail |
| SA-2x8-power45w-r2 | 7.86 | 390 ms | Pass |
| SA-4x8-power45w-r2 | 7.86 | 3598 ms | Fail |
| SA-3x8-power45w-r2 | 7.90 | 461 ms | Pass |
| SA-2x10-power45w-r2 | 9.86 | 465 ms | Pass |
| SA-4x10-power45w-r2 | 9.98 | 6141 ms | Fail |
| SA-3x10-power45w-r2 | 9.87 | 2416 ms | Fail |
| S-1x5-power45w-r2 | 4.90 | 240 ms | Pass |
| S-2x5-power45w-r2 | 4.89 | 242 ms | Pass |
| S-4x5-power45w-r2 | 4.89 | 278 ms | Pass |
| S-8x5-power45w-r2 | 4.88 | 384 ms | Fail |
| S-5x5-power45w-r2 | 4.89 | 287 ms | Pass |
| S-6x5-power45w-r2 | 4.89 | 326 ms | Pass |
| S-7x5-power45w-r2 | 4.89 | 366 ms | Fail |
| S-1x8-power45w-r2 | 7.79 | 230 ms | Pass |
| S-2x8-power45w-r2 | 7.78 | 254 ms | Pass |
| S-4x8-power45w-r2 | 7.78 | 265 ms | Pass |
| S-8x8-power45w-r2 | 7.82 | 2703 ms | Fail |

These are encoded1080p30 crowded replay points; scene enabled on every camera. `SA` declares all applications, `S` scene only. Actual positive model work/outputs are retained separately. A short pass does not close timestamp, durability or long-term stability requirements.

Completed since the earlier table: old/new paired control; native quiet/motion, ANPR, hazard suspect/clear,720p/1440p, live RTSPS and an untrusted TLS relay; unreachable-first/last peer isolation; missing GPU/NPU/model negative controls; ordinary configuration and membership changes; source Secret change; worker kill/recovery; unwritable metrics storage;15-second source interruption; agent503/429/lost-ACK/slow-response controls.

Reviewed controls: ordinary configuration updates, camera membership, worker recovery and normal common-cutoff API accounting passed their narrow assertions. Source Secret switching FAILED: changed camera kept old1920x1080 revision1 scene frames while parent health and unchanged peer applied revision2; replacement1280x720 publisher was verified available. Unwritable metrics storage FAILED with25 PermissionErrors and camera-thread exceptions. Missing-device tests stopped analytics without successful CPU fallback, but expected explicit device diagnostics were absent. Initial native scenario NULL verdicts mean pending semantic review, never PASS.

Lost-ACK retries produced144 duplicate acknowledgements and0 protocol errors; do not count duplicate ACKs as missing unique packages.503 and429 fault windows each produced4 exhausted packages, so outage delivery cannot be called lossless. The API receiver checks complete bytes/IDs/schema; it is not a test of downstream Management or real agent disk durability.

The saved-gallery audit passed for95 sampled packages,85 JPEGs and81 vectors. Confirmed dimensions: face512, body384, gait4096, scene768; tested vectors were normalized. This is a sample, not recognition accuracy or an audit of every image.

Remaining:19 fixed-input budget profiles at each5/8/10FPS (scene on every camera), selected ten-minute pre-soak and one-hour stress if a pre-soak passes, final covered-case mapping/report and restoration of host power settings. Completed current-campaign budget galleries are copied to compute-a and SHA256-verified before their remote copies are released; receipts preserve the evidence location. All other remote evidence remains.

This is a live progress document. The authoritative current state is remote STATUS.json and run-ledger.json, not the earlier short-results table.


**Remote contact lost around21:32UTC.** Last verified21:31snapshot:87recorded native runs,23budget runs, nextSX6×8running. SSH/ping cannot reach.64;localgatewayworks. Furtherexecutionandpower/processcleanupunverified. Userphysical/networkcheckrequested. Seeconnectivity-loss.jsonandhandoffcheckpoint7. Testsarenotcomplete;donotreadoldtableasproductionqualification.


## Recovery update — 2026-11-06T04:48:08.395321+00:00

`.64`isreachableafterrestart.Userconfirmedthehostfroze.Testsremainstopped;originalpowerresetvaluesverifiedafterreboot.The778-fileinterruptedcaptureisverifiedlocally;fullremainingarchivecopyisinprogressonaresumableuserservice.Theoldcontact-lost/cleanup-unverifiedparagraphsdescribetheearliercheckpoint.See[thecurrentfreeze review](REMOTE64_HOST_FREEZE_REVIEW.md)forevidenceandlimits.


### Recovery completion — 2026-11-06

The full 5,401,323,520-byte evidence archive completed copying and verification at 05:40:58 UTC (11:10:58 IST). All 34,557 manifest files passed, with zero failures. SHA-256: `b43a986c385a6937c16aad666634c53cd85d6435c5ceee88a3d0be4edda3b0d5`. The separate interrupted capture had already passed all 778 file checks. These checks verify surviving post-freeze bytes; damaged or unflushed original records remain explicitly incomplete.

The one-time local copy service finished successfully and is inactive. Only the redundant temporary remote TAR was removed after verification. The original remote campaign tree and local verified archive remain. The original image, models, gates and contract were unchanged.

Data recovery is complete. The remaining inference tests are still incomplete following the confirmed host freeze; no full test sweep was restarted. Before new isolated tests, account for the separate `sentry-cv.service` workload that auto-started after reboot.

Local verification: `optimization_evidence/2026-11-05-remote64-tlsfix2/recovery/full-evidence-verification.json`. Archive: `remote-root-after-freeze.tar` in the same directory.
