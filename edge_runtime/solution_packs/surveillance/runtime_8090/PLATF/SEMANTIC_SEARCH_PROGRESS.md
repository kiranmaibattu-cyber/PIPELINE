# Semantic search build — progress

Goal: search by description or name → find a person's crops → reconstruct their
cross-camera path with timestamps → show which camera they are on now.

Run tests with: `PYTHONPATH=. python3 -m pytest PLATF/tests/ -q`
Restart the app with: `./stop_8090.sh && ./run_8090.sh`

## Stages

- [x] **1. Durable history store** — `PLATF/history.py`, `PLATF/tests/test_history.py` (13 tests).
      SQLite. One row per person-per-camera VISIT (not per frame). Copies the best
      crop out of the volatile FIFO cache so evidence survives pruning. Stores app +
      face vectors per sighting. Merges re-point `canonical_gid` without erasing the
      original `gid`. Age-based retention (`HISTORY_KEEP_DAYS`, default 30).
- [x] **2. Wire history into LivePlatform** — `_record_history()` per observation,
      `_record_event()` on the bus, `add_merge()` from the mapper (`server.py`) and the
      BEV linker (`app.py`), flush+prune thread. `PLATF/tests/conftest.py` sets
      HISTORY=0 so tests never write the real store. VERIFIED: opens on startup
      (`[hist]` log line), schema created, no errors. NOT YET verified with real
      people — the cameras have had zero detections since it went in (empty office).
- [x] **3. Identity persistence** — `REJOIN_STORE=PLATF/rejoin_store` in run_8090.sh
      (a FRESH dir on purpose: `reid_store/` holds July vectors from the ch* cameras,
      and rejoining today's people to those would fabricate identity links).
      VERIFIED active in the log:
      `[rejoin] thresholds face=0.45 app=0.145 gait=0.350 (body+gait must agree)`.
      Store files appear on its own save cadence (`REJOIN_SAVE_S`, default 60 s).
- [x] **4. Re-id guardrails** — evidence gate (`HISTORY_MIN_OBS=3`,
      `HISTORY_MIN_DUR_S=0.5`) drops one-frame blips; `History.link_check()` returns
      impossible / implausible / plausible; co-visibility on one camera is a hard
      veto (`HISTORY_STRICT_MERGE=1`) and refused merges are RECORDED, not dropped;
      topology windows loaded from the mapper's `learned_transitions.json`.
      Also added a schema migration (`_migrate`) — the live DB had the pre-verdict
      merge table and would have failed on the first merge.
- [x] **5. IRRA search over history** — `PLATF/search.py`, `/api/search` on :8090.
      Background indexer persists `irra` vectors per sighting. VERIFIED: model loads
      in-app (`search=ready`), and an end-to-end run on 24 REAL camera crops indexed
      and returned differently-ranked results per query.
- [x] **6. Name search** — name → face gallery + sighting names, no model needed.
- [x] **7. Path + live location UI** — Search page: evidence crops, full path trail
      across cameras, "on cam7 now" pill from the live store.

## Known quality limit (measured, not assumed)

IRRA retrieval on THIS footage is mediocre. Visual check of top-3 for "person in a
white shirt": #1 and #2 plausible, #3 (score 0.50) a dark patterned top — a miss.
Scores cluster tightly (0.52/0.51/0.50), which is itself weak discrimination.
Cause: the crops are people SEATED at desks, partially occluded, cropped mid-body;
IRRA was trained on CUHK-PEDES full-body pedestrian images. Options if this matters:
raise the crop bar for indexing (only index sightings above a quality/size
threshold), or try the other searchers already in `MTMC/text_search/models.py`
(`aptm`, `rde`, `clip_zeroshot`) and compare on a labelled set.

## Key facts discovered (do not re-derive)

- IRRA text→person search already exists: `MTMC/streamapp_2tier.py:967` `IRRAIndex`,
  model via `MTMC/text_search/models.py` `IRRASearcher` (CLIP dual encoder, 1.3 GB,
  fp32 CPU). NOT reachable on :8090 — its `/api/search` route is on the engine's own
  handler, which PLATF does not run. Indexes from in-memory `CROP_STORE`.
- FAISS is installed (1.14.3) but unused in the live path; the live galleries rank
  with numpy. `MTMC/persistent_gallery.py` uses it, and persists RAW vectors to
  `store.npz` + `meta.json` with faiss indexes rebuilt on load — keep that split.
- `reid_store/` and `reid_store_2tier/` are old rejoin stores (24 July): 285 face,
  748 app vectors. `REJOIN_STORE` is not set in `run_8090.sh`, so it is off.
- Crop cache `PLATF/cache/crops` is FIFO at cap 20000 and SATURATED — it deletes
  oldest-first continuously. That is why history copies its own evidence.
- `PersonStore(max_age_s=600)` drops a person 10 min after last sighting.
- Cross-camera appearance merging is deliberately OFF (`MAP_CROSS_THR=0`); the
  reliable cross-camera link on this footage is a face match
  (`MAP_FACE_MATCH_THR=0.26`) or BEV overlap. Expect path gaps and id splits —
  present results as sightings + evidence, not one confident track.
- Crop relative paths in observations resolve against `plat.obs_base_dir`
  (= `PLATF/cache`). Crop filename form: `crops/{camera}_{stable_id}_{frame}.jpg`.

## Restructure (in progress)

- [x] **Re-id exemplar memory** — `Exemplar` now carries `crop`, and eviction is
      PER-CAMERA: a full bucket drops the weakest exemplar of whichever camera holds
      the most, so a person walking into a second camera always gets a foothold.
      Previously the bucket was sorted by quality and truncated, so a lower-quality
      second camera contributed nothing — the exact case the memory exists for.
      `Exemplar` is `@dataclass(eq=False)`: `list.remove()` compares with `==`, and a
      generated `__eq__` over a numpy field raises "truth value of an array is
      ambiguous" (same trap FACE gallery `Vec` documents).
      Tests: `PLATF/tests/test_exemplars.py` (4).
- [ ] **Persist exemplars + their crops** so the 10-per-person memory is on disk and
      viewable, not only in RAM.
- [ ] **Face gallery**: platform-enrolled people have vectors but NO chips
      (`daya`: 5 vectors, `chip_path=""`, no files). Old flat path wrote them.
      Re-enrol through the new page, and/or backfill.
- [ ] **Consolidate stores** — currently 7 locations / 4 formats (see below).

## Where FAISS actually is (verified)

Used in EXACTLY one place: `MTMC/persistent_gallery.py` (the re-id rejoin store),
which builds `IndexIDMap2(IndexFlatIP)` from `store.npz` on load —
`[rejoin] loaded 690 identities (1019 face, 3781 app, 348 gait)`.
IRRA text search and face recognition use plain numpy `@`: at 899 x 512 that is a
1.8 MB matmul, sub-millisecond, and faster than FAISS at this scale. Add a FAISS
index only if the vector count reaches tens of thousands — and derive it from the
DB, never treat it as storage.
