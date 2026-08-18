"""Compile desired camera apps into optimized per-camera graph nodes."""
from __future__ import annotations

from edge_runtime.graph.manifest_loader import ManifestRepository
from edge_runtime.graph.models import CameraDesiredState, CameraGraph, DesiredState
from edge_runtime.runtime.api_tags import ApiTagBuilder


NODE_MODELS = {
    "person_detector": ("person_detector",),
    "body_embedder": ("body_reid_embedder",),
    "face_embedder": ("face_embedder",),
    "gait_segmenter": ("segmenter",),
    "gait_embedder": ("gait_embedder",),
    "vehicle_detector": ("vehicle_detector",),
    "plate_detector": ("plate_detector",),
    "ocr_service": ("plate_ocr",),
}

NODE_STATE = {
    "global_reid_service": ("reid_gallery", "persistent_identity_store"),
    "enrolled_face_gallery": ("enrolled_face_gallery",),
    "identity_anchor": ("identity_store",),
}


class CameraGraphBuilder:
    """Builds one camera graph from app manifests.

    It only decides data dependencies and feature flags. Hardware placement is a
    separate concern handled by the planner.
    """

    def __init__(self, manifests: ManifestRepository) -> None:
        self._manifests = manifests
        self._api_tags = ApiTagBuilder()

    def build(self, edge_id: str, revision: int, camera: CameraDesiredState) -> CameraGraph:
        manifests = [self._manifests.get(camera.solution_pack, app) for app in camera.apps]
        required_data, required_services = self._expand_requirements(camera, manifests)
        nodes = self._nodes_for(camera, required_data, required_services)
        edges = self._edges_for(camera, required_data, required_services)
        required_models = _dedupe(v for node in nodes for v in NODE_MODELS.get(node, ()))
        required_state = _dedupe(
            list(v for m in manifests for v in m.state)
            + list(v for node in nodes for v in NODE_STATE.get(node, ()))
        )
        return CameraGraph(
            camera_id=camera.camera_id,
            source=camera.source,
            solution_pack=camera.solution_pack,
            apps=camera.apps,
            fps=camera.fps,
            nodes=nodes,
            edges=edges,
            required_data=required_data,
            required_services=required_services,
            required_models=required_models,
            required_state=required_state,
            plugins=camera.apps,
            feature_flags=self._feature_flags(camera, required_data),
            api_tags=self._api_tags.camera_tags(
                edge_id=edge_id,
                revision=revision,
                solution_pack=camera.solution_pack,
                camera_id=camera.camera_id,
                apps=camera.apps,
            ),
            config=camera.config,
        )

    def _expand_requirements(self, camera: CameraDesiredState, manifests) -> tuple[tuple[str, ...], tuple[str, ...]]:
        data = list(v for manifest in manifests for v in manifest.required_data)
        services = set(v for manifest in manifests for v in manifest.required_services)
        apps = set(camera.apps)

        if camera.solution_pack == "surveillance":
            if apps:
                data.append("person_tracks")
            if "reid" in apps:
                data.append("body_embeddings")
                reid_manifest = self._manifests.get("surveillance", "reid")
                if reid_manifest.policy.get("enable_face_fusion", False):
                    data.append("face_embeddings")
                if reid_manifest.policy.get("enable_gait", False):
                    data.append("gait_embeddings")
            if "face_recognition" in apps:
                data.extend(["face_embeddings", "global_person_id"])
                services.update({"enrolled_face_gallery", "identity_anchor"})
            if "global_person_id" in data:
                data.append("body_embeddings")
                services.add("global_reid_service")

        if camera.solution_pack == "traffic" and apps:
            if apps & {"anpr", "wrong_way", "vehicle_counting", "illegal_parking"}:
                data.append("vehicle_tracks")
            if "pedestrian_counting" in apps:
                data.append("pedestrian_tracks")

        return _dedupe(data), _dedupe(services)

    def _nodes_for(
        self,
        camera: CameraDesiredState,
        required_data: tuple[str, ...],
        required_services: tuple[str, ...],
    ) -> tuple[str, ...]:
        nodes = ["camera_source", "decode"]
        if camera.solution_pack == "surveillance":
            nodes.extend(["person_detector", "person_tracker"])
            if "body_embeddings" in required_data:
                nodes.extend(["person_cropper", "body_embedder"])
            if "face_embeddings" in required_data:
                nodes.extend(["face_cropper", "face_embedder"])
            if "gait_embeddings" in required_data:
                nodes.extend(["gait_segmenter", "gait_embedder"])
            if "global_person_id" in required_data or "reid" in camera.apps or "global_reid_service" in required_services:
                nodes.append("global_reid_service")
            if "enrolled_face_gallery" in required_services:
                nodes.append("enrolled_face_gallery")
            if "identity_anchor" in required_services:
                nodes.append("identity_anchor")
            nodes.extend(app for app in camera.apps if app not in {"reid"})
            nodes.append("event_sink")
            return tuple(nodes)

        if camera.solution_pack == "traffic":
            nodes.extend(["vehicle_detector"])
            if any(item in required_data for item in ("vehicle_tracks", "pedestrian_tracks")):
                nodes.append("vehicle_tracker")
            if "plate_detections" in required_data:
                nodes.append("plate_detector")
            if "plate_ocr" in required_data:
                nodes.append("ocr_service")
            nodes.extend(camera.apps)
            nodes.append("event_sink")
            return tuple(nodes)

        raise ValueError(f"unsupported solution_pack: {camera.solution_pack}")

    def _edges_for(
        self,
        camera: CameraDesiredState,
        required_data: tuple[str, ...],
        required_services: tuple[str, ...],
    ) -> tuple[tuple[str, str], ...]:
        if camera.solution_pack == "surveillance":
            edges = [
                ("camera_source", "decode"),
                ("decode", "person_detector"),
                ("person_detector", "person_tracker"),
            ]
            if "body_embeddings" in required_data:
                edges.extend([
                    ("person_tracker", "person_cropper"),
                    ("person_cropper", "body_embedder"),
                ])
            if "face_embeddings" in required_data:
                edges.extend([
                    ("person_tracker", "face_cropper"),
                    ("face_cropper", "face_embedder"),
                ])
            if "gait_embeddings" in required_data:
                edges.extend([
                    ("person_tracker", "gait_segmenter"),
                    ("gait_segmenter", "gait_embedder"),
                ])
            if "global_reid_service" in required_services or "global_person_id" in required_data or "reid" in camera.apps:
                for producer in ("body_embedder", "face_embedder", "gait_embedder"):
                    if producer in [edge[1] for edge in edges]:
                        edges.append((producer, "global_reid_service"))
            if "face_recognition" in camera.apps:
                edges.extend([
                    ("face_embedder", "enrolled_face_gallery"),
                    ("global_reid_service", "identity_anchor"),
                    ("enrolled_face_gallery", "identity_anchor"),
                    ("identity_anchor", "face_recognition"),
                    ("face_recognition", "event_sink"),
                ])
            for app in camera.apps:
                if app == "reid":
                    edges.append(("global_reid_service", "event_sink"))
                elif app in {"intrusion", "people_counting", "loitering", "absence"}:
                    edges.extend([
                        ("person_tracker", app),
                        (app, "event_sink"),
                    ])
            return _dedupe_edges(edges)

        if camera.solution_pack == "traffic":
            edges = [
                ("camera_source", "decode"),
                ("decode", "vehicle_detector"),
            ]
            if "vehicle_tracks" in required_data or "pedestrian_tracks" in required_data:
                edges.append(("vehicle_detector", "vehicle_tracker"))
            if "plate_detections" in required_data:
                edges.append(("vehicle_detector", "plate_detector"))
            if "plate_ocr" in required_data:
                edges.append(("plate_detector", "ocr_service"))
            for app in camera.apps:
                if app == "anpr":
                    edges.extend([("ocr_service", "anpr"), ("anpr", "event_sink")])
                elif app in {"wrong_way", "vehicle_counting", "pedestrian_counting", "illegal_parking"}:
                    edges.extend([("vehicle_tracker", app), (app, "event_sink")])
            return _dedupe_edges(edges)

        raise ValueError(f"unsupported solution_pack: {camera.solution_pack}")

    def _feature_flags(self, camera: CameraDesiredState, required_data: tuple[str, ...]) -> dict[str, bool]:
        apps = set(camera.apps)
        return {
            "person_detect": camera.solution_pack == "surveillance",
            "vehicle_detect": camera.solution_pack == "traffic",
            "track": "person_tracks" in required_data or "vehicle_tracks" in required_data or "pedestrian_tracks" in required_data,
            "body": "body_embeddings" in required_data,
            "face": "face_embeddings" in required_data,
            "gait": "gait_embeddings" in required_data,
            "reid": "reid" in apps or "global_person_id" in required_data,
            "plate": "plate_detections" in required_data,
            "ocr": "plate_ocr" in required_data,
        }


class EdgeGraphBuilder:
    def __init__(self, camera_builder: CameraGraphBuilder) -> None:
        self._camera_builder = camera_builder

    def build_camera_graphs(self, desired: DesiredState) -> tuple[CameraGraph, ...]:
        return tuple(
            self._camera_builder.build(desired.edge_id, desired.revision, camera)
            for camera in desired.cameras
        )


def _dedupe(values) -> tuple[str, ...]:
    out = []
    seen = set()
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return tuple(out)


def _dedupe_edges(values: list[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    out = []
    seen = set()
    for src, dst in values:
        key = (src, dst)
        if key not in seen:
            out.append(key)
            seen.add(key)
    return tuple(out)
