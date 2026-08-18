# PLATF Build Log

Running record of what was built, and every issue hit + how it was solved. Newest
phase at the bottom. Keep this current as the platform grows.

---

## Phase A — Platform substrate (in progress)
Goal: thin foundation so every use-case (re-id, face, intrusion, loitering,
counting, absence) is a clean plugin on a shared Person-Object + event bus. Does NOT
touch the working `MTMC/` pipeline yet.

### Files created
| file | purpose |
|---|---|
| `PLATF/README.md` | structure + track-first principle |
| `PLATF/__init__.py` | package (v0.1.0) |
| `PLATF/core/observation.py` | `TrackObservation` — backbone→platform contract (one tracked person/frame; optional app/face/gait features; foot-point for zones/BEV) |
| `PLATF/core/person.py` | `Person` object + `PersonStore` (the memory): per-modality capped exemplars, camera/zone history, trajectory, (camera,local_id)→gid binding, mint/merge/prune |
| `PLATF/core/events.py` | `Event` + `EventBus` (thin thread-safe in-proc pub/sub, isolated subscribers, history ring) |
| `PLATF/core/plugin.py` | `Plugin` ABC + `PluginContext` + `PluginHost` (runs observations through store, dispatches to plugins in order — identity first) |
| `PLATF/core/__init__.py` | exports the substrate |
| `PLATF/plugins/__init__.py` | plugin package (empty; plugins come Phase B+) |
| `PLATF/config/platform.yaml` | store + plugin + paths config |
| `PLATF/tests/test_substrate.py` | end-to-end: observations→store+plugins→events |
| `PLATF/galleries|cache|data/.gitkeep` | storage dirs (identity memory, caches, state) |

### Issues + fixes
1. **Exemplar cap bypassed (test caught it).** `test_substrate` failed: cap=3 got 5.
   Cause: `Person.add_exemplar` has its own default `cap=10`; the plugin called it
   without passing the store's configured cap, so the store's `exemplar_cap=3` was
   ignored. **Fix:** added `PersonStore.add_exemplar(person, ...)` that applies
   `self.exemplar_cap` — the single place the per-modality memory-size policy lives,
   so a plugin can't forget it. Plugins now call `ctx.store.add_exemplar(...)`.
   Result: test green (`persons=1 events(identity=2,count=1) exemplars_kept=3 merge=ok`).

### Verify
`python -m PLATF.tests.test_substrate` → prints `OK ...`.

### Notes / decisions
- numpy is available in the local Windows python (test runs locally, no box needed).
- The identity (re-id) plugin runs FIRST in the host so it binds (camera,local_id)→
  Person before analytics plugins see the observation; the host re-resolves the
  binding before each plugin and `touch()`es the Person once per observation.

---

## Phase B step 1 — Re-ID as the first plugin (identity logic on the substrate)
Goal: make re-id a real `Plugin` on the substrate, reusing the proven matching engine
(the `MultiModalGallery`) behind a small gallery protocol so the identity LOGIC
(sticky-id, same-frame guard, exemplar memory, identity events) lives on the platform.
Live `MTMC/streamapp_2tier` still untouched.

### Files created
| file | purpose |
|---|---|
| `PLATF/plugins/reid.py` | `ReIDPlugin` — event-triggered identity: sticky-id (a bound track keeps its gid, only reinforces), same-frame guard (excludes co-visible gids), exemplar memory, emits `identity` events (new / cross_cam) |
| `PLATF/plugins/reid_gallery.py` | `FakeGallery` (cosine-NN test double) + `MMGalleryAdapter` (wraps real `MultiModalGallery`, mirrors two-tier `RealReID` match/reinforce) behind one protocol |
| `PLATF/tests/test_reid_plugin.py` | logic test on `FakeGallery`: same-frame guard, sticky-id, cross-cam match |
| `PersonStore.get_or_create(gid,t)` (in `core/person.py`) | aligns Person ids to the gallery's gid space (gallery owns the id space) |

