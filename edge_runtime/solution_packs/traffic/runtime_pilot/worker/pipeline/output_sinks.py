from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
from contextlib import suppress
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Protocol

if TYPE_CHECKING:
    from .types import FramePacket

logger = logging.getLogger(__name__)
ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*))?\}$")


class AnalyticsSink(Protocol):
    def publish(self, payload: dict) -> None:
        ...

    def close(self) -> None:
        ...


def json_payload(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        match = ENV_PATTERN.match(value)
        if match:
            return os.getenv(match.group(1), match.group(2) or "")
        return value
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    return value


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


class MultiSink:
    def __init__(self, sinks: list[AnalyticsSink]):
        self.sinks = sinks

    def publish(self, payload: dict) -> None:
        errors = []
        for sink in self.sinks:
            try:
                sink.publish(payload)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("; ".join(errors))

    def close(self) -> None:
        for sink in self.sinks:
            with suppress(Exception):
                sink.close()


class RedisStreamSink:
    def __init__(
        self,
        host: str = "redis",
        port: int = 6379,
        password: str | None = None,
        stream_key: str = "traffic:analytics",
        maxlen: int = 10000,
    ):
        import redis

        self.stream_key = stream_key
        self.maxlen = maxlen
        self.client = redis.Redis(
            host=host,
            port=port,
            password=password,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
            decode_responses=True,
        )

    @classmethod
    def from_env(cls) -> "RedisStreamSink":
        return cls(
            host=os.getenv("ANALYTICS_REDIS_HOST") or os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("ANALYTICS_REDIS_PORT") or os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("ANALYTICS_REDIS_PASSWORD") or os.getenv("REDIS_PASSWORD"),
            stream_key=os.getenv("ANALYTICS_REDIS_STREAM", "traffic:analytics"),
            maxlen=int(os.getenv("ANALYTICS_REDIS_MAXLEN", "10000")),
        )

    def publish(self, payload: dict) -> None:
        self.client.xadd(
            self.stream_key,
            {"payload": json_payload(payload)},
            maxlen=self.maxlen,
            approximate=True,
        )

    def close(self) -> None:
        self.client.close()


class MqttSink:
    def __init__(
        self,
        host: str = "mqtt",
        port: int = 1883,
        topic: str = "traffic/analytics",
        qos: int = 0,
        retain: bool = False,
        client_id: str | None = None,
        auth: Mapping[str, Any] | None = None,
    ):
        import paho.mqtt.client as mqtt

        auth = auth or {}
        self.topic = topic
        self.qos = int(qos)
        self.retain = bool(retain)
        self.client = mqtt.Client(client_id=client_id or "")
        username = optional_str(auth.get("username"))
        password = optional_str(auth.get("password"))
        if username:
            self.client.username_pw_set(username, password=password)
        if truthy(auth.get("tls")):
            self.client.tls_set()
        self.client.connect(host, int(port), keepalive=30)
        self.client.loop_start()

    def publish(self, payload: dict) -> None:
        info = self.client.publish(
            self.topic,
            payload=json_payload(payload),
            qos=self.qos,
            retain=self.retain,
        )
        if info.rc:
            raise RuntimeError(f"mqtt publish failed rc={info.rc}")

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


class KafkaSink:
    def __init__(
        self,
        bootstrap_servers: str | list[str] = "kafka:9092",
        topic: str = "traffic.analytics",
        auth: Mapping[str, Any] | None = None,
    ):
        from kafka import KafkaProducer

        auth = auth or {}
        kwargs = {
            "bootstrap_servers": bootstrap_servers,
            "value_serializer": lambda value: json_payload(value).encode("utf-8"),
            "key_serializer": lambda value: str(value).encode("utf-8") if value is not None else None,
            "linger_ms": 10,
        }
        security_protocol = optional_str(auth.get("security_protocol"))
        if security_protocol:
            kwargs["security_protocol"] = security_protocol
        sasl_mechanism = optional_str(auth.get("sasl_mechanism"))
        username = optional_str(auth.get("username"))
        password = optional_str(auth.get("password"))
        if sasl_mechanism:
            kwargs["sasl_mechanism"] = sasl_mechanism
        if username:
            kwargs["sasl_plain_username"] = username
        if password:
            kwargs["sasl_plain_password"] = password
        self.topic = topic
        self.producer = KafkaProducer(**kwargs)

    def publish(self, payload: dict) -> None:
        key = ((payload.get("source") or {}).get("id") or (payload.get("source") or {}).get("name"))
        self.producer.send(self.topic, key=key, value=payload)

    def close(self) -> None:
        self.producer.flush(timeout=2)
        self.producer.close(timeout=2)


class RabbitMqSink:
    def __init__(
        self,
        host: str = "rabbitmq",
        port: int = 5672,
        exchange: str = "traffic.analytics",
        exchange_type: str = "topic",
        routing_key: str = "camera.observation",
        auth: Mapping[str, Any] | None = None,
    ):
        import pika

        auth = auth or {}
        credentials = pika.PlainCredentials(
            optional_str(auth.get("username")) or "guest",
            optional_str(auth.get("password")) or "guest",
        )
        parameters = pika.ConnectionParameters(
            host=host,
            port=int(port),
            virtual_host=optional_str(auth.get("virtual_host")) or "/",
            credentials=credentials,
            heartbeat=30,
            blocked_connection_timeout=1,
        )
        self.exchange = exchange
        self.routing_key = routing_key
        self.connection = pika.BlockingConnection(parameters)
        self.channel = self.connection.channel()
        self.channel.exchange_declare(
            exchange=exchange,
            exchange_type=exchange_type,
            durable=True,
        )

    def publish(self, payload: dict) -> None:
        self.channel.basic_publish(
            exchange=self.exchange,
            routing_key=self.routing_key,
            body=json_payload(payload).encode("utf-8"),
            properties=None,
        )

    def close(self) -> None:
        self.connection.close()


class AzureEventHubSink:
    def __init__(
        self,
        connection_string: str,
        eventhub_name: str | None = None,
    ):
        from azure.eventhub import EventData, EventHubProducerClient

        if not connection_string:
            raise ValueError("azure_event_hub connection_string is required")
        self.EventData = EventData
        self.client = EventHubProducerClient.from_connection_string(
            conn_str=connection_string,
            eventhub_name=optional_str(eventhub_name),
        )

    def publish(self, payload: dict) -> None:
        batch = self.client.create_batch()
        batch.add(self.EventData(json_payload(payload)))
        self.client.send_batch(batch)

    def close(self) -> None:
        self.client.close()


def sink_from_config(config: Mapping[str, Any]) -> AnalyticsSink:
    config = expand_env(dict(config or {}))
    sink_type = str(config.get("type") or "redis").lower()
    auth = config.get("auth") or {}
    if sink_type == "redis":
        return RedisStreamSink(
            host=config.get("host", "redis"),
            port=int(config.get("port", 6379)),
            password=optional_str(auth.get("password")),
            stream_key=config.get("stream") or config.get("stream_key") or "traffic:analytics",
            maxlen=int(config.get("maxlen", 10000)),
        )
    if sink_type == "mqtt":
        return MqttSink(
            host=config.get("host", "mqtt"),
            port=int(config.get("port", 1883)),
            topic=config.get("topic", "traffic/analytics"),
            qos=int(config.get("qos", 0)),
            retain=truthy(config.get("retain", False)),
            client_id=optional_str(config.get("client_id")),
            auth=auth,
        )
    if sink_type == "kafka":
        return KafkaSink(
            bootstrap_servers=config.get("bootstrap_servers", "kafka:9092"),
            topic=config.get("topic", "traffic.analytics"),
            auth=auth,
        )
    if sink_type == "rabbitmq":
        return RabbitMqSink(
            host=config.get("host", "rabbitmq"),
            port=int(config.get("port", 5672)),
            exchange=config.get("exchange", "traffic.analytics"),
            exchange_type=config.get("exchange_type", "topic"),
            routing_key=config.get("routing_key", "camera.observation"),
            auth=auth,
        )
    if sink_type in {"azure_event_hub", "azure-event-hub", "eventhub"}:
        return AzureEventHubSink(
            connection_string=config.get("connection_string", ""),
            eventhub_name=config.get("eventhub_name"),
        )
    raise ValueError(f"Unsupported analytics output type: {sink_type}")


_SINK_KINDS = {
    "RedisStreamSink": "redis",
    "MqttSink": "mqtt",
    "KafkaSink": "kafka",
    "RabbitMqSink": "rabbitmq",
    "AzureEventHubSink": "azure_event_hub",
}


def sink_kinds(sink: AnalyticsSink | None) -> list[str]:
    """Flatten a sink (or MultiSink) into transport-kind strings for the metrics
    heartbeat, e.g. ["azure_event_hub", "redis"]."""
    if sink is None:
        return []
    if isinstance(sink, MultiSink):
        kinds: list[str] = []
        for member in sink.sinks:
            kinds.extend(sink_kinds(member))
        return kinds
    return [_SINK_KINDS.get(type(sink).__name__, type(sink).__name__)]


def build_analytics_sink(
    worker_config: Mapping[str, Any] | None,
    *,
    redis_host: str,
    redis_port: int,
    redis_password: str | None = None,
    stream_key: str = "traffic:analytics",
    redis_maxlen: int = 10000,
) -> AnalyticsSink | None:
    """Build the analytics sink set: the product outputs configured under
    json_streaming.outputs (Azure Event Hub, Kafka, MQTT, …) PLUS an always-on
    Redis 'debug tap' so the operator UI never goes blind — even when the only
    configured product sink is Azure Event Hub. Disable the tap with
    json_streaming.debug_tap=false or DISABLE_DEBUG_TAP=1."""
    streaming = (worker_config or {}).get("json_streaming") or {}
    if not truthy(streaming.get("enabled", True)):
        logger.info("Analytics JSON streaming disabled by config")
        return None

    sinks: list[AnalyticsSink] = []
    for output in streaming.get("outputs") or []:
        output = output or {}
        if not truthy(output.get("enabled", False)):
            continue
        # Redis is always provided by the local debug tap below, with the host
        # resolved per-deployment via REDIS_HOST (localhost on host-native, the
        # 'redis' service name under Docker). Skip any configured redis output so
        # a Docker-oriented host string doesn't break a host-native deploy.
        if str(output.get("type") or "redis").lower() == "redis":
            continue
        try:
            sinks.append(sink_from_config(output))
            logger.info(
                "Analytics output enabled: %s (%s)",
                output.get("name") or output.get("type"),
                output.get("type"),
            )
        except Exception as exc:
            logger.warning(
                "Analytics output disabled after init failure: %s (%s)",
                output.get("name") or output.get("type"),
                exc,
            )

    tap_enabled = truthy(streaming.get("debug_tap", True)) and not truthy(os.getenv("DISABLE_DEBUG_TAP"))
    if tap_enabled and not any(isinstance(s, RedisStreamSink) for s in sinks):
        try:
            sinks.append(
                RedisStreamSink(
                    host=redis_host,
                    port=redis_port,
                    password=redis_password,
                    stream_key=stream_key,
                    maxlen=redis_maxlen,
                )
            )
            logger.info("Analytics debug tap -> redis stream %s", stream_key)
        except Exception as exc:
            logger.warning("Analytics debug tap unavailable: %s", exc)

    if not sinks:
        logger.warning("No analytics outputs enabled")
        return None
    return sinks[0] if len(sinks) == 1 else MultiSink(sinks)


class AsyncAnalyticsDispatcher:
    def __init__(
        self,
        sink: AnalyticsSink | None = None,
        camera_configs: Mapping[str, dict] | None = None,
        max_queue_size: int = 1000,
    ):
        self.sink = sink
        self.camera_configs = camera_configs or {}
        self.local_sink = self._local_sink_from_env()
        self.queue: queue.Queue[dict | None] = queue.Queue(maxsize=max_queue_size)
        self.dropped = 0          # payloads dropped because the queue was full (backpressure)
        self.published = 0        # payloads the sink accepted
        self.publish_errors = 0   # sink.publish() raised
        self.last_error: str | None = None
        self.sequence_by_camera: dict[str, int] = {}
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="analytics-output",
            daemon=True,
        )

    def start(self) -> "AsyncAnalyticsDispatcher":
        if self.sink is None:
            logger.info("Analytics output dispatcher disabled")
            return self
        self._thread.start()
        return self

    def publish_packets(self, packets: Iterable[FramePacket]) -> None:
        packets = list(packets)
        if self.local_sink is not None:
            for packet in packets:
                events = simple_events(packet.analytics_events)
                self.local_sink.publish_packet(packet, events)
        if self.sink is None:
            return
        for packet in packets:
            payload = camera_payload(
                packet,
                self.camera_configs.get(packet.name) or {},
                self._next_sequence(packet.name),
            )
            try:
                self.queue.put_nowait(payload)
            except queue.Full:
                self.dropped += 1

    def close(self) -> None:
        if self.sink is None:
            return
        self._stop_event.set()
        with suppress(queue.Full):
            self.queue.put(None, timeout=0.5)
        self._thread.join(timeout=2.0)
        self.sink.close()
        if self.dropped:
            logger.warning("Analytics output dropped %d payload(s)", self.dropped)

    def _run(self) -> None:
        while True:
            try:
                payload = self.queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            if payload is None:
                return
            try:
                self.sink.publish(payload)
                self.published += 1
            except Exception as exc:
                self.publish_errors += 1
                self.last_error = str(exc)
                logger.warning("Analytics output publish failed: %s", exc)

    def _next_sequence(self, camera_name: str) -> int:
        value = self.sequence_by_camera.get(camera_name, 0) + 1
        self.sequence_by_camera[camera_name] = value
        return value

    @staticmethod
    def _local_sink_from_env():
        try:
            from .local_event_sink import LocalManagementEventSink
            return LocalManagementEventSink.from_env()
        except Exception as exc:
            logger.warning("Local management event sink disabled: %s", exc)
            return None

    def stats(self) -> dict:
        """Snapshot for the worker_metrics heartbeat (see monitor.WorkerMetricsMonitor).
        Raw cumulative counters; fps deltas are computed by the caller."""
        return {
            "queue_depth": self.queue.qsize(),
            "queue_max": self.queue.maxsize,
            "dropped": self.dropped,
            "published": self.published,
            "errors": self.publish_errors,
            "last_error": self.last_error,
            "sinks": sink_kinds(self.sink),
            "local_event_sink": self.local_sink is not None,
            "sequence_by_camera": dict(self.sequence_by_camera),
        }


