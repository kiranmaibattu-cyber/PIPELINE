"""Offline GBSL tracklet mapper -- restore ids by re-clustering the Person store.

Live re-id is online-greedy: it commits identity on a track's first frames, so a person
who re-appears (looped clip / new track / slightly different crop) often fails to re-match
and gets a NEW gid -> churn. This runs periodically ON THE SIDE over the ACCUMULATED Person
records (all their exemplars + camera/time), builds a similarity graph, and clusters
duplicates so `PersonStore.merge` can collapse them back into one persistent identity --
graph-based self-learning: each pass decides with more evidence than the first-frame match.

Pure functions over a list of Person objects, so they unit-test without the engine. All the
false-merge lessons are baked in as HARD constraints:
  - co-visibility (same camera, overlapping time) => different people => never merge
  - complete-linkage clustering => no transitivity chaining (A~B, B~C, A/~C)
  - a TIGHTER bar for cross-camera than same-camera (cross-cam is faceless / data-limited)
  - face veto when both sides have a face and the faces disagree
"""
from __future__ import annotations

import numpy as np


def _unit(vs):
    if not vs:
        return None
    M = np.stack([np.asarray(v, np.float32) for v in vs])
    n = np.linalg.norm(M, axis=1, keepdims=True)
    n[n < 1e-9] = 1e-9
    return M / n


def _min_dist(a_embs, b_embs) -> float:
    """1 - max cosine similarity over the two exemplar sets (min appearance distance)."""
    A, B = _unit(a_embs), _unit(b_embs)
    if A is None or B is None:
        return 999.0
    return 1.0 - float((A @ B.T).max())


def _cam_intervals(person) -> dict:
    """camera -> [min_t, max_t] envelope of the person's presence (from the trajectory,
    falling back to exemplar stamps)."""
    iv: dict = {}
    def note(cam, t):
        if cam is None:
            return
        if cam in iv:
            iv[cam][0] = min(iv[cam][0], t); iv[cam][1] = max(iv[cam][1], t)
        else:
            iv[cam] = [t, t]
    for (t, cam, _fp) in getattr(person, "trajectory", []) or []:
        note(cam, float(t))
    for e in person.app_embs:
        note(e.camera, float(e.t))
    return iv


def _covisible(a_iv, b_iv, margin: float = 1.5) -> bool:
    """True if the two were BOTH present in the same camera for more than `margin` seconds
    at the same time -> physically different people -> must never merge. A brief boundary
    touch (a tracker break splitting one person) is allowed."""
    for cam in set(a_iv) & set(b_iv):
        s = max(a_iv[cam][0], b_iv[cam][0])
        e = min(a_iv[cam][1], b_iv[cam][1])
        if e - s > margin:
            return True
    return False


def _concurrent_conflict(a_iv, b_iv, overlapping, margin: float = 1.5) -> bool:
    """True if the two persons are present in DIFFERENT cameras at overlapping wall-clock
    times AND that camera pair is NOT declared overlapping -> one person cannot be in two
    non-overlapping places at once -> different people -> block. (Same-camera concurrency is
    already handled by _covisible.) This is the offline pipeline's cross-cam concurrent
    exclusion -- the hard negative that kills the 'one id in 5 cameras at once' blob."""
    for ca, (as_, ae) in a_iv.items():
        for cb, (bs, be) in b_iv.items():
            if ca == cb:
                continue
            if frozenset((ca, cb)) in overlapping:
                continue                                # e.g. ch9/ch10 legitimately co-occur
            s = max(as_, bs); e = min(ae, be)
            if e - s > margin:
                return True
    return False


def _face_disagree(a, b, veto: float) -> bool:
    fa = [e.emb for e in a.face_embs]
    fb = [e.emb for e in b.face_embs]
    if not fa or not fb:
        return False
    return _min_dist(fa, fb) > veto


def _bhattacharyya(a, b) -> float:
    """Bhattacharyya distance between two colour histograms (0 identical, 1 disjoint)."""
    a = np.asarray(a, np.float32); b = np.asarray(b, np.float32)
    sa, sb = a.sum(), b.sum()
    if sa < 1e-9 or sb < 1e-9:
        return 1.0
    bc = float(np.sqrt((a / sa) * (b / sb)).sum())
    return float(np.sqrt(max(0.0, 1.0 - bc)))


def _color_dist(a_colors, b_colors) -> float:
    """Min torso-colour distance between two persons across their cameras (999 if unknown).
    This is the offline pipeline's cross-cam precision gate."""
    ha = [h for hs in a_colors.values() for h in hs]
    hb = [h for hs in b_colors.values() for h in hs]
    if not ha or not hb:
        return 999.0
    return min(_bhattacharyya(x, y) for x in ha for y in hb)


