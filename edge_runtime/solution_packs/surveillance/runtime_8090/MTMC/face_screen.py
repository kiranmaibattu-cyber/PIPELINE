"""Stage-2 face-model screen: verification AUC on ch10 (frontal camera) faces.

Phase A (--collect): person detection + IoU tracking on ch10; run insightface
face detection inside each person crop; save ALIGNED 112x112 chips per track.
Same-person pairs = same track >= min_gap sampled frames apart; different-person
pairs = co-occurring tracks in the same frame (both with visible faces).

Phase B (--model X): embed all pairs with one face model, report verification
AUC + midpoint threshold. Results merge into MTMC/reports/stage2_face_screen.json.

Face models:
  arcface_buffalo_s  — insightface pack (existing)
  arcface_buffalo_l  — insightface pack (auto-download)
  antelopev2         — insightface pack (manual download; auto-dl broken upstream)
  adaface_ir101      — AdaFace repo + GDrive ckpt
  magface_ir100      — MagFace repo + GDrive ckpt

Usage:
    python -m MTMC.face_screen --collect
    python -m MTMC.face_screen --model arcface_buffalo_l
    python -m MTMC.face_screen --all
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
_CACHE = _ROOT / "MTMC" / "cache" / "face_chips"
_OUT = _ROOT / "MTMC" / "reports" / "stage2_face_screen.json"

FACE_MODELS = [
    "arcface_buffalo_s",
    "arcface_buffalo_l",
    "antelopev2",
    "adaface_ir101",
    "magface_ir100",
    "kprpe",
]


def _l2(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


# ---------------------------------------------------------------------- collect

def collect_chips(
    n_frames: int = 200,
    frame_step: int = 15,
    min_gap: int = 3,
    max_same: int = 300,
    max_diff: int = 300,
    min_face_px: int = 28,
) -> None:
    from insightface.app import FaceAnalysis
    from insightface.utils import face_align

    from MTMC.pipelines import load_mtmc_config
    from MTMC.adapters import MultiClassDetector, IoUTracker, crop_boxes

    config = load_mtmc_config()
    bench = config["benchmark"]
    detector = MultiClassDetector(bench["detector"], bench["confidence"],
                                  bench["iou"], set(bench["class_ids"]))

    app = FaceAnalysis(name="buffalo_s", root=str(_ROOT / "models" / "face_reid"),
                       providers=["CPUExecutionProvider"], allowed_modules=["detection"])
    app.prepare(ctx_id=-1, det_size=(320, 320))

    cap = cv2.VideoCapture(str(_ROOT / config["videos"]["ch10_5min"]))
    tracker = IoUTracker()
    track_chips: dict[int, list[tuple[int, np.ndarray]]] = {}
    frame_chips: dict[int, list[tuple[int, np.ndarray]]] = {}  # sampled_frame -> [(track, chip)]

    sampled = 0
    frame_idx = 0
    while sampled < n_frames:
        ok, frame = cap.read()
        if not ok:
            break
        frame_idx += 1
        if frame_idx % frame_step:
            continue
        sampled += 1
        boxes = detector.detect(frame)
        tracks = tracker.update(boxes, sampled)
        crops = crop_boxes(frame, [t.bbox for t in tracks])
        for tr, crop in zip(tracks, crops):
            if crop.shape[0] < 100:
                continue
            rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            faces = app.get(rgb)
            if not faces:
                continue
            best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
            if (best.bbox[2] - best.bbox[0]) < min_face_px:
                continue
            chip = face_align.norm_crop(crop, best.kps)  # BGR 112x112 aligned
            track_chips.setdefault(tr.local_id, []).append((sampled, chip))
            frame_chips.setdefault(sampled, []).append((tr.local_id, chip))
    cap.release()

    same_pairs, diff_pairs = [], []
    for chips in track_chips.values():
        for a in range(len(chips)):
            for b in range(a + 1, len(chips)):
                if chips[b][0] - chips[a][0] >= min_gap and len(same_pairs) < max_same:
                    same_pairs.append((chips[a][1], chips[b][1]))
    for entries in frame_chips.values():
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                if entries[i][0] != entries[j][0] and len(diff_pairs) < max_diff:
                    diff_pairs.append((entries[i][1], entries[j][1]))

    _CACHE.mkdir(parents=True, exist_ok=True)
    for name, pairs in (("same", same_pairs), ("diff", diff_pairs)):
        if not pairs:
            print(f"WARNING: no {name} pairs mined")
            continue
        np.savez_compressed(_CACHE / f"{name}_pairs.npz",
                            a=np.array([p[0] for p in pairs], dtype=np.uint8),
                            b=np.array([p[1] for p in pairs], dtype=np.uint8))
    print(f"tracks with faces: {len(track_chips)} | same pairs: {len(same_pairs)} | diff pairs: {len(diff_pairs)}")


# ---------------------------------------------------------------------- embedders

def _load_insightface_rec(pack: str):
    """Return embed(chips_bgr_112) using an insightface pack's recognition model."""
    from insightface.app import FaceAnalysis

    app = FaceAnalysis(name=pack, root=str(_ROOT / "models" / "face_reid"),
                       providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
                       allowed_modules=["detection", "recognition"])
    app.prepare(ctx_id=0, det_size=(320, 320))
    rec = app.models["recognition"]

    def embed(chips: list[np.ndarray]) -> np.ndarray:
        return np.vstack([_l2(rec.get_feat(c).flatten()) for c in chips])
    return embed, f"insightface:{pack}"