def tracked_objects(packet: FramePacket) -> list[dict]:
    objects = []
    plates_by_parent: dict[Any, list[dict]] = {}
    for index, detection in enumerate(packet.detections):
        if detection.model_name != "license_plate":
            continue
        if not detection.metadata.get("reported"):
            continue
        parent_id = detection.parent_id
        if parent_id is None:
            continue
        plate = {
            "id": f"plate:{parent_id}:{index}",
            "text": str(detection.metadata.get("ocr_text") or "").strip() or None,
            "confidence": round(float(detection.confidence), 4),
            "bbox": bbox_payload(detection.bbox),
            "center": center_payload(detection.bbox),
        }
        plates_by_parent.setdefault(parent_id, []).append(plate)

    for detection in packet.detections:
        track_id = detection.metadata.get("track_id")
        if detection.model_name != "vehicle" or track_id is None:
            continue
        attributes = {
            "track_age": detection.metadata.get("track_age"),
            "track_hits": detection.metadata.get("track_hits"),
            "track_missed": detection.metadata.get("track_missed"),
            "predicted": bool(detection.metadata.get("predicted")),
            "zones": zone_memberships(detection.metadata.get("zones", {})),
        }
        object_use_cases = object_use_case_payload(detection.metadata.get("use_cases", {}))
        if object_use_cases:
            attributes["use_cases"] = object_use_cases
            attributes["violations"] = [
                item for item in object_use_cases if item.get("violation")
            ]
        plates = plates_by_parent.get(track_id) or []
        if plates:
            attributes["license_plates"] = plates
        objects.append(
            {
                "id": track_id,
                "kind": "tracked_object",
                "class": detection.class_name,
                "confidence": round(float(detection.confidence), 4),
                "bbox": bbox_payload(detection.bbox),
                "center": center_payload(detection.bbox),
                "attributes": attributes,
            }
        )
    return objects


