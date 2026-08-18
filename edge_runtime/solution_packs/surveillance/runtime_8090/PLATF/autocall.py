"""Outbound call trigger for unauthorised-person alerts.

This module intentionally stays on the PLATF side of the boundary: it listens to
PLATF events and asks the Auto-Caller service to place the Vobiz call. The live
detector thread must never wait on telephony/network work, so event handling only
enqueues a job and a background worker performs the HTTP request.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = {
    "enabled": False,
    "endpoint": "http://127.0.0.1:8080/api/trigger",
    "cooldown_s": 300,
    "org": "Zone Monitoring Desk",
    "recipients": [
        {
            "name": "Security Control",
            "phone": "",
        }
    ],
}


class AutoCallDispatcher:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        log_path: str | Path | None = None,
        outcome_callback=None,
    ):
        self.config = _merged_config(config or {})
        self.log_path = Path(log_path) if log_path else Path(__file__).resolve().parent / "autocall_attempts.jsonl"
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
        self._last_call: dict[tuple[str, str], float] = {}
        self._outcome_callback = outcome_callback
        self._calls: dict[str, dict[str, Any]] = {}
        self._completed: set[str] = set()
        self._lock = threading.RLock()
        self._worker = threading.Thread(target=self._run, name="platf-autocall", daemon=True)
        self._worker.start()
        self._poller = threading.Thread(target=self._poll_outcomes, name="platf-autocall-outcomes", daemon=True)
        self._poller.start()

    def on_alert(self, event) -> None:
        """EventBus callback. Keep this fast and exception-free."""
        if not self.config.get("enabled"):
            return
        if getattr(event, "type", "") != "unauthorised":
            return

        payload = getattr(event, "payload", None) or {}
        employee = str(payload.get("employee_id") or "unknown").strip()
        camera = str(getattr(event, "camera", "") or "unknown").strip()
        key = (employee, camera)
        now = time.time()
        cooldown = float(self.config.get("cooldown_s") or 0)
        if now - self._last_call.get(key, 0.0) < cooldown:
            return

        recipients = [r for r in self.config.get("recipients", []) if str(r.get("phone", "")).strip()]
        if not recipients:
            self._record("skipped", event, None, "no recipient phone configured")
            return

        self._last_call[key] = now
        job = {
            "event": _event_dict(event),
            "employee": employee,
            "camera": camera,
            "recipients": recipients,
        }
        try:
            self._q.put_nowait(job)
        except queue.Full:
            self._record("skipped", event, None, "autocall queue full")

    def _run(self) -> None:
        while True:
            job = self._q.get()
            try:
                for recipient in job["recipients"]:
                    self._trigger(job, recipient)
            finally:
                self._q.task_done()

    def _trigger(self, job: dict[str, Any], recipient: dict[str, Any]) -> None:
        endpoint = str(self.config.get("endpoint") or "").strip()
        if not endpoint:
            self._record("failed", job["event"], recipient, "missing autocall endpoint")
            return

        body = {
            "to": str(recipient.get("phone", "")).strip(),
            "name": str(recipient.get("name", "")).strip(),
            "org": str(self.config.get("org") or DEFAULT_CONFIG["org"]),
            "reminder": self._reminder(job),
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                self._remember_call(job, recipient, raw)
                self._record("queued", job["event"], recipient, raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self._record("failed", job["event"], recipient, f"HTTP {exc.code}: {detail}")
        except Exception as exc:
            self._record("failed", job["event"], recipient, str(exc))

    def _remember_call(self, job: dict[str, Any], recipient: dict[str, Any], raw: str) -> None:
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return
        call_id = (
            data.get("call_id")
            or (data.get("result") or {}).get("request_uuid")
            or (data.get("result") or {}).get("call_id")
        )
        if not call_id:
            return
        with self._lock:
            self._calls[str(call_id)] = {
                "event": job["event"],
                "recipient": dict(recipient),
                "queued_at": time.time(),
            }

    def _poll_outcomes(self) -> None:
        while True:
            time.sleep(float(self.config.get("outcome_poll_s") or 5))
            if self._outcome_callback is None:
                continue
            with self._lock:
                pending = set(self._calls) - set(self._completed)
            if not pending:
                continue
            outcomes_url = self._outcomes_url()
            if not outcomes_url:
                continue
            try:
                with urllib.request.urlopen(outcomes_url, timeout=5) as resp:
                    rows = json.loads(resp.read().decode("utf-8", errors="replace") or "[]")
            except Exception as exc:
                self._record("outcome_poll_failed", {}, None, str(exc))
                continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                call_id = str(row.get("call_id") or row.get("call_sid") or "")
                if not call_id or call_id not in pending:
                    continue
                outcome = row.get("outcome") or {}
                with self._lock:
                    source = self._calls.get(call_id)
                    self._completed.add(call_id)
                if not source:
                    continue
                try:
                    self._outcome_callback(call_id, source, outcome, row)
                except Exception as exc:
                    self._record("outcome_callback_failed", source.get("event", {}), source.get("recipient"), str(exc))

    def _outcomes_url(self) -> str:
        explicit = str(self.config.get("outcomes_endpoint") or "").strip()
        if explicit:
            return explicit
        endpoint = str(self.config.get("endpoint") or "").strip()
        if endpoint.endswith("/api/trigger"):
            return endpoint[: -len("/api/trigger")] + "/api/outcomes"
        return ""

    def annotate_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach latest call status to matching unauthorised alert rows.

        This reads the append-only attempt log plus Auto-Caller's outcome API, so it
        also covers calls that finished before the current PLATF process started.
        """
        if not events:
            return events
        outcomes = self._latest_outcomes()
        if not outcomes:
            return events
        by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        for attempt in self._attempt_rows():
            event = attempt.get("event") or {}
            call_id = self._attempt_call_id(attempt)
            outcome = outcomes.get(call_id)
            if not outcome:
                continue
            payload = event.get("payload") or {}
            key = (
                str(payload.get("employee_id") or "").lower(),
                str(event.get("camera") or ""),
                _event_time_bucket(event.get("t")),
            )
            by_key[key] = {
                "call_id": call_id,
                "status": (outcome.get("outcome") or {}).get("status") or "unclear",
                "note": (outcome.get("outcome") or {}).get("note") or "",
            }
        for event in events:
            if event.get("type") != "unauthorised":
                continue
            payload = event.setdefault("payload", {})
            key = (
                str(payload.get("employee_id") or event.get("name") or "").lower(),
                str(event.get("camera") or ""),
                _event_time_bucket(event.get("t")),
            )
            call = by_key.get(key)
            if call:
                payload["autocall_status"] = (
                    "investigated" if call["status"] == "acknowledged" else call["status"]
                )
                payload["autocall_note"] = call["note"]
                payload["autocall_id"] = call["call_id"]
        return events

    def _latest_outcomes(self) -> dict[str, dict[str, Any]]:
        url = self._outcomes_url()
        if not url:
            return {}
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                rows = json.loads(resp.read().decode("utf-8", errors="replace") or "[]")
        except Exception:
            return {}
        out = {}
        if isinstance(rows, list):
            for row in rows:
                call_id = str(row.get("call_id") or row.get("call_sid") or "")
                if call_id:
                    out[call_id] = row
        return out

    def _attempt_rows(self) -> list[dict[str, Any]]:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines[-500:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") == "queued":
                rows.append(row)
        return rows

    @staticmethod
    def _attempt_call_id(attempt: dict[str, Any]) -> str:
        try:
            detail = json.loads(attempt.get("detail") or "{}")
        except json.JSONDecodeError:
            return ""
        return str(
            detail.get("call_id")
            or (detail.get("result") or {}).get("request_uuid")
            or (detail.get("result") or {}).get("call_id")
            or ""
        )

    def _reminder(self, job: dict[str, Any]) -> str:
        employee = job["employee"] or "an unauthorised person"
        camera = job["camera"] or "unknown camera"
        event = job["event"]
        gid = event.get("person_id")
        parts = [
            f"Unauthorised person {employee} on {camera}.",
            "Check 8090 now.",
        ]
        if gid is not None:
            parts.append(f"ID P{gid}.")
        return " ".join(parts)

    def _record(self, status: str, event: Any, recipient: dict[str, Any] | None, detail: str) -> None:
        row = {
            "wall": round(time.time(), 3),
            "status": status,
            "event": _event_dict(event) if not isinstance(event, dict) else event,
            "recipient": recipient or {},
            "detail": detail,
        }
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _merged_config(config: dict[str, Any]) -> dict[str, Any]:
    out = dict(DEFAULT_CONFIG)
    out.update({k: v for k, v in config.items() if v is not None})
    if not isinstance(out.get("recipients"), list):
        out["recipients"] = []
    return out


def _event_dict(event) -> dict[str, Any]:
    if isinstance(event, dict):
        return dict(event)
    if hasattr(event, "as_dict"):
        return event.as_dict()
    return {
        "type": getattr(event, "type", None),
        "t": getattr(event, "t", None),
        "camera": getattr(event, "camera", None),
        "person_id": getattr(event, "person_id", None),
        "zone": getattr(event, "zone", None),
        "payload": getattr(event, "payload", None) or {},
    }


def _event_time_bucket(value: Any) -> str:
    try:
        return str(round(float(value), 1))
    except (TypeError, ValueError):
        return ""
