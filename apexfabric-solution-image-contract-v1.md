# ApexFabric Solution Image Contract — V1

This contract defines what a solution developer must provide for a containerized CV pipeline.

**V1 assumptions**

- Models are baked into the container image.
- Persistent volumes are not used.
- Kubernetes owns scheduling and lifecycle.
- Configuration and camera credentials are injected through Kubernetes objects.
- Analytics are exposed over HTTP.
- The image must not require a separate management server.

---

## 1. Image delivery

The developer must provide:

- A Linux container image or `docker save` archive.
- An immutable version tag, not only `latest`.
- Supported architecture:
  - `linux/amd64` for Intel 285H
  - `linux/arm64` for Jetson Orin
  - Preferably a multi-architecture image
- Image digest or archive SHA-256.
- A list of required CPU, memory, camera streams, and accelerators.
- A sample desired-state file with fake credentials.

Example tag:

```text
traffic-edge-runtime:intel-285h-2026.08.19
```

The image must include all runtime code, models, and model metadata needed to start.

---

## 2. Runtime behavior

The image must:

- Start without an interactive terminal.
- Run continuously in the foreground.
- Bind its HTTP server to `0.0.0.0`, not `127.0.0.1`.
- Exit nonzero when startup fails irrecoverably.
- Handle `SIGTERM` and stop gracefully.
- Not invoke Docker, Kubernetes, `kubectl`, or systemd.
- Not attempt to create Pods, Services, Secrets, or volumes.
- Not require privileged mode unless explicitly agreed.
- Not require an undeclared local or external service.

The image must **not** assume services such as:

```text
localhost:6379
management-server:9000
host.docker.internal
```

If a dependency is required, it must be declared to ApexFabric before delivery.

---

## 3. Configuration input

ApexFabric mounts the desired-state document at:

```text
/configs/desired_state.json
```

The runtime must read that file at startup.

Example:

```json
{
  "edge_id": "intel-box-01",
  "revision": 1,
  "cameras": [
    {
      "camera_id": "cam4",
      "source": "file:/run/secrets/apexfabric/cam4.rtsp",
      "solution_pack": "traffic",
      "fps": 8,
      "apps": ["anpr", "wrong_way", "vehicle_counting"]
    }
  ]
}
```

The developer must provide a JSON Schema for the desired-state format.

Unknown or invalid configuration must produce:

- A clear log message
- A nonzero startup result
- An unhealthy readiness endpoint

The application must **not** silently substitute default cameras or credentials when required configuration is missing.

---

## 4. Kubernetes Secret contract

Sensitive values are mounted as files under:

```text
/run/secrets/apexfabric/
```

Example:

```text
/run/secrets/apexfabric/cam4.rtsp
/run/secrets/apexfabric/cam5.rtsp
```

Each file contains one value:

```text
rtsp://username:password@camera-address:8554/stream
```

The desired-state document references the mounted file:

```json
{
  "camera_id": "cam4",
  "source": "file:/run/secrets/apexfabric/cam4.rtsp"
}
```

The image must:

- Read Secret values from the referenced files.
- Support a different file for every camera.
- Treat missing or unreadable files as configuration errors.
- Trim an optional trailing newline.
- Never print the complete RTSP URL.
- Never print usernames, passwords, tokens, or Secret contents.
- Never copy Secret values into analytics events.
- Never store Secret values in generated files.
- Re-read the Secret on restart.

**Safe logging:**
```text
Camera cam4 source loaded from mounted Secret
```

**Unsafe logging:**
```text
Connecting to rtsp://admin:password@192.168.1.95/stream
```

Non-sensitive settings may be passed through environment variables, but credentials must use mounted Secret files.

---

## 5. Plan compilation

For the current ApexFabric implementation, the same image must support this command:

```bash
python -m edge_runtime.agent.edge_agent \
  --desired-state /configs/desired_state.json \
  --output-dir /plans \
  --models-root /models
```

It must:

- Validate desired state.
- Resolve Secret file references.
- Validate that baked-in models are available.
- Generate `/plans/traffic.runtime_plan.json`.
- Exit `0` only when the plan is valid.
- Exit nonzero with a useful error when compilation fails.
- Never include resolved Secret values in the generated plan.