def bbox_payload(bbox: list[int] | tuple[int, int, int, int] | None) -> dict:
    if not bbox or len(bbox) < 4:
        return {"x1": None, "y1": None, "x2": None, "y2": None, "width": None, "height": None}
    x1, y1, x2, y2 = [int(value) for value in bbox[:4]]
    return {
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "width": max(0, x2 - x1),
        "height": max(0, y2 - y1),
    }


def center_payload(bbox: list[int] | tuple[int, int, int, int] | None) -> dict:
    if not bbox or len(bbox) < 4:
        return {"x": None, "y": None}
    x1, y1, x2, y2 = [float(value) for value in bbox[:4]]
    return {
        "x": (x1 + x2) / 2.0,
        "y": (y1 + y2) / 2.0,
    }


def zone_memberships(zones_by_use_case: Mapping[str, list]) -> list[dict]:
    memberships = []
    for use_case, zones in zones_by_use_case.items():
        for zone in zones:
            memberships.append(
                {
                    "use_case": use_case,
                    "id": zone.get("id"),
                    "label": zone.get("label"),
                    "name": zone.get("name"),
                }
            )
    return memberships


def object_use_case_payload(use_cases: Mapping[str, dict]) -> list[dict]:
    payload = []
    for name, state in (use_cases or {}).items():
        item = {
            "name": name,
            "state": state.get("state"),
            "violation": bool(state.get("violation")),
        }
        if state.get("event_type"):
            item["event_type"] = state.get("event_type")
        if state.get("location"):
            location = state.get("location") or {}
            item["location"] = {
                "type": location.get("type"),
                **geometry_payload(location),
            }
        if state.get("count"):
            item["count"] = state.get("count")
        if state.get("duration_seconds") is not None:
            item["duration_seconds"] = float(state.get("duration_seconds") or 0.0)
        payload.append(item)
    return payload


