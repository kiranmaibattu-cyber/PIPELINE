#!/bin/bash
# Launch the Person Intelligence Platform (the single app) on the box.
# Run detached:  nohup setsid bash PLATF/start.sh >/tmp/app.log 2>&1 </dev/null &
# Restart cleanly:  send SIGTERM to the main PLATF.app python proc (graceful), verify all
# python PLATF.app procs are gone, THEN relaunch (a fast kill+relaunch stalls the decode
# because the dead instance's GPU/shm is not yet released).
cd "$(dirname "$0")/.." || exit 1

# faiss candidate-search engages at this gallery size. Default 512 fell back to a LINEAR
# scan as the gallery grew (churn) and starved the decode workers -> fps collapsed 20->8.
# 128 keeps re-id search O(topk) as the gallery grows, so fps stays stable + scales.
export FAISS_MIN_GALLERY="${FAISS_MIN_GALLERY:-128}"

# re-id match bars (measured on the looping clips). The default 0.145 was calibrated on
# same-track near-duplicates -> too tight, so a person re-appearing (looped video / new
# track) fell outside it and got a NEW id = heavy churn. 0.20 re-recognises the same
# person (within-camera live-id count ~70 -> ~41). XCAM tightens the CROSS-camera bar to
# a fraction of that so faceless look-alikes across ch9<->ch10 don't false-merge (links
# ~15 -> ~8). Cross-cam stays data-limited regardless (no frontal faces). Tune base live
# via the Metrics slider.
export REID_THR="${REID_THR:-0.20}"
# CROSS-CAMERA MATCHING OFF (bar = 0): appearance alone false-merges different people
# across cameras on faceless/uniform footage (crops proved one "cross-cam id" was 3
# different people). So ids are PER CAMERA -- clean, no false merges. A person crossing
# cameras gets a new id (honest: we can't reliably link them without faces). Cross-cam
# identity comes back later via the BEV map / frontal cameras. Raise XCAM_THR to re-enable.
export XCAM_THR="${XCAM_THR:-0.0}"

# CPU / scaling: keep the shared gallery SMALL so the re-id service's fusion + repair
# stay cheap as cameras grow. Default 1800s (30min) holds the whole looping clip; 300s
# keeps only recent ids. Repair less often + over fewer ids (it is O(N^2)).
export GALLERY_MAX_AGE_S="${GALLERY_MAX_AGE_S:-300}"
export REPAIR_EVERY_S="${REPAIR_EVERY_S:-40}"
export REPAIR_MAX_IDS="${REPAIR_MAX_IDS:-200}"

# The real CPU driver on this box is DETECTION (iGPU detection-volume-bound; measured: the
# gallery-aging tune above barely moved the 2 hot procs). PROC_EVERY=2 detects every 2nd
# processed frame and lets the tracker carry the between-frames -> ~half the detection CPU,
# so the box scales to ~2x cameras. Trade: slightly less detection freshness. Set 1 for max
# accuracy, 3 for even more headroom.
export PROC_EVERY="${PROC_EVERY:-2}"

# LOCAL_REASSOC inherits a gid by BOX POSITION, so person B stepping into the spot person A
# just left gets A's id -> one gid oscillating across 2 people (the garbage track lines). On
# uniform/faceless footage the appearance verify can't separate them. OFF: a re-acquired
# track gets a fresh id (more fragmentation + a brief T->P flicker) but NEVER a wrong merge,
# and the GBSL mapper re-collapses the honest splits. Clean tracklets are the foundation
# cross-cam MTMC needs. Set 1 to restore the flicker-heal.
export LOCAL_REASSOC="${LOCAL_REASSOC:-0}"

# everything the app owns lives UNDER PLATF/: code, config, models (PLATF/models),
# and the cache (PLATF/cache/crops = person crops, bounded by PLATF_CROP_CAP).
exec python3 -u -m PLATF.app \
  --streams PLATF/config/streams.yaml \
  --crops PLATF/cache \
  --port "${PLATF_PORT:-8090}"
