"""Multimodal person embeddings for the Sentinel v4 ingest contract."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import time
from typing import Any
import uuid

import cv2
import numpy as np
from PIL import Image

from .sentinel_delivery import SentinelV2Outbox, SentinelV2Uploader


BODY_MODEL_ID = "transreid_ssl_int8"
BODY_MODEL_VERSION = "v1"
BODY_DIMENSIONS = 384
BODY_EMBEDDING_SPACE = "transreid_ssl_int8@v1:l2:384"
FACE_MODEL_ID = "face-embedding-model-v1"
FACE_MODEL_VERSION = "adaface_ir101_int8-v1"
FACE_DIMENSIONS = 512
FACE_EMBEDDING_SPACE = "adaface-ir101-int8@v1:aligned112:l2:512"
GAIT_MODEL_ID = "gaitbase_int8"
GAIT_MODEL_VERSION = "v1"
GAIT_DIMENSIONS = 4096
GAIT_EMBEDDING_SPACE = "gaitbase_int8@v1:30x64x44:parts16x256:l2:4096"


def _l2(vector: np.ndarray, expected: int) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    if vector.size != expected or not np.isfinite(vector).all():
        raise ValueError(f"embedding must contain {expected} finite values")
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        raise ValueError("embedding must not be zero")
    return vector / norm


class OpenVINOBodyExtractor:
    model_id = BODY_MODEL_ID
    model_version = BODY_MODEL_VERSION
    dimension = BODY_DIMENSIONS

    def __init__(self, model_path: str, device: str = "NPU") -> None:
        try:
            import openvino as ov
        except ImportError as exc:
            raise RuntimeError("OpenVINO is required for body Re-ID") from exc
        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(f"body Re-ID model is missing: {path}")
        core = ov.Core()
        self.compiled = core.compile_model(core.read_model(str(path)), device)
        self.input = self.compiled.input(0)
        self.output = self.compiled.output(0)
        shape = list(self.input.shape)
        self.height, self.width = int(shape[2]), int(shape[3])

    def warmup(self) -> None:
        self.embed([np.zeros((self.height, self.width, 3), dtype=np.uint8)])

    def _prepare(self, crop: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        rgb = np.asarray(
            Image.fromarray(rgb).resize((self.width, self.height), Image.BILINEAR),
            dtype=np.float32,
        )
        tensor = ((rgb / 255.0) - 0.5) / 0.5
        return np.transpose(tensor, (2, 0, 1))[None]

    def embed(self, crops: list[np.ndarray]) -> np.ndarray:
        vectors = []
        for crop in crops:
            result = np.asarray(
                self.compiled([self._prepare(crop)])[self.output], dtype=np.float32
            )
            vectors.append(_l2(result, self.dimension))
        return np.stack(vectors) if vectors else np.empty((0, self.dimension), np.float32)


def opengait_pretreat(mask: np.ndarray, target_h: int = 64, target_w: int = 44):
    """Normalize a binary person mask using the OpenGait silhouette convention."""
    if mask is None or mask.size == 0 or mask.max() == 0:
        return None
    ys = np.where(mask.any(axis=1))[0]
    if len(ys) < 10:
        return None
    mask = mask[ys[0]:ys[-1] + 1]
    height, width = mask.shape
    resized_width = max(1, int(width * target_h / height))
    mask = cv2.resize(mask, (resized_width, target_h), interpolation=cv2.INTER_NEAREST)
    weights = mask.sum(axis=0)
    if weights.sum() == 0:
        return None
    center = int(round((weights * np.arange(len(weights))).sum() / weights.sum()))
    half = target_w // 2
    padded = np.zeros((target_h, len(weights) + 2 * target_w), dtype=mask.dtype)
    padded[:, target_w:target_w + len(weights)] = mask
    result = padded[:, center + target_w - half:center + target_w + half]
    if result.shape != (target_h, target_w):
        return None
    return (result > 0).astype(np.uint8) * 255


class OpenVINOGaitExtractor:
    model_id = GAIT_MODEL_ID
    model_version = GAIT_MODEL_VERSION
    dimension = GAIT_DIMENSIONS

    def __init__(
        self,
        gait_model_path: str,
        gait_device: str = "NPU",
        min_sequence: int = 20,
        max_sequence: int = 60,
        minimum_motion_pixels: float = 2.0,
    ) -> None:
        try:
            import openvino as ov
        except ImportError as exc:
            raise RuntimeError("OpenVINO is required for gait Re-ID") from exc
        self.source = "background_subtraction"
        core = ov.Core()
        gait_path = Path(gait_model_path)
        if not gait_path.is_file():
            raise FileNotFoundError(f"GaitBase model is missing: {gait_path}")
        self.gait = core.compile_model(core.read_model(str(gait_path)), gait_device)
        self.gait_output = self.gait.output(0)
        self.sequence_length = int(self.gait.input(0).shape[1])
        self.min_sequence = int(min_sequence)
        self.max_sequence = int(max_sequence)
        self.buffers: dict[str, list[np.ndarray]] = defaultdict(list)
        self.cache: dict[str, np.ndarray] = {}
        self.backgrounds: dict[str, Any] = {}
        self.minimum_motion_pixels = float(minimum_motion_pixels)
        self.centers: dict[tuple[str, int], np.ndarray] = {}

    def warmup(self) -> None:
        sequence = np.zeros((1, self.sequence_length, 64, 44), dtype=np.float32)
        self.gait([sequence])

    def collect(self, frame: np.ndarray, people: list[Any], camera_id: str,
                keys: dict[int, str]) -> dict[int, np.ndarray]:
        masks = self._silhouettes(frame, people, camera_id)
        output: dict[int, np.ndarray] = {}
        for person, mask in zip(people, masks):
            track_id = int(person.metadata["track_id"])
            x1, y1, x2, y2 = person.bbox
            center = np.asarray(((x1 + x2) / 2.0, (y1 + y2) / 2.0), np.float32)
            center_key = (camera_id, track_id)
            previous_center = self.centers.get(center_key)
            self.centers[center_key] = center
            if (previous_center is None or
                    float(np.linalg.norm(center - previous_center))
                    < self.minimum_motion_pixels):
                continue
            if mask is None:
                continue
            key = keys.get(track_id, f"track:{track_id}")
            buffer = self.buffers[key]
            buffer.append(mask)
            if len(buffer) > self.max_sequence:
                del buffer[:-self.max_sequence]
            if len(buffer) < self.min_sequence:
                continue
            indices = np.linspace(0, len(buffer) - 1, self.sequence_length).round().astype(int)
            sequence = np.stack(buffer)[indices].astype(np.float32) / 255.0
            result = np.asarray(self.gait([sequence[None]])[self.gait_output])
            vector = _l2(result, self.dimension)
            self.cache[key] = vector
            output[track_id] = vector
        return output

    def cached(self, key: str) -> np.ndarray | None:
        value = self.cache.get(key)
        return value.copy() if value is not None else None

    def rekey(self, old_key: str, new_key: str) -> None:
        if old_key == new_key:
            return
        if old_key in self.buffers:
            merged = self.buffers[new_key] + self.buffers.pop(old_key)
            self.buffers[new_key] = merged[-self.max_sequence:]
        if old_key in self.cache:
            self.cache[new_key] = self.cache.pop(old_key)

    def _silhouettes(self, frame: np.ndarray, people: list[Any], camera_id: str):
        background = self.backgrounds.get(camera_id)
        if background is None:
            background = cv2.createBackgroundSubtractorMOG2(
                history=300, varThreshold=25, detectShadows=False
            )
            self.backgrounds[camera_id] = background
        foreground = background.apply(frame)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        foreground = cv2.morphologyEx(foreground, cv2.MORPH_OPEN, kernel)
        foreground = cv2.morphologyEx(
            foreground, cv2.MORPH_CLOSE, kernel, iterations=2
        )
        return [opengait_pretreat(self._crop(foreground, p.bbox)) for p in people]

    @staticmethod
    def _crop(image: np.ndarray, bbox):
        height, width = image.shape[:2]
        x1, y1, x2, y2 = [int(value) for value in bbox]
        return image[max(0, y1):min(height, y2), max(0, x1):min(width, x2)]


class PresenceResolver:
    """Keep a camera-local presence stable across short tracker ID breaks."""

    def __init__(self, camera_id: str, max_gap: float, threshold: float,
                 maximum_center_distance: float = 320.0) -> None:
        self.camera_id = camera_id
        self.max_gap = max_gap
        self.threshold = threshold
        self.maximum_center_distance = maximum_center_distance
        self.track_to_presence: dict[int, str] = {}
        self.presences: dict[str, dict[str, Any]] = {}

    def resolve(self, track_id: int, vector: np.ndarray, bbox, now: float,
                active_tracks: set[int]):
        existing = self.track_to_presence.get(track_id)
        if existing:
            state = self.presences[existing]
            state.update(vector=vector.copy(), bbox=list(bbox), last_seen=now)
            return existing, None
        best = None
        for presence_id, state in self.presences.items():
            if now - state["last_seen"] > self.max_gap:
                continue
            if any(track in active_tracks for track in state["track_ids"][-1:]):
                continue
            similarity = float(np.dot(vector, state["vector"]))
            old_box = state["bbox"]
            old_center = np.asarray(((old_box[0] + old_box[2]) / 2.0,
                                     (old_box[1] + old_box[3]) / 2.0))
            new_center = np.asarray(((bbox[0] + bbox[2]) / 2.0,
                                     (bbox[1] + bbox[3]) / 2.0))
            if float(np.linalg.norm(new_center - old_center)) > self.maximum_center_distance:
                continue
            if similarity >= self.threshold and (best is None or similarity > best[0]):
                best = similarity, presence_id, state
        if best is None:
            presence_id = f"{self.camera_id}:presence:{uuid.uuid4().hex}"
            self.presences[presence_id] = {
                "vector": vector.copy(), "bbox": list(bbox), "last_seen": now,
                "track_ids": [track_id], "first_seen": now,
            }
            self.track_to_presence[track_id] = presence_id
            return presence_id, None
        similarity, presence_id, state = best
        previous = state["track_ids"][-1]
        state["track_ids"].append(track_id)
        state.update(vector=vector.copy(), bbox=list(bbox), last_seen=now)
        self.track_to_presence[track_id] = presence_id
        return presence_id, {"previous_track_id": previous, "confidence": similarity,
                             "method": "body_reid_temporal"}


class PersonReidPipeline:
    def __init__(self, camera_id: str, edge_id: str, body_extractor: Any,
                 gait_extractor: Any, camera_config: dict[str, Any]) -> None:
        self.camera_id = camera_id
        self.edge_id = edge_id
        self.body = body_extractor
        self.gait = gait_extractor
        self.camera_config = camera_config
        self.stream_session_id = str(uuid.uuid4())
        config = (camera_config.get("analytics") or {}).get("person_reid") or {}
        self._validate_model_config(config.get("embedding") or {})
        emission = config.get("emission") or {}
        self.interval = float(emission.get("interval_seconds", 5.0))
        self.jpeg_quality = int(emission.get("jpeg_quality", 88))
        self.silhouette_interval = int(emission.get("silhouette_interval_frames", 3))
        self.minimum_body_quality = float(emission.get("minimum_body_quality", 0.2))
        self.last_emitted: dict[int, float] = {}
        reassociation = config.get("reassociation") or {}
        self.resolver = PresenceResolver(
            camera_id,
            float(reassociation.get("maximum_gap_seconds", 8.0)),
            float(reassociation.get("minimum_body_similarity", 0.78)),
            float(reassociation.get("maximum_center_distance_pixels", 320.0)),
        )
        self.store = SentinelV2Outbox(
            Path(os.getenv("APEXFABRIC_STATE_ROOT", "/state")), camera_id
        )
        self.uploader = None
        base_url = os.getenv("SENTINEL_INGEST_BASE_URL", "").strip()
        token = os.getenv("SENTINEL_EDGE_TOKEN", "").strip()
        if base_url and token:
            self.uploader = SentinelV2Uploader(self.store, base_url, token)
            self.uploader.start()

    @staticmethod
    def _validate_model_config(embedding: dict[str, Any]) -> None:
        expected = {
            "body": (BODY_MODEL_ID, BODY_MODEL_VERSION, BODY_EMBEDDING_SPACE, BODY_DIMENSIONS),
            "face": (FACE_MODEL_ID, FACE_MODEL_VERSION, FACE_EMBEDDING_SPACE, FACE_DIMENSIONS),
            "gait": (GAIT_MODEL_ID, GAIT_MODEL_VERSION, GAIT_EMBEDDING_SPACE, GAIT_DIMENSIONS),
        }
        for modality, values in expected.items():
            configured = embedding.get(modality) or {}
            actual = (
                configured.get("model_id"), configured.get("model_version"),
                configured.get("embedding_space"), configured.get("dimensions"),
            )
            if actual != values:
                raise ValueError(
                    f"configured {modality} embedding space does not match the loaded model"
                )

    def process(self, packet: Any, people: list[Any], faces: list[Any] | None = None) -> int:
        people = [person for person in people if self._eligible(person, packet.frame.shape)]
        if not people:
            return 0
        now = time.time()
        active = {int(person.metadata["track_id"]) for person in people}
        due = [person for person in people
               if now - self.last_emitted.get(int(person.metadata["track_id"]), 0.0) >= self.interval]
        crops = [self._crop(packet.frame, person.bbox) for person in due]
        valid = [(person, crop) for person, crop in zip(due, crops)
                 if crop is not None and crop.size and self._quality(crop) >= self.minimum_body_quality]
        body_vectors = self.body.embed([crop for _, crop in valid]) if valid else []
        face_by_track = {int(face.track_id): face for face in (faces or [])}
        keys = {track: self.resolver.track_to_presence.get(track, f"track:{track}") for track in active}
        if packet.index % self.silhouette_interval == 0:
            self.gait.collect(packet.frame, people, self.camera_id, keys)
        emitted = 0
        for (person, crop), body_vector in zip(valid, body_vectors):
            track_id = int(person.metadata["track_id"])
            vector = _l2(body_vector, BODY_DIMENSIONS)
            old_key = keys.get(track_id, f"track:{track_id}")
            presence_id, reassociation = self.resolver.resolve(
                track_id, vector, person.bbox, now, active - {track_id}
            )
            self.gait.rekey(old_key, presence_id)
            gait_vector = self.gait.cached(presence_id)
            face = face_by_track.get(track_id)
            self._persist(packet, person, crop, vector, face, gait_vector,
                          presence_id, reassociation, now)
            self.last_emitted[track_id] = now
            emitted += 1
        return emitted

    def close(self) -> None:
        if self.uploader is not None:
            self.uploader.stop()

    def _persist(self, packet, person, crop, body_vector, face, gait_vector,
                 presence_id, reassociation, now):
        observation_id = str(uuid.uuid4())
        person_evidence_id = str(uuid.uuid4())
        captured_at = datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")
        person_body = self._jpeg(crop)
        height, width = packet.frame.shape[:2]
        raw_x1, raw_y1, raw_x2, raw_y2 = [int(value) for value in person.bbox]
        x1, x2 = max(0, min(width, raw_x1)), max(0, min(width, raw_x2))
        y1, y2 = max(0, min(height, raw_y1)), max(0, min(height, raw_y2))
        if x2 <= x1 or y2 <= y1:
            raise ValueError("tracked person bounding box is outside the frame")
        state = self.resolver.presences[presence_id]
        config = (self.camera_config.get("analytics") or {}).get("person_reid") or {}
        v4 = config.get("v4") or {}
        enabled_outputs = set(v4.get("outputs") or [])
        observation = {
            "schema_version": "4.0", "observation_id": observation_id,
            "deployment_id": v4["deployment_id"], "camera_id": self.camera_id,
            "config_revision": int(v4["config_revision"]),
            "event_type": "object_present", "observed_at": captured_at,
            "stream_session_id": self.stream_session_id,
            "track_id": str(person.metadata["track_id"]), "presence_id": presence_id,
            "object_type": "person", "vehicle_type": None,
            "bbox_normalized": {"x1": x1 / width, "y1": y1 / height,
                                "x2": x2 / width, "y2": y2 / height},
            "confidence": float(person.confidence), "quality": self._quality(crop),
            "first_seen_at": datetime.fromtimestamp(state["first_seen"], timezone.utc).isoformat().replace("+00:00", "Z"),
            "zone_id": None,
        }
        evidence = []
        embeddings = []
        if "body_embeddings" in enabled_outputs or (
                "gait_embeddings" in enabled_outputs and gait_vector is not None):
            evidence.append((self._evidence(person_evidence_id, observation_id, "person_crop",
                                            "subject_crop", captured_at, person_body, crop), person_body))
        if "body_embeddings" in enabled_outputs:
            embeddings.append(self._embedding(
                str(uuid.uuid4()), observation_id, person_evidence_id, "body",
                body_vector, observation["quality"], captured_at,
            ))
        if "gait_embeddings" in enabled_outputs and gait_vector is not None:
            embeddings.append(self._embedding(
                str(uuid.uuid4()), observation_id, person_evidence_id, "gait",
                gait_vector, observation["quality"], captured_at,
            ))
        if "face_embeddings" in enabled_outputs and face is not None:
            face_crop = self._crop(packet.frame, face.bbox)
            if face_crop is not None and face_crop.size:
                face_body = self._jpeg(face_crop)
                face_evidence_id = str(uuid.uuid4())
                evidence.append((self._evidence(
                    face_evidence_id, observation_id, "face_crop", "face_crop",
                    captured_at, face_body, face_crop,
                ), face_body))
                embeddings.append(self._embedding(
                    str(uuid.uuid4()), observation_id, face_evidence_id, "face",
                    _l2(face.embedding, FACE_DIMENSIONS), float(face.quality), captured_at,
                ))
        if embeddings:
            self.store.persist(observation, evidence, embeddings)

    @staticmethod
    def _embedding(identifier, observation_id, evidence_id, modality, vector,
                   quality, captured_at):
        values = np.asarray(vector, np.float32).reshape(-1)
        profiles = {
            "face": ("adaface-ir101-v18.1", "AdaFace-IR101-INT8", "v18.1"),
            "body": ("transreid-ssl-v18.1", "TransReID-SSL-INT8", "v18.1"),
            "gait": ("gaitbase-v18.1", "GaitBase-INT8", "v18.1"),
        }
        profile_id, model_id, version = profiles[modality]
        return {
            "schema_version": "4.0", "embedding_id": identifier,
            "observation_id": observation_id, "evidence_id": evidence_id,
            "profile_id": profile_id, "kind": modality, "model_id": model_id,
            "model_version": version, "embedding_space": profile_id,
            "dim": int(values.size),
            "distance_metric": "cosine", "normalization": "l2",
            "vector": [float(value) for value in values], "quality": float(quality),
            "captured_at": captured_at,
        }

    @staticmethod
    def _evidence(identifier, observation_id, evidence_type, role, captured_at,
                  body, image):
        return {
            "schema_version": "4.0", "evidence_id": identifier,
            "observation_id": observation_id, "evidence_type": evidence_type,
            "evidence_role": role, "captured_at": captured_at,
            "content_type": "image/jpeg", "width": int(image.shape[1]),
            "height": int(image.shape[0]), "size_bytes": len(body),
            "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
        }

    def _eligible(self, person, frame_shape) -> bool:
        if person.class_name != "pedestrian" or person.metadata.get("track_id") is None:
            return False
        runtime = (self.camera_config.get("runtime_analytics") or {}).get("person_reid") or {}
        zones = runtime.get("zones") or []
        if not zones:
            return True
        height, width = frame_shape[:2]
        x1, y1, x2, y2 = person.bbox
        point = ((x1 + x2) / (2 * width), (y1 + y2) / (2 * height))
        return any(self._inside(point, zone.get("points") or []) for zone in zones)

    @staticmethod
    def _inside(point, points):
        polygon = [(float(p.get("x", 0)), float(p.get("y", 0))) for p in points]
        if len(polygon) < 3:
            return False
        x, y, inside = point[0], point[1], False
        previous = polygon[-1]
        for current in polygon:
            xi, yi = current
            xj, yj = previous
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
                inside = not inside
            previous = current
        return inside

    @staticmethod
    def _crop(frame, bbox):
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = [int(value) for value in bbox]
        if x2 <= x1 or y2 <= y1:
            return None
        return frame[max(0, y1):min(height, y2), max(0, x1):min(width, x2)].copy()

    def _jpeg(self, image):
        ok, encoded = cv2.imencode(
            ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        )
        if not ok:
            raise RuntimeError("could not encode Re-ID evidence")
        return encoded.tobytes()

    @staticmethod
    def _quality(image) -> float:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        area = min(1.0, image.shape[0] * image.shape[1] / 120000.0)
        sharpness = min(1.0, float(cv2.Laplacian(gray, cv2.CV_64F).var()) / 800.0)
        exposure = max(0.0, 1.0 - abs(float(gray.mean()) - 127.5) / 127.5)
        return round(0.4 * area + 0.4 * sharpness + 0.2 * exposure, 6)
