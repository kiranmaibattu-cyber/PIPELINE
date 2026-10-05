"""V4 scene samples with optional SigLIP 2 embeddings and durable evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import time
from typing import Any
import uuid

import cv2
import numpy as np

from .sentinel_delivery import SentinelV2Outbox, SentinelV2Uploader


SCENE_MODEL_ID = "google/siglip2-base-patch16-224"
SCENE_MODEL_VERSION = "75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2"
SCENE_DIMENSIONS = 768
DEFAULT_EMBEDDING_SPACE = (
    "google/siglip2-base-patch16-224@75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2:image-text:l2:768"
)


class OpenVINOSceneExtractor:
    model_id = SCENE_MODEL_ID
    model_version = SCENE_MODEL_VERSION
    dimension = SCENE_DIMENSIONS

    def __init__(self, model_path: str, device: str = "GPU") -> None:
        try:
            import openvino as ov
        except ImportError as exc:
            raise RuntimeError("OpenVINO is required for scene embeddings") from exc
        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(f"scene embedding model is missing: {path}")
        core = ov.Core()
        model = core.read_model(str(path))
        if len(model.inputs) != 1:
            raise ValueError("scene image encoder must expose exactly one image input")
        self.compiled = core.compile_model(model, device)
        self.input = self.compiled.input(0)
        self.output = self.compiled.output(0)
        output_shape = self.output.partial_shape
        static_last_dimension = (
            output_shape.rank.is_static
            and len(output_shape) > 0
            and output_shape[-1].is_static
        )
        if static_last_dimension and output_shape[-1].get_length() != self.dimension:
            raise ValueError(
                f"scene model output must end in {self.dimension}, got {output_shape}"
            )

    def warmup(self) -> None:
        self.embed(np.zeros((224, 224, 3), dtype=np.uint8))

    def embed(self, image: np.ndarray) -> np.ndarray:
        if image is None or image.size == 0:
            raise ValueError("scene image is empty")
        resized = cv2.resize(image, (224, 224), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32)
        # SigLIP image preprocessing maps [0, 255] to [-1, 1].
        tensor = ((rgb / 255.0) - 0.5) / 0.5
        tensor = np.transpose(tensor, (2, 0, 1))[None, ...]
        result = np.asarray(self.compiled([tensor])[self.output], dtype=np.float32).reshape(-1)
        if result.size != self.dimension or not np.isfinite(result).all():
            raise ValueError("scene model returned an invalid vector")
        norm = float(np.linalg.norm(result))
        if norm <= 1e-12:
            raise ValueError("scene model returned a zero vector")
        return result / norm


class SceneEmbeddingPipeline:
    def __init__(
        self,
        camera_id: str,
        edge_id: str,
        extractor: Any,
        camera_config: dict[str, Any],
    ) -> None:
        self.camera_id = camera_id
        self.edge_id = edge_id
        self.extractor = extractor
        self.camera_config = camera_config
        self.stream_session_id = str(uuid.uuid4())
        self.last_emitted = 0.0
        self.store = SentinelV2Outbox(
            Path(os.getenv("APEXFABRIC_STATE_ROOT", "/state")), camera_id
        )
        self.uploader = None
        base_url = os.getenv("SENTINEL_INGEST_BASE_URL", "").strip()
        token = os.getenv("SENTINEL_EDGE_TOKEN", "").strip()
        if base_url and token:
            self.uploader = SentinelV2Uploader(self.store, base_url, token)
            self.uploader.start()

    def process(self, packet: Any) -> int:
        config = (self.camera_config.get("analytics") or {}).get("scene_embeddings") or {}
        emission = config.get("emission") or {}
        interval = float(emission.get("interval_seconds", 10.0))
        now = time.time()
        if now - self.last_emitted < interval:
            return 0
        self.last_emitted = now

        embedding_config = config.get("embedding") or {}
        v4 = config.get("v4") or {}
        emit_embedding = "scene_embeddings" in set(v4.get("outputs") or [])
        if emit_embedding:
            self._validate_extractor(embedding_config)
        runtime = (self.camera_config.get("runtime_analytics") or {}).get("scene_embeddings") or {}
        zones = list(runtime.get("zones") or [])
        regions = zones if zones else [None]
        emitted = 0
        for zone in regions:
            image = self._region(packet.frame, zone)
            if image is None or image.size == 0:
                continue
            vector = None
            if emit_embedding:
                vector = np.asarray(self.extractor.embed(image), dtype=np.float32).reshape(-1)
                self._validate_vector(vector)
            encoded_ok, encoded = cv2.imencode(
                ".jpg", image,
                [int(cv2.IMWRITE_JPEG_QUALITY), int(emission.get("jpeg_quality", 88))],
            )
            if not encoded_ok:
                continue
            body = encoded.tobytes()
            if not 0 < len(body) <= 20 * 1024 * 1024:
                continue
            self._persist(packet, zone, vector, body, embedding_config, now)
            emitted += 1
        return emitted

    def close(self) -> None:
        if self.uploader is not None:
            self.uploader.stop()

    def _persist(
        self,
        packet: Any,
        zone: dict[str, Any] | None,
        vector: np.ndarray | None,
        body: bytes,
        embedding_config: dict[str, Any],
        now: float,
    ) -> None:
        observation_id = str(uuid.uuid4())
        evidence_id = str(uuid.uuid4())
        embedding_id = str(uuid.uuid4()) if vector is not None else None
        observed_at = datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")
        v4 = ((self.camera_config.get("analytics") or {}).get("scene_embeddings") or {}).get("v4") or {}
        observation = {
            "schema_version": "4.0",
            "observation_id": observation_id,
            "deployment_id": v4["deployment_id"],
            "camera_id": self.camera_id,
            "config_revision": int(v4["config_revision"]),
            "event_type": "scene_sample",
            "observed_at": observed_at,
            "stream_session_id": self.stream_session_id,
            "zone_id": str(zone["id"]) if zone else None,
            "evidence_id": evidence_id,
        }
        if embedding_id is not None:
            observation["embedding_id"] = embedding_id
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        evidence = {
            "schema_version": "4.0",
            "evidence_id": evidence_id,
            "observation_id": observation_id,
            "evidence_type": "frame",
            "evidence_role": "context",
            "captured_at": observed_at,
            "content_type": "image/jpeg",
            "width": int(self._region_width(packet.frame, zone)),
            "height": int(self._region_height(packet.frame, zone)),
            "size_bytes": len(body),
            "sha256": digest,
            "quality": self._quality(body),
        }
        embeddings = []
        if vector is not None:
            embeddings.append({
            "schema_version": "4.0",
            "embedding_id": embedding_id,
            "observation_id": observation_id,
            "evidence_id": evidence_id,
            "profile_id": "siglip2-base-v1",
            "kind": "scene",
            "model_id": embedding_config["model_id"],
            "model_version": embedding_config["model_version"],
            "embedding_space": "siglip2-base-v1",
            "dim": int(embedding_config["dimensions"]),
            "distance_metric": "cosine",
            "normalization": "l2",
            "vector": [float(value) for value in vector],
            "quality": evidence["quality"],
            "captured_at": observed_at,
        })
        self.store.persist(observation, [(evidence, body)], embeddings)

    def _validate_extractor(self, config: dict[str, Any]) -> None:
        expected = (
            (self.extractor.model_id, config.get("model_id")),
            (self.extractor.model_version, config.get("model_version")),
            (int(self.extractor.dimension), int(config.get("dimensions", 0))),
        )
        if any(actual != configured for actual, configured in expected):
            raise ValueError("configured scene embedding space does not match the loaded model")

    @staticmethod
    def _validate_vector(vector: np.ndarray) -> None:
        if vector.size != SCENE_DIMENSIONS or not np.isfinite(vector).all():
            raise ValueError("scene embedding must contain 768 finite values")
        if not np.isclose(float(np.linalg.norm(vector)), 1.0, atol=1e-4):
            raise ValueError("scene embedding must be L2 normalized")

    @staticmethod
    def _bounds(frame: np.ndarray, zone: dict[str, Any] | None) -> tuple[int, int, int, int]:
        height, width = frame.shape[:2]
        if not zone:
            return 0, 0, width, height
        points = zone.get("points") or []
        if not points:
            return 0, 0, width, height
        xs = [float(point.get("x", 0)) for point in points]
        ys = [float(point.get("y", 0)) for point in points]
        normalized = bool(zone.get("normalized", True))
        if normalized:
            xs = [value * width for value in xs]
            ys = [value * height for value in ys]
        x1, x2 = max(0, int(min(xs))), min(width, int(max(xs)))
        y1, y2 = max(0, int(min(ys))), min(height, int(max(ys)))
        return x1, y1, x2, y2

    @classmethod
    def _region(cls, frame: np.ndarray, zone: dict[str, Any] | None) -> np.ndarray:
        x1, y1, x2, y2 = cls._bounds(frame, zone)
        return frame[y1:y2, x1:x2]

    @classmethod
    def _region_width(cls, frame: np.ndarray, zone: dict[str, Any] | None) -> int:
        x1, _, x2, _ = cls._bounds(frame, zone)
        return x2 - x1

    @classmethod
    def _region_height(cls, frame: np.ndarray, zone: dict[str, Any] | None) -> int:
        _, y1, _, y2 = cls._bounds(frame, zone)
        return y2 - y1

    @staticmethod
    def _quality(body: bytes) -> float:
        image = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None or image.size == 0:
            return 0.0
        sharpness = min(1.0, float(cv2.Laplacian(image, cv2.CV_64F).var()) / 1000.0)
        exposure = max(0.0, 1.0 - abs(float(image.mean()) - 127.5) / 127.5)
        return round(0.7 * sharpness + 0.3 * exposure, 6)
