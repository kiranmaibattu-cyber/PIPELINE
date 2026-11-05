# ROI conversion qualification — not production integration

### ROI experiment results and promotion decision

Three sequential hardware probes ran on unchanged local opt67. Source: Usecase-Catalog-main/videos/ANPR.mp4, 1280x720 H264. No other host programs stopped. Script retained each source VA sample and checked PTS/output geometry; five rectangles included odd origins, edges,16x16 and full frame. VAMemory caps at postprocessor input are verified, not proof of absence of internal driver copies.

| Probe | Frames / conversions | Result |
|---|---:|---|
| One dynamically reconfigured crop converter |20 /100| Pixel parity failed: maximum channel difference249/255; even full-frame requests differed by up to25 |
| Separate fixed converter per rectangle, repeated full-reference control |10 /50| Full-reference repeat and fixed full-frame crop were pixel-exact; smaller crop differences remained, maximum40/255 |
| Fixed converters plus explicit source colorimetry inferred from VideoInfo |10 /50| Still failed; maximum40/255. Not resolved by explicit colour metadata |

PTS/shape checks passed in all completed probes. Differences are not evidence of a changed model or production gate: this is a standalone conversion experiment. Root cause is unresolved. Dynamic crop renegotiation is implicated by the differing dynamic/full-frame versus fixed/full-frame result, but not proven as the sole cause; fixed crops still differ. Do not assert chroma alignment or colourimetry alone explains it. Halo/even alignment did not establish equivalence.

Performance is not a proven win: original dynamic median small-ROI conversions were ~4.3–5.2ms, but one corner path had ~486ms outliers. Fixed/control runs also had outliers and widely varying timings. Repeated controls and multiple converter graphs add work, and host is shared. Do not extrapolate these numbers to capacity. Several ROI calls can cost more than one full conversion. No new model accuracy evaluation was attempted: pixel equivalence protects existing gate inputs.

**No runtime ROI integration, new image, commit or publication was performed. Opt67 remains the unchanged candidate.** The new script is qualification tooling only. Artifacts are archived under optimization_evidence/2026-11-04-roi-conversion/ (not only /tmp).

Reproduction: mount scripts/v8_roi_conversion_probe.py as /probe.py, ANPR.mp4 as /fixture.mp4, writable output directory as /results into opt67; run with --network=none --device /dev/dri --group-add keep-groups --entrypoint python. Command inside container: /probe.py /fixture.mp4 --output /results/roi.json (20 frames); fixed control adds --frames 10 --fixed-converters; colour control additionally adds --explicit-color. All probe containers exited and were removed.

### Next bounded optimization steps (do not restart the architecture)

1. Keep existing production full-frame converter as reference. Resolve/replace the failed VA region converter before rollout. A possible alternative is full-surface VA colour conversion with region-only CPU mapping/copy; that saves CPU transfer/copy only if measured, and must not be called region-only GPU conversion. Neither alternative is implemented yet.
2. Introduce explicit frame/region access only after parity: crop requests retain camera/session/frame ID, PTS, source geometry and expanded face evidence bounds. One full-frame materialization must be reused if requested, rather than converted again for each consumer. Retain the source surface until all bounded consumers finish. Avoid passing a lazy Python proxy to OpenCV or fake full-frame pixels to quality gates.
3. Preserve scheduled full-frame adaptive quality sampling, scene sampling and any ANPR/zone/line/hazard evidence requirements. Audit ANPR full-frame candidate snapshots before claiming broad ROI savings. Multiple/overlapping region requests need measured crossover to a shared full frame; do not lower quality gates or emission cadence to achieve speed.
4. Remaining independent bottleneck: synchronous shared-secondary waits can block camera policy. Any future asynchronous work must preserve per-camera ordering, session invalidation, timestamps and candidate associations; no new unbounded queue or silent required-output drop. Profile before restructuring.
5. After each accepted change, repeat same-input output/pixel checks and short busy multicamera throughput/freshness measurements against opt67; then increase duration/cameras. Do not call missing qualification a proved failure or an experimental path integrated.