`/plans` is temporary storage shared between the init container and runtime container. It is recreated whenever the Pod is replaced.

The main runtime must consume the generated plan without contacting a separate management service.

---

## 6. Baked-in model layout

Models must be available at an agreed, immutable path in the image, for example:

```text
/models/traffic/openvino/
├── vehicle.xml
├── vehicle.bin
├── license_plate.xml
├── license_plate.bin
├── ocr.xml
└── ocr.bin
```

The developer must provide:

- Exact model paths
- Model versions
- SHA-256 digests
- Runtime/model compatibility information
- Estimated image size
- Required accelerator

The image must fail clearly if a required baked-in model is missing or incompatible.

---

## 7. HTTP API

The runtime must listen on `0.0.0.0:8080` and expose these endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Process and runtime health |
| `GET /readyz` | Ability to process configured workloads |
| `GET /metrics` | Operational and pipeline metrics |
| `GET /events` | Streaming analytics events |

These endpoints must not require access to camera credentials.

---

## 8. Health endpoint

`GET /healthz` indicates whether the process and its internal workers are alive.

Successful response:

```http
HTTP/1.1 200 OK
Content-Type: application/json
```
```json
{
  "status": "ok",
  "child_running": true,
  "plan_loaded": true
}
```

Return HTTP `500` when the runtime is irrecoverably unhealthy.

Temporary camera disconnection should normally not crash the process. It should be represented in metrics and status.

---

## 9. Readiness endpoint

`GET /readyz` indicates whether Kubernetes should send traffic to the Pod.

Successful response:

```http
HTTP/1.1 200 OK
Content-Type: application/json
```
```json
{
  "ready": true,
  "configured_cameras": 2
}
```

Return HTTP `503` when:

- Configuration is invalid.
- The runtime plan cannot be loaded.
- Required models are unavailable.
- Internal workers failed to start.
- The analytics endpoint cannot operate.

The solution developer and ApexFabric must agree whether camera connectivity is required for readiness. The default is that temporary camera loss does **not** make the entire Pod unready.

---

## 10. Metrics endpoint

`GET /metrics` must expose operational metrics using either:

- Prometheus text format (preferred), or
- A documented JSON schema for the prototype.

Recommended Prometheus metrics:

```text
apexfabric_pipeline_up
apexfabric_camera_connected{camera_id="cam4"}
apexfabric_frames_processed_total{camera_id="cam4"}
apexfabric_frames_dropped_total{camera_id="cam4"}
apexfabric_analytics_events_total{camera_id="cam4",event_type="plate_read"}
apexfabric_inference_duration_seconds
apexfabric_pipeline_errors_total
```

Metrics must **not** include:

- RTSP URLs
- Passwords
- Tokens
- Personally identifying values such as recognized faces or license plates

Analytics values belong in the event stream, not metric labels.

---

## 11. Analytics event endpoint

`GET /events` must expose Server-Sent Events on port `8080`.