def _load_adaface():
    import torch
    repo = _ROOT / "repos" / "adaface"
    ckpt_path = _ROOT / "models" / "adaface" / "adaface_ir101_webface12m.ckpt"
    if not ckpt_path.exists():
        raise FileNotFoundError(f"missing {ckpt_path} — see https://github.com/mk-minchul/AdaFace")
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    import net  # AdaFace repo

    model = net.build_model("ir_101")
    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    state = {k[6:]: v for k, v in ckpt["state_dict"].items() if k.startswith("model.")}
    model.load_state_dict(state)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.eval().to(device)

    def embed(chips: list[np.ndarray]) -> np.ndarray:
        # AdaFace: BGR, /255 -> [0,1], then (x-0.5)/0.5
        batch = np.stack([((c.astype(np.float32) / 255.0) - 0.5) / 0.5 for c in chips])
        tensor = torch.from_numpy(np.ascontiguousarray(batch.transpose(0, 3, 1, 2))).to(device)
        with torch.no_grad():
            feats, _ = model(tensor)
        return np.vstack([_l2(f) for f in feats.cpu().numpy()])
    return embed, "adaface:ir101_webface12m"


def _load_magface():
    import torch
    repo = _ROOT / "repos" / "magface"
    ckpt_path = _ROOT / "models" / "magface" / "magface_ir100_ms1mv2.pth"
    if not ckpt_path.exists():
        raise FileNotFoundError(f"missing {ckpt_path} — see https://github.com/IrvingMeng/MagFace")
    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(repo / "inference"))  # inference/ has no __init__.py
    from network_inf import builder_inf  # MagFace repo

    class _Args:
        arch = "iresnet100"
        embedding_size = 512
        cpu_mode = not torch.cuda.is_available()
        resume = str(ckpt_path)
        dist = 1
    model = builder_inf(_Args())
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.eval().to(device)

    def embed(chips: list[np.ndarray]) -> np.ndarray:
        # MagFace: RGB? official inference uses cv2 imgs /255, mean 0 std 1... use [0,1]
        batch = np.stack([c[:, :, ::-1].astype(np.float32) / 255.0 for c in chips])
        tensor = torch.from_numpy(batch.transpose(0, 3, 1, 2).copy()).to(device)
        with torch.no_grad():
            feats = model(tensor)
        return np.vstack([_l2(f) for f in feats.cpu().numpy()])
    return embed, "magface:ir100"


def _load_kprpe():
    import os
    import subprocess as _subprocess

    import torch

    repo_id = "minchul/cvlface_adaface_vit_base_kprpe_webface4m"
    repo = _ROOT / "repos" / "cvlface"
    model_dir = _ROOT / "models" / "kprpe"
    ckpt_path = model_dir / "pretrained_model" / "model.pt"

    if not ckpt_path.exists():
        from huggingface_hub import hf_hub_download

        model_dir.mkdir(parents=True, exist_ok=True)
        files_txt = hf_hub_download(repo_id, "files.txt", local_dir=model_dir)
        files = [f.strip() for f in Path(files_txt).read_text(encoding="utf-8").splitlines() if f.strip()]
        for name in files + ["config.json", "wrapper.py", "model.safetensors"]:
            hf_hub_download(repo_id, name, local_dir=model_dir)

    if not ckpt_path.exists():
        raise FileNotFoundError(f"missing {ckpt_path} after downloading {repo_id}")
    if not repo.exists():
        raise FileNotFoundError(f"missing {repo} - clone https://github.com/mk-minchul/CVLface")

    model_path = str(model_dir)
    cwd = os.getcwd()
    orig_check_call = _subprocess.check_call

    def _skip_rpe_build(*args, **kwargs):
        os.chdir(model_path)
        raise _subprocess.CalledProcessError(1, args[0] if args else None)

    added_path = False
    if model_path not in sys.path:
        sys.path.insert(0, model_path)
        added_path = True
    try:
        # CVLface tries to build an optional CUDA/C++ RPE op at import time.
        # The model has a torch gather fallback; this keeps cwd stable on Windows
        # when CUDA_HOME is not set.
        _subprocess.check_call = _skip_rpe_build
        os.chdir(model_path)
        from wrapper import CVLFaceRecognitionModel, ModelConfig  # CVLface HF bundle

        model = CVLFaceRecognitionModel(ModelConfig())
    finally:
        _subprocess.check_call = orig_check_call
        os.chdir(cwd)
        if added_path:
            sys.path.remove(model_path)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.eval().to(device)
    keypoints = torch.tensor(
        [[38.29459953, 51.69630051],
         [73.53179932, 51.50139999],
         [56.02519989, 71.73660278],
         [41.54930115, 92.3655014],
         [70.72990036, 92.20410156]],
        dtype=torch.float32,
        device=device,
    ) / 112.0

    def embed(chips: list[np.ndarray]) -> np.ndarray:
        # CVLface recognition models expect RGB tensors normalized to [-1, 1].
        batch = np.stack([((c[:, :, ::-1].astype(np.float32) / 255.0) - 0.5) / 0.5 for c in chips])
        tensor = torch.from_numpy(np.ascontiguousarray(batch.transpose(0, 3, 1, 2))).to(device)
        kps = keypoints.unsqueeze(0).expand(tensor.shape[0], -1, -1)
        with torch.no_grad():
            feats = model(tensor, kps)
        return np.vstack([_l2(f) for f in feats.cpu().numpy()])
    return embed, "cvlface:adaface_vit_base_kprpe_webface4m"


