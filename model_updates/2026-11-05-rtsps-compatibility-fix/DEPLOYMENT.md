# RTSPS compatibility repair

Deploy the `cv` container using:

```text
ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.05-v8-qaft-int8-tlsfix2
```

Registry digest:

```text
sha256:b5a5dc68db4cba9e2b18ec28c61175686189161b25f605b298c1241dbc4957fc
```

The previous `...v8-qaft-int8-candidate` tag is unchanged. Management must deploy the new tag or digest to receive this repair. The V8 desired state and camera URL Secret format are unchanged. No deployment was modified by the image-authoring task.

## Default: match dev3

The user explicitly selected **"Legacy-compatible by default, like dev3"** after being informed of the certificate-verification tradeoff. The new image bakes in:

```text
SENTINEL_CAMERA_TLS_MODE=legacy-compatible
```

RTSPS remains encrypted. Server certificate validation is disabled, matching the effective behavior of the earlier default FFmpeg path. A site CA is not required in this mode. The runtime still uses native GStreamer/DL Streamer and VAAPI; it does not switch to FFmpeg or CPU decoding. RTSP/TCP input configuration and analytics gates are unchanged.

Each RTSPS source logs `camera_tls_configured` with camera/session IDs, effective mode and `certificate_verification:false`. This makes the selected connection behavior visible. Compatibility mode does not authenticate the camera/server identity and can accept self-signed, expired or mismatched certificates, as verified by the tests.

Camera15's separately reported `401 Unauthorized` requires correct camera credentials/access in its Secret; certificate compatibility cannot grant authentication access. Cameras04/05/08/12 should be checked after rollout. Direct access to those office sources was not available for this task; the repair was tested against the reproduced trust failure and a complete live pipeline through an untrusted TLS relay.

## Optional strict validation

For deployments that require authenticated server certificates, set:

```yaml
env:
  - name: SENTINEL_CAMERA_TLS_MODE
    value: strict
```

Publicly trusted camera certificates work with the existing system CA store. If a site uses its own CA, optionally mount its public PEM `.crt`/`.pem` files read-only under `/run/secrets/sentinel/camera-ca/`. The CA directory is optional; public roots are preserved when extra site CAs are loaded. Supply CA certificates, not private keys.

Example volume configuration, only for strict mode with a site CA:

```yaml
# Under the existing cv container:
env:
  - name: SENTINEL_CAMERA_TLS_MODE
    value: strict
volumeMounts:
  - name: camera-ca
    mountPath: /run/secrets/sentinel/camera-ca
    readOnly: true

# Under pod spec:
volumes:
  - name: camera-ca
    configMap:
      name: sentinel-camera-ca
```

Alternative paths can be supplied using `SENTINEL_CAMERA_CA_DIR` or `SENTINEL_CAMERA_CA_FILE`. These are deployment environment settings, not new desired-state/contract fields. Missing explicit files/directories, malformed PEM and private-key material fail configuration; unknown TLS mode values also fail. Restart the deployment after changing trust material so retained camera branches reload it.

Strict-mode failures now identify `tls_unknown_ca`, `tls_bad_identity`, `tls_expired`, or a TLS configuration error in sanitized decoder diagnostics. Authentication failures remain separate. The final startup exception retains the source failure reason instead of hiding it behind only `UnsupportedSurfaceSource`.

## Qualification boundary

This repairs the RTSPS connection compatibility regression. It does not resolve the previous QAFT numerical-fidelity, absolute timestamp, memory/state-retention, admission or capacity findings. See [repair test report](REPAIR_REPORT.md) and the previous [QAFT validation](../2026-11-05-qaft-int8/VALIDATION_REPORT.md).
