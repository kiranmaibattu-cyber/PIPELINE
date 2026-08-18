# PLATF — Person Intelligence Platform

Self-contained platform layer. Everything platform (code, identity memory, caches,
persisted state) lives under this folder. The existing `MTMC/` pipeline stays as-is
and is reused as the shared detection/tracking/embedding **backbone**; PLATF wraps it
with a Person-Object core and a plugin system.

## Principle: TRACK FIRST, THEN ANALYZE
One shared backbone (decode → detect → track → tracklet) feeds a **Person-State
service** (the memory). Use-case **plugins** (re-id, face, intrusion, loitering,
counting, absence, …) subscribe to the person stream and emit **events**. Shared
backbone, separate analyzers — never one giant per-frame pipeline.

## Structure
```
PLATF/
  core/          substrate primitives (no heavy deps)
    observation.py   TrackObservation — the backbone → platform contract (one tracked
                     person, one frame). Optional attached features (app/face/gait).
    person.py        Person object + PersonStore (the memory): per-modality exemplars,
                     camera/zone history, trajectory, lifecycle.
    events.py        Event + EventBus (thin in-proc pub/sub). Plugins emit; UI/store subscribe.
    plugin.py        Plugin base + PluginContext + PluginHost (runs observations through
                     the store, dispatches to plugins in order, collects events).
  plugins/       use-case analyzers (each implements Plugin). reid first + hardest.
  galleries/     identity memory persisted to disk (appearance / face / gait ANN indices).
  cache/         runtime caches (gait buffers, face cache, event snapshots/clips).
  data/          persisted Person state + event DB.
  config/        platform.yaml (cameras, zones, plugin config, thresholds).
  tests/         substrate + plugin tests.
```

## Build sequence (see plan)
Phase A substrate (this) → Phase B Re-ID plugin stabilization → Phase C cheap analytics
plugins → Phase D spatial/BEV map → Phase E scale. Re-ID gates on `MTMC/honest_metric.py`.