### Issues + fixes
2. **Person camera-history not updated on a cross-cam first-sight (test caught it).**
   `test_reid_plugin` failed `multi_camera` then `cross_cam`. Cause: the host `touch()`ed
   the Person **before each plugin**, but on the frame re-id first *binds* a new camera
   the host saw `person=None` (pre-bind), so the new camera never entered `cameras_seen`
   and the cross-cam link was invisible. **Fix (a):** host now `touch()`es the Person
   **once, after** all plugins run (identity resolved), never per-plugin (also removes a
   double-log of trajectory across multiple plugins). **Fix (b):** `ReIDPlugin` computes
   `cross_cam` from the pre-bind `p.cameras_seen` (the person already lived elsewhere)
   rather than `p.multi_camera`, which is still single at emit time. Result: green
   (`persons=2 gA=1 gB=2 gC=1 identity_events=3 guard=held sticky=held crosscam=held`).

### Verify
`python -m PLATF.tests.test_reid_plugin` and `python -m PLATF.tests.test_substrate` → both `OK ...`.

### Notes / decisions
- Gallery gid IS the Person.global_id (via `get_or_create`), so the gallery and the
  store never diverge — no second id-mapping table.
- `MMGalleryAdapter.reinforce` feeds an exemplar via the gallery's own storage internals
  (`_store_embedding` + `_age_stamp`) without re-matching — the sticky path.
- Next (Phase B step 2): instantiate `MMGalleryAdapter` on the real gallery from a config
  and run the plugin host on a recorded observation stream; gate on `honest_metric.py`
  (no regression vs live two-tier). Then the deep identity work (appearance→face→gait→
  fusion + gallery retrieval upgrade).

---

## Phase B step 2 — replay gate + real-gallery adapter
Goal: prove the platform re-id path produces per-detection identity output that
`MTMC/honest_metric.py` scores exactly like the live pipeline's — the no-regression gate
— and wire the adapter to the REAL gallery. Live pipeline still untouched.

### Files created / changed
| file | purpose |
|---|---|
| `PLATF/replay.py` | replays an observation-stream JSONL through `PluginHost([ReIDPlugin(gallery)])` in co-temporal batches, writes a honest_metric CSV (camera,frame,track_id,global_id,bbox). `--gallery fake\|real` |
| `PLATF/tests/test_replay.py` | end-to-end (no box): synthetic 3-person/2-cam stream → replay → real `python -m MTMC.honest_metric` → asserts merge_rate 0 on the clean stream |
| `PLATF/plugins/reid_gallery.py` (`MMGalleryAdapter`) | **rewritten** to wrap `MTMC.streamapp_2tier.RealReID` (the live gallery SERVICE) via `from_config()`, not raw MMG |

### Issues + fixes
3. **MMGalleryAdapter wrapped raw MMG with guessed reinforce internals (wrong).**
   The real `_store_embedding(embs, quals, emb, q, cameras=, camera=)` / `_age_stamp(t)`
   signatures differ from my guess, and reinforce must operate on a gallery ENTRY, not a
   gid. **Fix:** wrap `RealReID` instead — it already exposes `match(app,face,cam,t,gait,
   exclude_gids)` and `reinforce(gid,app,face,cam,t,gait)` (this plugin's exact protocol),
   owns the lock, the calibrated thresholds and the correct internals. Adapter is now a
   thin pass-through; verified the MMG.match signature (`app,face,camera,t,gait,quality,
   exclude_gids`) matches. MMG mints on no-match, so it never returns a None gid.
4. **honest_metric subprocess: `No module named 'MTMC'`.** `_repo_root()` was one
   `dirname` short (returned `PLATF/`, not the worktree root). **Fix:** one more dirname.
   Result: `persons=3 identity_events=4 honest_metric.merge_rate=0.0`.

### Verify
`python -m PLATF.tests.test_replay` → `OK ... merge_rate=0.0`.

### Notes / open
- Part (a) — the replay harness + honest-metric plumbing + real-gallery adapter — is
  DONE and proven end-to-end with `--gallery fake` (no box).
- Part (b) — the real-gallery gate on REAL embeddings — is BLOCKED on capturing an
  observation stream WITH per-detection appearance embeddings. `EVENT_LOG_DIR` dumps
  bbox+gid+face only, NOT the appearance emb, so the real gallery can't be replayed from
  existing dumps. Needs a gated (off-by-default) observation dump in the worker + one live
  box run. Held for user OK before touching `streamapp_2tier` (standing constraint: don't
  change the working of the MTMC pipeline).
