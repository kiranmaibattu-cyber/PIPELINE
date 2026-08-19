# Building a Solution Pack image for ApexFabric

This is a from-scratch guide for building a CV pipeline (or other application)
image that will run correctly on the ApexFabric platform. You don't need to
know Kubernetes internals — just what to build, what to expose, and where
your inputs/config come from.

## 1. What ApexFabric is, briefly
ApexFabric is a platform that runs your container on one or more "compute
boxes" (Intel or NVIDIA hardware) and connects it to cameras, a management
UI, and monitoring — all on a private local network with **no internet
access**. Your image gets built, pushed to our private registry, and then
started by our scheduler on whichever qualified box has room for it. You
never manually place it on a specific machine — the platform decides that.

## 2. What you're responsible for building
A Docker image containing your application, plus a short spec (we'll give you
a template) declaring what your app needs — cameras, resources, ports,
storage. You are not responsible for Kubernetes YAML, cluster setup, or
scheduling logic — that's ours. You are responsible for making your
container behave correctly once it's running.

## 3. Build requirements

- **Any vendor SDK/framework you build against (e.g. a hardware runtime) is a
  build-time dependency only.** Your final image must be able to run with
  nothing else installed on the machine beyond what's in your image — no
  installers, credentials, or vendor registries reachable at deploy time.
  Use multi-stage builds: pull the vendor base image in an early build stage,
  and make sure everything the app needs at runtime is copied/compiled into
  your final stage.
- **Never depend on a mutable tag (`:latest`) for a vendor base image.**
  Pin an exact version so your build is reproducible.
- **No internet access at runtime, ever.** Nothing in your app should try to
  phone out — no update checks, no telemetry calls, no external API calls
  unless explicitly required and pre-approved. If a library you depend on
  does this by default, disable it.
- **Target one specific hardware profile per image.** We currently support
  `intel-285h` and `jetson-orin` — these have different drivers/runtimes and
  are not interchangeable. If you need to support both, build two separate
  images; don't try to make one image detect and adapt at runtime.
- **Kernel-level drivers are not your concern.** If your pipeline uses a
  physical accelerator, assume the host already has the driver installed —
  your image only needs the userspace libraries/bindings, and device access
  will be granted to your container by us (via device paths or a device
  plugin), not something you configure.

## 4. Where camera information comes from
**You do not hardcode cameras.** Camera assignment (which streams your
instance handles, and their connection info) is provided to your container
at deploy time by the platform
- **Platform-managed**: we hand you a list of camera IDs/config, likely as a
  mounted file and/or Secret

- **Never store camera credentials (RTSP passwords, etc.) in plaintext on
  disk.** Read them from a mounted Secret or environment variable we
  provide, or encrypt anything you persist yourself.
- Your app must be able to start and become healthy with **zero cameras
  configured** — don't fail startup just because no camera is present yet
  (this matters if an operator configures cameras after the pod is already
  running).

## 5. Required endpoints
Your container must expose HTTP endpoints for the platform to monitor and
integrate with it. At minimum:

| Purpose | What it must do |
|---|---|
| **Liveness** (e.g. `/healthz`) | Returns healthy if the process itself is alive — must NOT depend on camera/analytics state. A stalled camera should not make this fail. |
| **Readiness** (e.g. `/readyz`) | Returns healthy only when your app can currently do useful work. This can reflect camera/pipeline state. |
| **Startup** (can reuse readiness if init is fast) | Only needed separately if your app has a slow init (model load, device init) that shouldn't be mistaken for a hang. |
| **Metrics** | Either Prometheus-format (if you want it scraped automatically) or a JSON metrics endpoint (if it's just operational data an API/UI will query) — pick one and tell us which, don't mix formats on one endpoint. |
| **Analytics/events** | If your app emits ongoing events (detections, alerts, etc.), expose them as a WebSocket (or declare another protocol explicitly) so the central UI can subscribe in real time. |

**All of these must bind to `0.0.0.0` inside the container, not
`127.0.0.1`/localhost.** Binding to localhost is the most common reason an
otherwise-correct app can't be reached once deployed — it works in local
testing and silently fails in the cluster.

## 6. Storage
Anything your app writes to disk needs to be under a mounted volume path we
give you — not somewhere inside the image filesystem, which doesn't
persist. Separate what you write into at least two categories, on separate
paths if possible:
- **Durable config** (must survive restarts/upgrades) — e.g. saved settings,
  zone definitions.
- **Cache/reconstructable data** (logs, snapshots, temp files) — lower
  priority, doesn't need the same backup guarantees.

Tell us roughly how much storage each category needs and how fast it grows,
so we can size volumes correctly.

## 7. Resource requests — how scheduling works
Every pack declares its expected resource usage (CPU, memory, and how many
camera streams it will process) so the platform can decide which box has
room for it. **Tell us the maximum number of camera streams a single
instance of your app could ever be configured to handle** — this is used to
reserve capacity on the box, so it needs to reflect the ceiling, not just
whatever's configured on day one.

## 8. Lifecycle behavior
- **Handle `SIGTERM` gracefully.** When the platform stops your app (or
  during an upgrade), it sends `SIGTERM` and gives you a grace period —
  flush any in-progress state and exit cleanly rather than assuming an
  immediate kill.
- **"Stopping" your app means scaling it to zero, not deleting anything.**
  Your persistent data/config survives a stop, and restarting must resume
  cleanly from that same data.
- If you run any internal service (e.g. an in-container cache/queue) that
  isn't backed by the mounted volume, understand that its contents will be
  lost on every restart — that's fine for purely transient/live data, but
  flag it to us if you're treating any of it as durable history, since it
  isn't.

## 9. Security
- No secrets (credentials, keys, tokens) baked into the image or written in
  plain config files — Secret references only.
- Request the minimum device/host access your app actually needs. If you
  think you need privileged/full-device access, tell us specifically what
  hardware you're accessing — we likely have a narrower way to grant it.

## 10. What to send back to us
When your image is ready (or if anything above doesn't fit your app's
design), tell us:
- Which hardware profile(s) it targets
- Camera model: platform-managed or self-managed, and max streams per
  instance
- Your endpoint paths for liveness, readiness, metrics, and analytics/events
  (and their formats/protocols)
- Storage paths and rough size/growth per category
- Any external network calls your dependencies make, even if disabled by
  default
- Any device/privileged access requirements

We'll turn that into the deployment bundle (resource requests, volumes,
probes, Service definitions) on our end — you don't need to write any of
that yourself.