Required response headers:

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
```

Each event must contain one JSON object:

```text
event: analytics
data: {"schema_version":"1.0","event_id":"...","timestamp":"2026-08-19T12:00:00Z","camera_id":"cam4","solution_pack":"traffic","application":"anpr","event_type":"plate_read","payload":{"plate":"KA52P1295","confidence":0.94}}
```

Minimum event fields:

```json
{
  "schema_version": "1.0",
  "event_id": "unique-event-id",
  "timestamp": "RFC3339 UTC timestamp",
  "camera_id": "cam4",
  "solution_pack": "traffic",
  "application": "anpr",
  "event_type": "plate_read",
  "payload": {}
}
```

Requirements:

- `event_id` must be unique.
- `timestamp` must use UTC.
- `camera_id` must match configured desired state.
- `event_type` must come from a documented list.
- `payload` must follow the event-type schema.
- Events must be valid single-line JSON.
- The connection must remain open.
- Send a heartbeat at least every 30 seconds when no analytics are generated.
- A disconnected HTTP client must not crash or block the pipeline.
- Multiple clients should be able to observe events.
- Slow clients must not block inference.

The server accesses this endpoint through a Kubernetes Service. The image must not call back to the control-plane UI.

---

## 12. Logging

Write logs to stdout and stderr.

Recommended format:

```json
{
  "timestamp": "2026-08-19T12:00:00Z",
  "level": "INFO",
  "component": "traffic-runtime",
  "camera_id": "cam4",
  "message": "Camera stream connected"
}
```

Requirements:

- Do not log Secret values.
- Do not dump the environment.
- Do not log full desired-state documents when they might contain sensitive data.
- Include camera ID and component for operational failures.
- Use stable, understandable error messages.
- Avoid unbounded log volume for repeated camera failures.

---

## 13. Network behavior

The container **may**:

- Connect outbound to configured RTSP camera addresses.
- Serve health, metrics, and events on port `8080`.

It must **not** require:

- Direct access to the Kubernetes API
- Direct access to the K3s server
- Direct access to the UI
- Direct access to the container registry after startup
- A separate management server

The container must tolerate the analytics client disconnecting and reconnecting.

---

## 14. Security and process requirements

Preferred runtime requirements:

- Run as a non-root user.
- Use a read-only root filesystem where practical.
- Write only to declared temporary paths such as `/tmp` and `/plans`.
- Do not embed production credentials in the image.
- Do not expose debug shells or unauthenticated administrative actions.
- Do not require host networking.
- Do not require host PID or IPC namespaces.
- Do not request more device access than necessary.

Accelerator device requirements must be documented separately.

---

## 15. Developer acceptance test

Before delivering the image, the developer must demonstrate:

```bash
docker run --rm \
  -p 8080:8080 \
  -v ./desired_state.json:/configs/desired_state.json:ro \
  -v ./secrets:/run/secrets/apexfabric:ro \
  IMAGE_REFERENCE
```

These checks must pass:

```bash
curl -f http://127.0.0.1:8080/healthz
curl -f http://127.0.0.1:8080/readyz
curl -f http://127.0.0.1:8080/metrics
curl -N http://127.0.0.1:8080/events
```

Also test:

- Missing desired-state file
- Invalid desired state
- Missing camera Secret
- Invalid RTSP source
- Camera disconnection and reconnection
- Event client disconnect and reconnect
- `SIGTERM` graceful shutdown
- Verification that no Secret value appears in logs
- Correct execution on every declared architecture

---

## 16. Required delivery package

The solution developer must deliver:

```text
solution/
├── image.tar
├── image.sha256
├── desired-state.schema.json
├── desired-state.example.json
├── analytics-event.schema.json
├── image-contract.yaml
└── README.md
```

`image-contract.yaml` should contain:

```yaml
name: traffic-edge-runtime
version: 2026.8.19
architectures: [amd64]
models:
  delivery: baked-in
  root: /models/traffic/openvino
configuration:
  desiredStatePath: /configs/desired_state.json
secrets:
  root: /run/secrets/apexfabric
  requiredKeys:
    - cam4.rtsp
    - cam5.rtsp
runtimePlan:
  compiler: python -m edge_runtime.agent.edge_agent
  output: /plans/traffic.runtime_plan.json
network:
  port: 8080
endpoints:
  health: /healthz
  readiness: /readyz
  metrics: /metrics
  events: /events
eventProtocol: sse
externalDependencies: []
```

With this contract satisfied, ApexFabric only needs to create the desired-state Secret, camera Secret, Deployment, Service, probes, and scheduling requirements. Kubernetes can then schedule the image and ApexFabric can consume its analytics without a separate management server.

---

### Notes on this draft

- **Version string format:** the §1 image tag uses zero-padded date components (`2026.08.19`) while the §16 `image-contract.yaml` example uses `2026.8.19`. Worth aligning if the yaml is a literal template.
- **Architecture coverage:** §1 requires `linux/amd64` and `linux/arm64` (preferably multi-arch), but the §16 `image-contract.yaml` example only lists `architectures: [amd64]`. Confirm whether that's meant as an illustrative single-arch example or should reflect the full multi-arch requirement.