def geometry_payload(geometry: Mapping[str, Any]) -> dict:
    return {
        "id": geometry.get("id"),
        "label": geometry.get("label"),
        "name": geometry.get("name"),
    }


def direction_payload(direction: str, count: int) -> dict:
    parts = str(direction or "").split("_to_", 1)
    payload = {
        "key": direction,
        "count": int(count or 0),
    }
    if len(parts) == 2:
        payload["from"] = parts[0]
        payload["to"] = parts[1]
    return payload


def analytics_payload(use_case_state: Mapping[str, dict]) -> dict:
    global_use_cases = []
    for key, state in use_case_state.items():
        lines = []
        for item in state.get("geometry") or []:
            geometry = item.get("geometry") or {}
            if geometry.get("type") == "line":
                if "count" not in item:
                    continue
                directions = item.get("directions") or {}
                lines.append(
                    {
                        "line": geometry_payload(geometry),
                        "total": item.get("count", 0),
                        "directions": [
                            direction_payload(direction, count)
                            for direction, count in directions.items()
                        ],
                    }
                )
        if lines:
            global_use_cases.append(
                {
                    "name": key,
                    "scope": "camera",
                    "type": "aggregate_counter",
                    "lines": lines,
                }
            )
    return {
        "use_cases": global_use_cases,
    }


