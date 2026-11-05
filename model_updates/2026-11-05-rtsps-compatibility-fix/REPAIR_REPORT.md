# V8 RTSPS compatibility repair — 2026-11-05

**The repaired image restores dev3-compatible RTSPS by default, as explicitly requested by the user.** It keeps the current native DL Streamer/VAAPI pipeline and offers strict certificate validation with optional deployment CA files.

Image: `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.05-v8-qaft-int8-tlsfix2`.

Configuration ID: `dbaeacc92973fc5d1e41521e3200cd64151aaa53a472404a79f87833bade3ef5`.

Registry digest: `sha256:b5a5dc68db4cba9e2b18ec28c61175686189161b25f605b298c1241dbc4957fc`.

## Root cause and scope

The earlier dev3 image defaults to FFmpeg VAAPI, without certificate verification enabled in its RTSP command. The optimized image uses required GStreamer/DL Streamer, whose RTSPS source validates certificates. A controlled untrusted-certificate server reproduced FFmpeg reaching RTSP while GStreamer rejected `UNKNOWN_CA=1` with the same generic SDP setup error reported by Management. Camera04's in-pod certificate rejection was supplied by Management. Causes for05/08/12 were not independently established; camera15 returned401 and remains an authentication issue.

The first repair candidate (`tlsfix1`, local only) retained strict defaults and added deployment CA loading. The user then explained that site CAs may not be available and explicitly chose legacy-compatible behavior by default. `tlsfix2` implements that final requirement; the strict-only intermediate image and its tests are preserved as historical evidence and were not published as this repair.

## Exact changes

- Added `source_tls.py`: explicit legacy-compatible/strict modes, optional mounted public site CA loading alongside system roots, fail-closed configuration errors and camera/session-specific sanitized TLS diagnostics.
- Changed `surface_stream.py`: named RTSP source, trust configuration before source start, preserved probe stderr/authentication details and source-specific startup/reconnect diagnostics. Real received frames clear stale TLS error state.
- Changed `multi_camera_gst.py`: optional pre-start branch configuration hook. Trust/callbacks are installed before a bin joins an already running parent graph. A failed initial configuration releases startup ownership so peers can start.
- Baked `SENTINEL_CAMERA_TLS_MODE=legacy-compatible` and optional default CA directory into the overlay image. The general Dockerfile also explicitly requires glib-networking for future builds.

The image overlay changes exactly two existing runtime files and adds one. All33 baked model files and all contract/policy files matched the previous published candidate byte-for-byte. The only new environment settings concern TLS. The Opt80 experiment is not added. Models, hardware decode, tracker, motion rules, inference cadence, production quality gates and V8 package formats are unchanged by this overlay; see `image-change-verification.json`.

## Tests

- **96 tests passed, one Opt80-only case deselected.** The Opt80-only test module is excluded because that experimental implementation is intentionally absent. Test dependencies and external test tools were mounted read-only; the runtime modules came from the actual repaired image.
- **15 native TLS scenarios passed:** default compatibility on an untrusted certificate; default compatibility with hostname mismatch and expiry; compatible shared branch; strict unknown-CA rejection; mounted CA success; explicit CA-file success; strict hostname/expiry rejection; malformed PEM; shared trusted/untrusted branches with peer progress; invalid first-branch configuration without blocking a peer; separate RTSP401; and public RTSPS reception with an extra site CA installed.
- All trust callbacks were configured while the source was stillNULL. Strict cases retained validation flags127; compatibility cases used0, as authorized. The synthetic server advertises normal RTSP methods and deliberately returns404 to DESCRIBE: those cases prove TLS/RTSP progression, not video decoding. A separate actual-video test supplies the end-to-end execution evidence.
- **Live untrusted-TLS relay:** relayed the existing traffic1 camera through an ephemeral self-signed local RTSPS listener. The upstream TLS connection remained verified. Strict mode rejected the listener with unknown-CA1; legacy mode received video RTP. No CA was mounted for the compatibility run.
- **Complete native pipeline through that relay:** one camera requested4FPS, all use cases bound,15-second warmup and30-second steady hold. Final state healthy, no fatal native lines. The compatible agent API accepted681 packages containing347 evidence images and37 embeddings (33scene/4body), including presence/count/crossing/plate outputs. Face/gait positives are not claimed from this particular scene.
- The saved gallery subset audited52 packages,42 JPEGs and35 vectors, with zero hash/size/dimension/normalization/parent association errors. The receiver validates all accepted package schemas and multipart bytes; the gallery is a subset.

At one sampled endpoint662 packages were admitted/acknowledged, with5 backpressure counts and zero exhausted retries/protocol errors; final capture681 extends to a later cutoff. Those totals must not be subtracted to infer loss. Existing admission-state guarantees were not repaired here.

The original capacity harness labels the short native run as a capacity pass, but this task claims **connection and output execution only**. Short measured selection exceeded the requested4FPS; the inherited PTS/catch-up/clock issue is not resolved. The live video is unmarked, so independent source-to-API latency is not established. No new maximum-camera, accuracy, sustained-resource or production-readiness claim is made.

Ephemeral private test keys were removed. `ConnectionResetError` in the relay summary includes the intentional rejected/closed probe connections; the native run had no fatal source error. Raw artifacts are retained locally and included in the repair evidence archive. Management's actual office cameras were not accessed or redeployed.

## Test setup corrections retained

The first mock server returned404 to OPTIONS, then omitted SETUP from Public; both stopped RTSP before the expected DESCRIBE assertion despite a successful trusted handshake. The final fixture advertises normal methods and returns404 to DESCRIBE. Those two failed fixture attempts are preserved. The first regression launch mounted tests at the wrong root and included an Opt80-only case; the second omitted the external measurement-tool mount. Corrected layouts produced the final96-pass result. These were test setup errors, not runtime repairs or suppressed failures.

## Deployment

Use the new image tag/digest; the previous candidate tag was not overwritten. No CA certificate is needed for the default mode selected by the user. Server identity is not verified in that mode. Strict deployment settings and remaining limitations are documented in [DEPLOYMENT.md](DEPLOYMENT.md). All tests are stopped. No Management deployment was changed.
