# Why V8 dev3 worked and the optimized image rejects camera04

Direct comparison of baked images, not a claim based only on source HEAD. Old dev3 ID8c46f1a0f809cbd4bac4ed6edf39d764cf12854a86ad562c07da23659b6a6f7d; new QAFT IDd035cb44df82a66cbcdaa29e3ca22177f7bfdbf080009d6a27bf6633e5eb2127.

## Confirmed routing change

Dev3 sets SENTINEL_SURFACE_MOTION_GATE_ENABLED=false, has no required-DLStreamer flags. Baked stream_fleet_openvino.py routes by default to FfmpegDecoder. decode._network_input_options adds RTSP TCP and timeouts without tls_verify. It still uses VAAPI hardware decode. Even if native source were enabled, old code can fall back to FFmpeg on source failure.

QAFT sets SURFACE_MOTION_GATE_ENABLED=true and DLSTREAMER_ENABLED/REQUIRED=true. It uses GStreamer rtspsrc protocols=tcp latency100 and default TLS validate-all; native source failure is raised without FFmpeg fallback. This change came from the DLStreamer optimization migration, not from changing the detector weights.

## Controlled reproduction

Created an ephemeral localhost TLS server with self-signed untrusted certificate, correct localhost IP SAN, deliberately returning RTSP404 to requests. Both image FFmpeg clients completed TLS and sent RTSP OPTIONS (404 is intentional and does not claim media success). Both image GStreamer clients rejected UNKNOWN_CA=1 and emitted the same gst-resource-error-quark code7 / retrieve_sdp / Failed to connect(Generic error). This independently reproduces the effective client difference. No verification disabling was applied to production or probe GStreamer. No media decoding/models were run. The ephemeral private key was deleted.

Both images contain working GnuTLS and glib-networking. GStreamer version changed1.24.2→1.28.2, but both GStreamer versions rejected the untrusted synthetic certificate; missingTLSsupport or a newly introduced validation default is not the demonstrated mechanism. CA bundles also differ (121→120 roots;5removed4added). Camera04's actual chain has not been inspected locally, so no specific removedCA is attributed to the failure.

The controlled test also found that passing -tls_verify1 to FFprobe's RTSP URL still accepted the untrusted synthetic server with these builds. Therefore earlier successful explicit-option probes must NOT be interpreted as independent proof of actual RTSP certificate verification. The GStreamer certificate-error callback is the useful trust evidence here.

Management's camera04 evidence UNKNOWN_CA1, together with the default routing change and reproduction, explains why a previously working source can be rejected after upgrade without any camera change. Old deployment environment overrides have not been obtained, so baked defaults rather than an independently inspected old production process are the scope of comparison. Cameras05/08/12 require their own certificate checks. Camera15's401 remains separate.

## Status

Previous explanation was incomplete in treating this only as site trust provisioning without first comparing dev3. There is a real connection-compatibility change in our optimization migration. Preserve VAAPI/DLStreamer/model gates; implement explicit site CA trust provisioning and meaningful TLS diagnostics as the targeted repair. No runtime source change, trust bypass, rebuild, deployment change or push was performed by this investigation. Source/image inventories and exact controlled outputs are saved alongside this document.

Fresh paired live traffic1 check completed: both dev3 and QAFT GStreamer received video RTP with default certificate validation, no certificate rejection or bus error (1.051s and1.071s). Thus this public-trust source works in both clients/images; controlled untrusted-certificate test explains why it did not expose the office04 failure. Probes did not decode video or load models; all containers exited.