def simple_event(event: Mapping[str, object]) -> dict:
    subject = event.get("subject") if isinstance(event.get("subject"), Mapping) else {}
    geometry = event.get("geometry") if isinstance(event.get("geometry"), Mapping) else {}
    observed_at = event.get("observed_at")
    track_id = subject.get("track_id") or subject.get("parent_track_id")
    payload = {
        "id": event.get("observation_id"),
        "event_type": event.get("type"),
        "type": event.get("type"),
        "use_case": event.get("use_case"),
        "object_id": track_id,
        "timestamp": observed_at,
        "observed_at": observed_at,
        "subject": {
            "track_id": track_id,
            "parent_track_id": subject.get("parent_track_id"),
            "type": subject.get("class"),
            "confidence": subject.get("confidence"),
            "bbox": bbox_payload(subject.get("bbox")),
            "center": center_payload(subject.get("bbox")),
        },
        "details": {},
    }
    if geometry:
        payload["location"] = {
            "type": geometry.get("type"),
            **geometry_payload(geometry),
        }
        if geometry.get("type") == "zone":
            payload["details"]["zone_id"] = geometry.get("id")
            payload["details"]["zone_name"] = geometry.get("name") or geometry.get("label")
        elif geometry.get("type") == "line":
            payload["details"]["line_id"] = geometry.get("id")
            payload["details"]["line_name"] = geometry.get("name") or geometry.get("label")
    count = {}
    if event.get("value") is not None:
        count["total"] = event.get("value")
        payload["details"]["total"] = event.get("value")
    if event.get("count_mode"):
        count["mode"] = event.get("count_mode")
        payload["details"]["count_mode"] = event.get("count_mode")
    if event.get("direction"):
        direction = direction_payload(
            str(event.get("direction")),
            int(event.get("direction_count") or 0),
        )
        count["direction"] = direction
        payload["details"]["direction"] = direction
    if count:
        payload["count"] = count
    if event.get("plate_text"):
        payload["plate"] = {"text": event.get("plate_text")}
        payload["details"]["plate_text"] = event.get("plate_text")
    if event.get("duration_seconds") is not None:
        duration = float(event.get("duration_seconds") or 0.0)
        payload["duration_seconds"] = duration
        payload["details"]["duration_seconds"] = duration
    if not payload["details"]:
        payload.pop("details")
    return payload


def simple_events(events: list[dict]) -> list[dict]:
    return [simple_event(event) for event in events]


def camera_payload(
    packet: FramePacket,
    camera_config: Mapping[str, object] | None = None,
    sequence: int = 0,
) -> dict:
    camera_config = camera_config or {}
    now = datetime.now(timezone.utc).isoformat()
    use_case_state = packet.analytics_state.get("use_cases", {})
    objects = tracked_objects(packet)
    events = simple_events(packet.analytics_events)
    frame_height, frame_width = packet.frame.shape[:2]
    return {
        "schema_version": "5.0",
        "message_type": "camera_analytics",
        "message_id": f"{packet.name}:{sequence}:{now}",
        "sequence": sequence,
        "observed_at": now,
        "camera": {
            "id": camera_config.get("camera_id"),
            "name": packet.name,
        },
        "frame": {
            "index": packet.index,
            "observed_at": now,
            "resolution": {
                "width": frame_width,
                "height": frame_height,
            },
        },
        "worker": os.getenv("WORKER_NAME", "traffic_analysis_worker"),
        "summary": {
            "objects": len(objects),
            "events": len(events),
        },
        "objects": objects,
        "camera_analytics": analytics_payload(use_case_state),
        "events": events,
    }