def build_edges(persons, app_thr, cross_thr, face_veto, colour_gate=0.45, topo=None,
                overlapping=None, min_cross_obs=0, exclude=None, face_match_thr=0.0):
    """Candidate merges as (dist, gid_u, gid_v), best (closest) first. Every hard constraint
    is applied here so the clusterer only sees survivable pairs.

    Same-camera (a person's split ids): appearance close (+ face veto if both have faces).
    CROSS-camera needs PHYSICAL plausibility (not co-visible, not concurrent in two non-
    overlapping cameras, off-chain excluded, topology window ok) AND one of:
      * FACE MATCH: both have a face and the face distance < face_match_thr -- the STRONG
        signal that separates people in identical uniforms, and the only thing that works
        across NON-overlapping cameras where clothing colour can't. (face_match_thr<=0 off.)
      * APPEARANCE+COLOUR: body appearance < cross_thr AND torso colour agrees (Bhattacharyya
        <= colour_gate) -- the fallback for people whose face wasn't captured but whose
        clothing differs. (cross_thr<=0 off.)
    Face is thus a POSITIVE matcher here, not merely a veto. Physics still overrides face: a
    strong face match that is physically impossible (two places at once) is still rejected."""
    ov = {frozenset(p) for p in (overlapping or [])}
    excl = set(exclude or ())               # off-chain cameras: never cross-link across them
    ivs = {p.global_id: _cam_intervals(p) for p in persons}
    cams = {p.global_id: set(p.cameras_seen) for p in persons}
    embs = {p.global_id: [e.emb for e in p.app_embs] for p in persons}
    faces = {p.global_id: [e.emb for e in p.face_embs] for p in persons}
    cols = {p.global_id: dict(p.color_hists) for p in persons}
    edges = []
    n = len(persons)
    for i in range(n):
        a = persons[i]
        for j in range(i + 1, n):
            b = persons[j]
            ga, gb = a.global_id, b.global_id
            if _covisible(ivs[ga], ivs[gb]):
                continue                                   # hard negative
            d = _min_dist(embs[ga], embs[gb])
            if cams[ga] == cams[gb]:
                # same-camera split id: appearance close + face veto
                if app_thr <= 0 or d >= app_thr:
                    continue
                if _face_disagree(a, b, face_veto):
                    continue
                edges.append((d, ga, gb))
                continue
            # ---- cross-camera ----
            if cross_thr <= 0 and face_match_thr <= 0:
                continue                                   # cross-cam disabled
            # PHYSICAL plausibility (geometry/time, independent of appearance) -- always
            if excl and (excl & (cams[ga] ^ cams[gb])):
                continue                                   # bridges an off-chain camera (e.g. ch16)
            if _concurrent_conflict(ivs[ga], ivs[gb], ov):
                continue                                   # in two non-overlapping cameras at once
            if topo is not None and not _topology_ok(ivs[ga], ivs[gb], topo, ov):
                continue                                   # implausible / no known transition
            if min_cross_obs > 0 and (len(embs[ga]) < min_cross_obs
                                      or len(embs[gb]) < min_cross_obs):
                continue                                   # too green to absorb a foreign camera
            face_d = _min_dist(faces[ga], faces[gb])
            face_ok = face_match_thr > 0 and face_d < face_match_thr
            appc_ok = (cross_thr > 0 and d < cross_thr
                       and _color_dist(cols[ga], cols[gb]) <= colour_gate)
            if not (face_ok or appc_ok):
                continue                                   # neither strong face nor appearance+colour
            if not face_ok and _face_disagree(a, b, face_veto):
                continue                                   # appearance path still respects the face veto
            edges.append((face_d if face_ok else d, ga, gb))
    edges.sort(key=lambda e: e[0])
    return edges


def _topology_ok(a_iv, b_iv, topo, overlapping=frozenset()) -> bool:
    """A cross-camera merge needs POSITIVE evidence: either the two cameras are declared
    overlapping (ch9/ch10), or a learned transition window exists AND the time gap fits it.
    No overlap and no known window for ANY of their camera pairs -> block (was previously
    'allow', which let impossible far pairs like ch1<->ch16 merge on look-alike uniforms)."""
    for ca, (as_, ae) in a_iv.items():
        for cb, (bs, be) in b_iv.items():
            if ca == cb:
                continue
            if frozenset((ca, cb)) in overlapping:
                return True                             # co-located cameras -> plausible
            w = topo.window(ca, cb)
            if not w:
                continue
            lo, hi = w
            gap = bs - ae if bs >= ae else as_ - be     # gap between the two presences
            if lo - 2.0 <= gap <= hi + 2.0:
                return True
    return False


def cluster(persons, app_thr: float = 0.20, cross_thr: float = 0.14,
            face_veto: float = 1.1, colour_gate: float = 0.45, topo=None,
            max_cluster: int = 8, overlapping=None, min_cross_obs=0, exclude=None,
            face_match_thr: float = 0.0):
    """Complete-linkage clustering over the candidate edges. A node joins a cluster only if
    it is within-bar of EVERY current member (no transitivity chaining). Returns clusters of
    gids (size > 1) to merge, plus the survivor gid (the earliest-minted = smallest gid)."""
    edges = build_edges(persons, app_thr, cross_thr, face_veto, colour_gate, topo,
                        overlapping=overlapping, min_cross_obs=min_cross_obs, exclude=exclude,
                        face_match_thr=face_match_thr)
    passed = {(min(u, v), max(u, v)) for _d, u, v in edges}
    parent = {p.global_id: p.global_id for p in persons}
    members = {p.global_id: {p.global_id} for p in persons}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _d, u, v in edges:                                  # best first
        ru, rv = find(u), find(v)
        if ru == rv:
            continue
        if len(members[ru]) + len(members[rv]) > max_cluster:
            continue
        # complete-linkage: every cross pair between the two clusters must be a passed edge
        ok = all((min(x, y), max(x, y)) in passed
                 for x in members[ru] for y in members[rv])
        if not ok:
            continue
        parent[rv] = ru
        members[ru] |= members[rv]

    out = []
    seen = set()
    for gid in list(parent):
        r = find(gid)
        if r in seen:
            continue
        seen.add(r)
        grp = members[r]
        if len(grp) > 1:
            out.append({"survivor": min(grp), "gids": sorted(grp)})
    return out
