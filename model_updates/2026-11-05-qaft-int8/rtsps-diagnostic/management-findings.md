# Management RTSPS findings (user-supplied)

Container `cv`, pod `sentinel-cv-blr-office-2-v8-96c47788c-9vfzp`, node `zmd-pilot-1`, reported published QAFT candidate.

- Cameras04/05/08/12: generic rtspsrc SDP connection failure followed by startup retry exhaustion.
- Camera04: in-container GnuTLS backend available, supports_tls true, certificate_errors [1]. Confirmed unknown signing CA in that trust context. This does not prove a particular CA/intermediate is missing or that other certificate errors are absent.
- Cameras05/08/12: equivalent certificate checks outstanding; cause unconfirmed.
- Camera15: separate RTSP401Unauthorized, authentication/access issue.
- Empty inference instance during stop does not independently establish model failure.

This evidence narrows the failure to source connection/authentication before decode/model processing. Local gpu-box traffic1/ch9 successful TLS tests used different source endpoints and do not establish trust for the office cameras.

Next action is to inspect/provision the correct server certificate chain/trust anchors, verify each other camera, and fix camera15 credentials/access. Preserve TLS verification. No production fix/change or rebuild has been applied by this diagnosis.
