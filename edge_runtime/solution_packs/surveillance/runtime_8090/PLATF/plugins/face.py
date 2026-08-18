"""Face-recognition plugin -- name a Person by matching the READ-ONLY enrollment gallery.

Identity vs recognition are separate (the two-galleries rule): the re-id plugin decides
"same person?" and mints anonymous Persons; this plugin only answers "who is this?" by
searching the enrollment face gallery and, on a confident match, attaching an IdentityLink
to the Person via the IdentityMapping. It NEVER writes the enrollment gallery and never
changes the gid -- the name is metadata on the persistent Person, so it survives gid churn.

Honest limit: the current footage has no frontal faces, so this runs but rarely matches
here. It is correct + ready for frontal cameras / a populated enrollment gallery. With no
gallery wired it is a safe no-op.
"""
from __future__ import annotations

import os
import time

from PLATF.core import Event, IdentityLink, Plugin


class FacePlugin(Plugin):
    name = "face"

    def __init__(self, gallery=None, thr: float | None = None, reverify_s: float = 60.0,
                 event_cooldown_s: float | None = None, leave_s: float | None = None,
                 groups=None):
        # gallery: an EnrollmentFaceGallery (search(face_emb, k) -> [(employee_id, dist)]).
        # None => no-op (built + correct, waiting for an enrollment gallery).
        self.gallery = gallery
        # Watchlist: enrolled name -> group label. Held BY REFERENCE so re-tagging a
        # person in the UI takes effect on the next recognition without rebuilding the
        # plugin (which would drop the presence state and re-alert everyone on screen).
        self.groups = groups if groups is not None else {}
        # Detection floor for RECOGNITION. Chosen from measurement, not taste: past
        # recognitions ran at det 0.503-0.807 (median 0.615) with 72% of crops having
        # no detectable face, so the bar has to sit above the median to bite.
        self.min_det = float(os.environ.get("FACE_MIN_DET", "0.65"))
        self.min_px = float(os.environ.get("FACE_MIN_PX", "40"))
        self.require_meta = os.environ.get("FACE_REQUIRE_META", "0") == "1"
        self.weak_rejects = 0
        # EnrollmentGalleryAdapter returns cosine DISTANCE. Live measurements split
        # real Kiran matches around <=0.40 from false matches around >=0.49, so use a
        # distance threshold, not the old similarity-style 0.8045 value.
        self.thr = float(os.environ.get("FACE_THR", "0.45") if thr is None else thr)
        self.alert_thr = float(os.environ.get("FACE_ALERT_THR", str(self.thr)))
        self.reverify_s = float(reverify_s)
        # Backward-compatible constructor arg/env name, but this is no longer a blind
        # timer. It is now the "leave grace": if a named person disappears from a camera
        # for this long, the next recognition in that camera is treated as a new visit.
        self.leave_s = float(os.environ.get(
            "FACE_ACTIVITY_LEAVE_S",
            os.environ.get("FACE_EVENT_COOLDOWN_S",
                           "30" if leave_s is None and event_cooldown_s is None
                           else str(leave_s if leave_s is not None else event_cooldown_s))))
        self._misses = {}
        # Activity is presence-based, not frame-based. While the same enrolled identity
        # remains active in one camera, do not emit repeated "recognized" events. If the
        # same identity is also recognized in another camera, emit a separate event for
        # that camera. Tracker/GID churn inside the leave grace should not spam activity.
        self._active = {}      # (employee_id, camera) -> {"person_uuid", "last_seen", "last_emit"}

    def _face_is_worth_matching(self, obs) -> bool:
        """Is there actually a face here, or just a chip aligned out of hair?

        AdaFace returns a valid unit vector for ANY 112x112 chip, so `has_face()`
        only means "an embedding exists". Measured on 60 recognitions made before
        this gate: 72% of the crops contained NO detectable face at all, and the
        rest averaged det 0.615 at 40 px. Those vectors landed 0.57-0.67 from
        enrolled faces -- as high as genuine frontal matches -- so no match
        threshold could separate them. The only place the two are distinguishable
        is the DETECTION that produced the chip.

        Deliberately strict: on ceiling cameras this rejects most candidates, which
        is the intended trade. Rare and trustworthy beats frequent and wrong.

        An observation with no face_meta at all is not judged here -- replayed and
        synthetic observations carry none, and silently dropping them would change
        behaviour that has nothing to do with this problem.
        """
        meta = (obs.meta or {}).get("face_meta")
        if not meta:
            return not self.require_meta
        try:
            det = float(meta.get("det") or 0.0)
            px = float(meta.get("w") or 0.0)
        except (TypeError, ValueError):
            return not self.require_meta
        return det >= self.min_det and px >= self.min_px

    def process(self, obs, person, ctx):
        if self.gallery is None or person is None or not obs.has_face():
            return
        if not self._face_is_worth_matching(obs):
            self.weak_rejects += 1
            return
        cur = ctx.store.identity.get(person.person_uuid)
        try:
            res = self.gallery.search(obs.face_emb, k=1)
        except Exception:
            return
        if not res:
            key = person.person_uuid
            self._misses[key] = self._misses.get(key, 0) + 1
            return
        emp, dist = res[0]
        if float(dist) > self.thr:
            return
        self._misses[person.person_uuid] = 0
        link = IdentityLink(employee_id=str(emp), name=str(emp),
                            confidence=round(1.0 - float(dist), 3), source="face",
                            last_verified=float(obs.t))
        ctx.store.identity.link(person.person_uuid, link)
        now = time.monotonic()
        key = (str(emp), str(obs.camera))
        active = self._active.get(key)
        active_recent = bool(active and now - float(active.get("last_seen", -1e30)) <= self.leave_s)
        should_emit = not active_recent
        self._active[key] = {"person_uuid": person.person_uuid, "last_seen": now,
                             "last_emit": float(active.get("last_emit", now) if active else now)}
        if should_emit:
            self._active[key]["last_emit"] = now
            group = str(self.groups.get(str(emp), "") or "").strip().lower()
            payload = {"employee_id": str(emp), "dist": round(float(dist), 3)}
            if group:
                # Tag the recognition with the group so a reader can tell that this
                # arrival is also covered by an alert event, without having to join the
                # two streams on (name, camera, time). The dashboard uses it to show ONE
                # row per arrival; the recognition itself is still emitted, because it is
                # what feeds the recognition log and the roster's last-seen.
                payload["group"] = group
            ctx.emit(Event("face_recognized", obs.t, obs.camera, person.global_id,
                           payload=payload))
            # Watchlist alert, on the SAME presence rule as the recognition above: one
            # per arrival on a camera, not one per frame. Raised only for a confirmed
            # face match, so an unknown visitor never trips it.
            if group == "unauthorised" and float(dist) <= self.alert_thr:
                ctx.emit(Event("unauthorised", obs.t, obs.camera, person.global_id,
                               payload={"employee_id": str(emp), "group": group,
                                        "dist": round(float(dist), 3)}))

    def on_tick(self, t: float, ctx):
        """Keep presence alive from tracking, and expire it only after a real absence.

        Face embeddings can be intermittent. If Kiran was recognized once and then turns
        away, tracking should keep the activity presence active until she leaves the camera.
        """
        now = time.monotonic()
        live_named = set()
        for p in ctx.store.all():
            if now - p.last_active_mono > self.leave_s:
                continue
            link = ctx.store.identity.get(p.person_uuid)
            if not link or not link.name or not p.current_camera:
                continue
            key = (str(link.name), str(p.current_camera))
            live_named.add(key)
            active = self._active.get(key, {})
            self._active[key] = {
                "person_uuid": p.person_uuid,
                "last_seen": now,
                "last_emit": float(active.get("last_emit", now)),
            }
        for key, active in list(self._active.items()):
            if key in live_named:
                continue
            if now - float(active.get("last_seen", -1e30)) > self.leave_s:
                self._active.pop(key, None)
