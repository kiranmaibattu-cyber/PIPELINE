"""Vendored from FACE/gallery.py (read-only enrollment gallery).

The face gallery: what gets stored, what gets thrown away, and why.

The embedding model is settled (AdaFace, AUC 0.922 on this project's own
footage). This file is the part that actually decides long-run accuracy.

Storage is keyed on two axes:

  pose      MEASURED, from 3D landmarks: frontal / left / right.
  mode      DISCOVERED, not labelled. Inside one (person, pose) cell, a vector
            that is far from every existing mode centroid opens a new mode.

The second axis is the important one. Mask, beard, glasses, night IR and a new
camera angle are precisely the things that push a face far from its existing
vectors, so each earns its own mode automatically. No mask classifier is
needed, and no "IR = low saturation" rule -- which would misfire on grey
daylight, low light and monochrome footage alike. Those conditions are still
recorded per vector as hints, for reading in reports, never as storage keys.

Sizes: 2 vectors per mode, up to 3 modes per pose cell, hard cap 20 per person.
Enrollment writes 5. A long-lived identity settles around 6-10.

Matching is multi-template MAX, never a centroid. Averaging is actively wrong
here: masked/unmasked and RGB/IR are bimodal, so the mean lands between the
modes and matches neither. Max scoring costs accuracy in the other direction --
with K templates each identity gets K chances at an accidental high score -- so
a decision also has to clear a runner-up margin, and the threshold is
calibrated on the max statistic itself rather than on pairwise scores.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

# Seeds only. calibrate.py re-derives every one of these from real captures
# before any accuracy claim is made.
DEFAULTS = {
    # a vector this close to one already in its mode teaches nothing new
    "redundant_sim": 0.92,
    # below this similarity to every mode centroid, it is a NEW appearance mode
    "mode_split": 0.55,
    # refuse to store a face that looks more like someone else than like itself
    "impostor_guard": 0.55,
    # quality gate -- sharpness and source pixels, NOT the AdaFace norm, which
    # measures frontality rather than image quality (measured 2026-07-27)
    "min_sharp": 40.0,
    "min_px": 24.0,
    "min_det": 0.55,
    # Pitch is gated for STORAGE only, never for matching. Measured on 157
    # observations of one enrolled person: median score falls 0.836 (|pitch|
    # 0-10 deg) -> 0.762 -> 0.664 -> 0.613 -> 0.536 (40 deg+). Steep enough
    # that a tilted chip should not burn an appearance-mode slot; shallow
    # enough that it still matches comfortably, so rejecting it at query time
    # would throw away recall for nothing. This camera points up from below,
    # so large pitch is common, not exotic.
    "max_pitch": 30.0,
    # identification
    "match_threshold": 0.45,
    "match_margin": 0.05,
    # capacity
    "per_mode": 2,
    "modes_per_cell": 3,
    "max_per_person": 20,
}


@dataclass(eq=False)
class Vec:
    """eq=False is load-bearing, not a style choice.

    A generated __eq__ compares every field, including the numpy vector, and
    `array == array` yields an array -- so `list.remove(v)` raises "truth value
    of an array is ambiguous" the moment it compares against a non-identical
    entry. Identity is also the semantics we actually want: two stored faces
    are the same entry only if they are the same object.
    """

    person: str
    cell: str                  # pose bin
    mode: int
    vec: np.ndarray
    quality: float
    chip_path: str
    source: str = "camera"     # camera | photo
    added_at: float = field(default_factory=time.time)
    hits: int = 0
    last_hit: float = 0.0
    meta: dict = field(default_factory=dict)

    def row(self) -> dict:
        # quality is NOT rounded: it decides replacements, and a value rounded
        # on save would flip a "not better" decision into a replacement after a
        # restart, making the gallery behave differently across a reboot.
        d = {k: v for k, v in self.__dict__.items() if k != "vec"}
        d["added_at"] = round(self.added_at, 1)
        return d


def safe_name(person: str) -> str:
    """Person names become directory names, so they must not be able to escape.

    A name like ``../../outside`` would otherwise write chips outside the
    gallery, and the later relative_to() would raise only after the file had
    already landed somewhere unintended.
    """
    clean = "".join(ch if (ch.isalnum() or ch in "-_ .") else "_" for ch in person).strip()
    clean = clean.strip(".") or "unnamed"
    if clean in {".", ".."}:
        clean = "unnamed"
    return clean


def _unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else v


def quality_score(sharp: float, px: float, det: float) -> float:
    """Blur x resolution x detector confidence, each softly saturating.

    Deliberately excludes the AdaFace norm: on this camera the norm ranked a
    sharp 50 px profile (17) below a blurry 30 px frontal face (28), because it
    scores frontality, not image quality. Pose is already handled by the cell,
    so folding frontality in here would double-count it and starve the side
    bins.
    """
    return float(min(sharp / 200.0, 1.5) * min(px / 60.0, 1.5) * det)


class Gallery:
    def __init__(self, root: str | Path, cfg: dict | None = None) -> None:
        self.root = Path(root)
        self.chips = self.root / "chips"
        self.chips.mkdir(parents=True, exist_ok=True)
        self._cfg_override = dict(cfg or {})
        self.cfg = {**DEFAULTS, **self._cfg_override}
        self.vecs: list[Vec] = []
        self._load()

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        idx, arr = self.root / "index.json", self.root / "vectors.npy"
        if idx.exists() != arr.exists():
            # Half a gallery is not an empty gallery. Returning silently here
            # would let the next save() overwrite whichever half survived.
            raise RuntimeError(
                f"gallery half-written in {self.root}: "
                f"index.json={idx.exists()} vectors.npy={arr.exists()}")
        if not idx.exists():
            return
        blob = json.loads(idx.read_text(encoding="utf-8"))
        rows = blob["vectors"]
        mat = np.load(arr)
        if len(rows) != len(mat):
            raise RuntimeError(
                f"gallery corrupt: {len(rows)} index rows vs {len(mat)} vectors")
        # Thresholds are a property of the stored gallery, not of whoever opens
        # it. Reopening with library defaults would silently change what counts
        # as a match. Explicit cfg passed to __init__ still wins.
        self.cfg = {**DEFAULTS, **blob.get("config", {}), **self._cfg_override}
        self.vecs = [Vec(vec=mat[i], **r) for i, r in enumerate(rows)]

    def save(self) -> None:
        """Atomic: both files land together or neither does.

        Overwriting in place risks a crash between the two writes, which pairs
        new vectors with stale metadata whenever the counts happen to match --
        silently attributing faces to the wrong people.
        """
        mat = (np.stack([v.vec for v in self.vecs]).astype(np.float32)
               if self.vecs else np.zeros((0, 512), np.float32))
        tmp_a = self.root / "vectors.npy.tmp"
        tmp_i = self.root / "index.json.tmp"
        np.save(tmp_a, mat)
        tmp_i.write_text(json.dumps({
            "config": self.cfg,
            "people": self.people(),
            "vectors": [v.row() for v in self.vecs],
        }, indent=2), encoding="utf-8")
        # np.save appends .npy when the name lacks it; ours ends .tmp, so it
        # writes vectors.npy.tmp.npy. Take whichever exists.
        src_a = tmp_a if tmp_a.exists() else tmp_a.with_suffix(".tmp.npy")
        src_a.replace(self.root / "vectors.npy")
        tmp_i.replace(self.root / "index.json")

    # -- views -----------------------------------------------------------

    def people(self) -> list[str]:
        return sorted({v.person for v in self.vecs})

    def of(self, person: str) -> list[Vec]:
        return [v for v in self.vecs if v.person == person]

    def _cell(self, person: str, cell: str) -> list[Vec]:
        return [v for v in self.vecs if v.person == person and v.cell == cell]

    def _mode_centroids(self, person: str, cell: str) -> dict[int, np.ndarray]:
        out: dict[int, list[np.ndarray]] = {}
        for v in self._cell(person, cell):
            out.setdefault(v.mode, []).append(v.vec)
        cents = {}
        for m, vs in out.items():
            c = np.mean(vs, axis=0)
            n = float(np.linalg.norm(c))
            cents[m] = c / n if n > 1e-12 else c
        return cents

    def _matrix(self, exclude: str | None = None) -> tuple[np.ndarray, list[Vec]]:
        keep = [v for v in self.vecs if v.person != exclude]
        if not keep:
            return np.zeros((0, 512), np.float32), []
        return np.stack([v.vec for v in keep]), keep

    # -- the add decision -------------------------------------------------

    def consider(self, person: str, obs, source: str = "camera") -> dict:
        """Offer one FaceObs for storage. Returns what happened and why.

        Every branch names its reason, because a gallery that silently drops
        the sample you needed is impossible to debug six weeks later.
        """
        c = self.cfg
        if obs.pose is None:
            return {"action": "reject", "reason": f"yaw {obs.yaw:+.0f} out of range"}

        if abs(getattr(obs, "pitch", 0.0)) > c["max_pitch"]:
            return {"action": "reject",
                    "reason": f"pitch {obs.pitch:+.0f} too steep"}

        q = quality_score(obs.sharp, obs.face_px, obs.det_score)
        if obs.det_score < c["min_det"]:
            return {"action": "reject", "reason": f"det {obs.det_score:.2f}", "quality": q}
        if obs.sharp < c["min_sharp"]:
            return {"action": "reject", "reason": f"blurry sharp={obs.sharp:.0f}", "quality": q}
        if obs.face_px < c["min_px"]:
            return {"action": "reject", "reason": f"small {obs.face_px:.0f}px", "quality": q}

        # Never enroll a face that already looks like somebody else. This is the
        # guard that stops one bad chip from poisoning two identities at once.
        other, other_v = self._matrix(exclude=person)
        if len(other):
            sims = other @ obs.vec
            j = int(np.argmax(sims))
            if float(sims[j]) >= c["impostor_guard"]:
                return {"action": "reject", "quality": q,
                        "reason": f"looks like {other_v[j].person} ({sims[j]:.2f})"}

        cell = obs.pose
        cents = self._mode_centroids(person, cell)
        mode, sim = -1, -1.0
        for m, cvec in cents.items():
            s = float(cvec @ obs.vec)
            if s > sim:
                mode, sim = m, s

        mine = self.of(person)
        if len(mine) >= c["max_per_person"]:
            weakest = min(mine, key=lambda v: v.quality)
            if q <= weakest.quality or self._is_last_frontal(weakest):
                return {"action": "skip", "reason": "gallery full, not better", "quality": q}
            # Evict globally, but file the newcomer under ITS OWN pose cell.
            # Reusing the evicted vector's cell would label a profile as
            # frontal and quietly destroy the last real frontal.
            dest_mode = mode if (cents and sim >= c["mode_split"]) else (
                (max(cents) + 1) if cents else 0)
            return self._replace(weakest, person, cell, dest_mode, obs, q, source,
                                 "gallery full, upgraded")

        if not cents or sim < c["mode_split"]:
            if len(cents) < c["modes_per_cell"]:
                new_mode = (max(cents) + 1) if cents else 0
                return self._store(person, cell, new_mode, obs, q, source,
                                   f"new appearance mode (sim {sim:.2f})")
            # Cell is out of modes and this vector belongs to none of them.
            # Parking it in the nearest mode would blend two unrelated
            # appearances into one centroid, which then mis-routes everything
            # that follows. Refuse, and say so, so calibrate.py can tell us
            # whether modes_per_cell is set too tight.
            return {"action": "skip", "quality": q,
                    "reason": f"cell {cell} out of modes, nearest sim {sim:.2f}"}

        members = [v for v in self._cell(person, cell) if v.mode == mode]
        if len(members) < c["per_mode"]:
            near = max((float(m.vec @ obs.vec) for m in members), default=0.0)
            if near >= c["redundant_sim"]:
                return {"action": "skip", "quality": q,
                        "reason": f"redundant (sim {near:.2f})"}
            return self._store(person, cell, mode, obs, q, source,
                               f"fills mode {mode} (sim {sim:.2f})")

        # Mode is full. A sharper retake of a bad enrollment chip is a
        # near-duplicate by construction, so this path must NOT be gated on
        # novelty -- a pure novelty rule would reject exactly the sample that
        # fixes the problem.
        weakest = min(members, key=lambda v: v.quality)
        if q > 1.15 * weakest.quality and not self._is_last_frontal(weakest):
            return self._replace(weakest, person, cell, mode, obs, q, source,
                                 f"upgrade q {weakest.quality:.2f}->{q:.2f}")
        return {"action": "skip", "reason": "mode full, not better", "quality": q}

    def drop(self, person: str) -> int:
        """Forget a person, moving their chips aside rather than orphaning them.

        Deleting the vectors alone leaves the JPGs in chips/, so a contact
        sheet then shows faces the gallery no longer holds -- which defeats the
        one audit this project actually trusts.
        """
        gone = self.of(person)
        for v in gone:
            self.vecs.remove(v)
            # A vector with no chip has nothing to retire. Skipping is not just
            # tidiness: `root / ""` is the gallery ROOT, which exists, so the move
            # below would try to drag the whole gallery into retired/<person>/.
            # Platform-enrolled vectors carry chip_path="" and hit exactly this.
            if not v.chip_path:
                continue
            src = self.root / v.chip_path
            if not src.exists():
                continue
            ret = self.root / "retired" / safe_name(person)
            ret.mkdir(parents=True, exist_ok=True)
            stem, k = src.stem, 0
            while (ret / f"{stem}_{k}.jpg").exists():
                k += 1
            shutil.move(str(src), str(ret / f"{stem}_{k}.jpg"))
        return len(gone)

    def orphan_chips(self) -> list[Path]:
        """Chip files under chips/ that no stored vector references."""
        kept = {(self.root / v.chip_path).resolve() for v in self.vecs}
        return sorted(p for p in self.chips.rglob("*.jpg") if p.resolve() not in kept)

    def _is_last_frontal(self, v: Vec) -> bool:
        """A person must always keep one frontal vector -- it is the one view
        every camera eventually gets, and evicting it strands the identity."""
        return v.cell == "frontal" and sum(
            1 for o in self.vecs if o.person == v.person and o.cell == "frontal") <= 1

    def _chip_path(self, person: str, cell: str, mode: int) -> Path:
        d = self.chips / safe_name(person)
        d.mkdir(parents=True, exist_ok=True)
        n = 0
        while (d / f"{cell}_m{mode}_{n}.jpg").exists():
            n += 1
        return d / f"{cell}_m{mode}_{n}.jpg"

    def _store(self, person, cell, mode, obs, q, source, reason) -> dict:
        p = self._chip_path(person, cell, mode)
        # Every stored vector keeps its chip on disk. Not optional: an earlier
        # gallery in this project enrolled a burnt-in timestamp overlay as a
        # person's face, and only a contact sheet ever caught it.
        if not cv2.imwrite(str(p), obs.chip):
            raise RuntimeError(f"could not write chip {p}")
        self.vecs.append(Vec(
            person=person, cell=cell, mode=mode, vec=_unit(obs.vec),
            quality=q, chip_path=str(p.relative_to(self.root)).replace("\\", "/"),
            source=source,
            meta={"yaw": round(obs.yaw, 1), "pitch": round(obs.pitch, 1),
                  "px": round(obs.face_px, 1), "sharp": round(obs.sharp, 1),
                  "det": round(obs.det_score, 3), "norm": round(obs.norm, 2),
                  **obs.meta},
        ))
        return {"action": "add", "reason": reason, "quality": q,
                "cell": cell, "mode": mode, "chip": str(p)}

    def _replace(self, old: Vec, person, cell, mode, obs, q, source, reason) -> dict:
        """Store the newcomer FIRST, then retire the incumbent.

        The other order loses data outright: if the chip write fails (bad
        image, full or read-only disk) after the old vector has already been
        removed, the gallery is left short one face with nothing to show for
        it. `cell`/`mode` are the NEWCOMER's, never the evicted vector's.
        """
        out = self._store(person, cell, mode, obs, q, source, reason)
        gone = self.root / old.chip_path
        self.vecs.remove(old)
        if gone.exists():
            # Retired chips keep their person and a counter: several people can
            # own a `frontal_m0_0.jpg`, and on Windows a colliding move raises.
            ret = self.root / "retired" / safe_name(old.person)
            ret.mkdir(parents=True, exist_ok=True)
            stem, k = gone.stem, 0
            while (ret / f"{stem}_{k}.jpg").exists():
                k += 1
            shutil.move(str(gone), str(ret / f"{stem}_{k}.jpg"))
        out["action"] = "replace"
        out["replaced"] = old.chip_path
        return out

    # -- identification ---------------------------------------------------

    def identify(self, vec: np.ndarray) -> dict:
        """Best identity for one query vector, or unknown and why not.

        Max over that person's templates, then a runner-up margin against the
        second-best *person*. The margin is what keeps multi-template scoring
        honest: with up to 20 vectors each, some identity will always produce a
        flattering single score, so a win has to be a clear win.
        """
        if not self.vecs:
            return {"name": None, "matched": False, "reason": "empty gallery"}
        # Normalise the query. Without this both the threshold and the margin
        # scale with the query's magnitude, so the same face accepted or
        # rejected depending on how the caller happened to build the vector.
        vec = _unit(np.asarray(vec, dtype=np.float32))
        mat = np.stack([v.vec for v in self.vecs])
        sims = mat @ vec
        best: dict[str, tuple[float, int]] = {}
        for i, v in enumerate(self.vecs):
            s = float(sims[i])
            if s > best.get(v.person, (-2.0, -1))[0]:
                best[v.person] = (s, i)
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])
        (name, (top, idx)) = ranked[0]

        # With one enrolled person there is no runner-up. Inventing a score of
        # -1.0 would hand every query a margin of top+1 and disable the check
        # entirely -- exactly when a single identity makes false accepts most
        # likely, since everyone who walks past resembles the only option.
        if len(ranked) > 1:
            runner_name, runner = ranked[1][0], ranked[1][1][0]
            margin = top - runner
            margin_ok = margin >= self.cfg["match_margin"]
        else:
            runner_name, margin, margin_ok = None, None, True

        ok = bool(top >= self.cfg["match_threshold"] and margin_ok)
        if ok:
            self.vecs[idx].hits += 1
            self.vecs[idx].last_hit = time.time()
        return {"name": name if ok else None, "score": round(top, 4),
                "best": name, "runner_up": runner_name,
                "margin": None if margin is None else round(margin, 4),
                "matched": ok, "chip": self.vecs[idx].chip_path,
                "reason": "" if ok else
                          ("below threshold" if top < self.cfg["match_threshold"]
                           else "margin too small")}

    # -- reporting --------------------------------------------------------

    def summary(self) -> str:
        lines = []
        for p in self.people():
            vs = self.of(p)
            cells: dict[str, set[int]] = {}
            for v in vs:
                cells.setdefault(v.cell, set()).add(v.mode)
            desc = " ".join(f"{c}:{len(m)}m" for c, m in sorted(cells.items()))
            lines.append(f"  {p:<16} {len(vs):2d} vectors   {desc}")
        return "\n".join(lines) or "  (empty)"