def _load_face_model(key: str):
    if key == "arcface_buffalo_s":
        return _load_insightface_rec("buffalo_s")
    if key == "arcface_buffalo_l":
        return _load_insightface_rec("buffalo_l")
    if key == "antelopev2":
        return _load_insightface_rec("antelopev2")
    if key == "adaface_ir101":
        return _load_adaface()
    if key == "magface_ir100":
        return _load_magface()
    if key == "kprpe":
        return _load_kprpe()
    raise ValueError(f"unknown face model: {key}")


# ---------------------------------------------------------------------- screen

def screen_model(key: str) -> dict:
    try:
        embed, backend = _load_face_model(key)
    except Exception as exc:  # noqa: BLE001
        result = {"model": key, "status": "skipped", "reason": str(exc)[:200]}
        _merge(result)
        print(json.dumps(result, indent=2))
        return result

    same = np.load(_CACHE / "same_pairs.npz")
    diff = np.load(_CACHE / "diff_pairs.npz")

    def _sims(npz) -> np.ndarray:
        sims = []
        bs = 32
        n = npz["a"].shape[0]
        for s in range(0, n, bs):
            ea = embed([npz["a"][i] for i in range(s, min(s + bs, n))])
            eb = embed([npz["b"][i] for i in range(s, min(s + bs, n))])
            sims.extend(float(np.dot(x, y)) for x, y in zip(ea, eb))
        return np.array(sims)

    same_sims, diff_sims = _sims(same), _sims(diff)
    mean_same, mean_diff = float(same_sims.mean()), float(diff_sims.mean())
    labels = np.r_[np.ones_like(same_sims), np.zeros_like(diff_sims)]
    scores = np.r_[same_sims, diff_sims]
    order = np.argsort(scores)
    ranks = np.empty_like(order, dtype=float); ranks[order] = np.arange(1, len(scores) + 1)
    n_pos, n_neg = len(same_sims), len(diff_sims)
    auc = (ranks[labels == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)

    result = {
        "model": key, "status": "ok", "backend": backend,
        "mean_same_sim": round(mean_same, 4), "mean_diff_sim": round(mean_diff, 4),
        "threshold": round(1.0 - (mean_same + mean_diff) / 2.0, 4),
        "auc": round(float(auc), 4), "n_same": n_pos, "n_diff": n_neg,
    }
    _merge(result)
    print(json.dumps(result, indent=2))
    return result


def _merge(result: dict) -> None:
    merged = json.loads(_OUT.read_text(encoding="utf-8")) if _OUT.exists() else {}
    merged[result["model"]] = result
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps(merged, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--model", default="")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if args.collect:
        collect_chips()
        return 0
    if args.model:
        r = screen_model(args.model)
        return 0 if r.get("status") == "ok" else 1
    if args.all:
        if not (_CACHE / "same_pairs.npz").exists():
            collect_chips()
        done = json.loads(_OUT.read_text(encoding="utf-8")) if _OUT.exists() else {}
        for key in FACE_MODELS:
            if done.get(key, {}).get("status") == "ok":
                print(f"{key}: done (auc={done[key]['auc']})")
                continue
            subprocess.run([sys.executable, "-m", "MTMC.face_screen", "--model", key], cwd=str(_ROOT))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
