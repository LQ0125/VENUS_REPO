"""Read-only operational state for the VENUS monitoring dashboard.

This module observes the existing Core, MQTT, safety, and command paths. It
does not submit commands and never mutates hardware state.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any


SENSOR_NODE = "venus/living_room/sensor_node_01"
ACTUATOR_NODE = "venus/living_room/actuator_node_01"


def utc_timestamp(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(
        epoch if epoch is not None else time.time(),
        tz=timezone.utc,
    ).isoformat()


class MonitoringState:
    """Collect dashboard-facing state without becoming a control plane."""

    SCHEMA_VERSION = 1
    ONLINE_AFTER_SECONDS = 3.5
    OFFLINE_AFTER_SECONDS = 10.0

    def __init__(self, history_limit: int = 100):
        self.started_at = time.time()
        self.services: dict[str, dict[str, Any]] = {
            "core": self._service_state(True),
            "mqtt": self._service_state(False),
            "voice_sidecar": self._service_state(False),
        }
        self.sidecar_connections = 0
        self.voice: dict[str, Any] = {
            "microphone_enabled": False,
            "state": "muted",
            "controller_online": False,
            "controller_id": "voice_remote_01",
            "changed_by": "core_startup",
            "updated_at": utc_timestamp(),
        }
        self.box_last_seen: dict[str, float | None] = {
            "box_1": None,
            "box_2": None,
        }
        self.box_fields: dict[str, dict[str, Any]] = {
            "box_1": {},
            "box_2": {},
        }
        self.active_safety: dict[str, dict[str, Any]] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self.commands: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self._listeners: set[asyncio.Queue] = set()

    @staticmethod
    def _service_state(online: bool) -> dict[str, Any]:
        now = time.time()
        return {
            "online": online,
            "updated_at": now,
            "updated_at_iso": utc_timestamp(now),
        }

    def set_service(self, name: str, online: bool, **details: Any) -> None:
        previous = self.services.get(name, {}).get("online")
        if previous == online and not details:
            return

        state = self._service_state(online)
        state.update(details)
        self.services[name] = state
        self._notify("service_status", {"service": name, **state})

    def sidecar_connected(self) -> None:
        self.sidecar_connections += 1
        self.set_service(
            "voice_sidecar",
            True,
            connections=self.sidecar_connections,
        )

    def sidecar_disconnected(self) -> None:
        self.sidecar_connections = max(0, self.sidecar_connections - 1)
        self.set_service(
            "voice_sidecar",
            self.sidecar_connections > 0,
            connections=self.sidecar_connections,
        )
        if self.sidecar_connections == 0:
            self.update_voice_status(
                microphone_enabled=False,
                controller_online=False,
                controller_id=self.voice.get(
                    "controller_id",
                    "voice_remote_01",
                ),
                changed_by="sidecar_disconnected",
            )

    def update_voice_status(
        self,
        *,
        microphone_enabled: bool,
        controller_online: bool,
        controller_id: str,
        changed_by: str,
    ) -> None:
        """Store the sidecar-confirmed voice state for dashboard display."""
        self.voice = {
            "microphone_enabled": bool(microphone_enabled),
            "state": "listening" if microphone_enabled else "muted",
            "controller_online": bool(controller_online),
            "controller_id": controller_id,
            "changed_by": changed_by,
            "updated_at": utc_timestamp(),
        }
        self._notify("voice_status", copy.deepcopy(self.voice))

    def record_telemetry(self, node_path: str, telemetry: dict[str, Any]) -> None:
        now = time.time()
        sensors = telemetry.get("sensors", {})
        actuators = telemetry.get("actuators", {})
        details = telemetry.get("details", {})

        touched_boxes: set[str] = set()

        if sensors or "buzzer" in actuators:
            touched_boxes.add("box_1")
            self.box_fields["box_1"].update(sensors)
            if "buzzer" in actuators:
                self.box_fields["box_1"]["buzzer"] = bool(
                    actuators["buzzer"]
                )

        if (
            "led" in actuators
            or "servo" in actuators
            or "light_mode" in details
            or "door_angle" in details
        ):
            touched_boxes.add("box_2")
            if "led" in actuators:
                self.box_fields["box_2"]["led"] = bool(actuators["led"])
            if "servo" in actuators:
                self.box_fields["box_2"]["servo"] = bool(
                    actuators["servo"]
                )
            if "light_mode" in details:
                self.box_fields["box_2"]["light_mode"] = details[
                    "light_mode"
                ]
            if "door_angle" in details:
                self.box_fields["box_2"]["door_angle"] = details[
                    "door_angle"
                ]

        for box in touched_boxes:
            self.box_last_seen[box] = now

        self._notify(
            "telemetry_update",
            {
                "node_path": node_path,
                "boxes": sorted(touched_boxes),
                "observed_at": utc_timestamp(now),
            },
        )

    async def handle_event(self, event: dict[str, Any]) -> None:
        normalized = copy.deepcopy(event)
        now = time.time()
        if isinstance(normalized.get("timestamp"), (int, float)):
            normalized["timestamp"] = utc_timestamp(
                normalized["timestamp"]
            )
        normalized.setdefault("timestamp", utc_timestamp(now))
        normalized.setdefault("received_at", utc_timestamp(now))

        event_name = normalized.get("event")
        if event_name == "safety_alert":
            event_type = str(normalized.get("event_type", "UNKNOWN"))
            family = event_type.removesuffix("_DETECTED").removesuffix(
                "_CLEARED"
            )
            if normalized.get("simulated") is True:
                safety_key = (
                    f"drill:{normalized.get('drill_id', 'unknown')}:{family}"
                )
            else:
                safety_key = (
                    f"real:{normalized.get('source', 'unknown')}:{family}"
                )
            if normalized.get("active"):
                self.active_safety[safety_key] = normalized
            else:
                self.active_safety.pop(safety_key, None)
            self.events.appendleft(normalized)
        elif event_name in {
            "command_dispatched",
            "command_executed",
            "command_failed",
            "command_timeout",
        }:
            self.commands.appendleft(normalized)
            self.events.appendleft(normalized)
        else:
            self.events.appendleft(normalized)

        self._notify("core_event", normalized)

    def subscribe(self, maxsize: int = 64) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._listeners.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._listeners.discard(queue)

    def _notify(self, event_type: str, data: dict[str, Any]) -> None:
        message = {
            "schema_version": self.SCHEMA_VERSION,
            "event_type": event_type,
            "timestamp": utc_timestamp(),
            "data": data,
        }
        for queue in tuple(self._listeners):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    def _box_status(self, box: str, now: float) -> dict[str, Any]:
        last_seen = self.box_last_seen[box]
        if last_seen is None:
            status = "unknown"
            age = None
        else:
            age = max(0.0, now - last_seen)
            if age <= self.ONLINE_AFTER_SECONDS:
                status = "online"
            elif age <= self.OFFLINE_AFTER_SECONDS:
                status = "stale"
            else:
                status = "offline"

        return {
            "status": status,
            "last_seen": utc_timestamp(last_seen) if last_seen else None,
            "age_seconds": round(age, 2) if age is not None else None,
            "telemetry": copy.deepcopy(self.box_fields[box]),
        }

    @staticmethod
    def _sensor_value(node: dict[str, Any], name: str) -> dict[str, Any]:
        sensor = node.get("sensors", {}).get(name)
        if not sensor:
            return {"value": None, "timestamp": None}
        return {
            "value": sensor.get("value"),
            "timestamp": utc_timestamp(sensor.get("timestamp"))
            if sensor.get("timestamp")
            else None,
        }

    @staticmethod
    def _actual_actuator(
        node: dict[str, Any],
        name: str,
        fallback: Any = None,
    ) -> Any:
        actuator = node.get("actuators", {}).get(name, {})
        return actuator.get("actual", fallback)

    def snapshot(self, twin) -> dict[str, Any]:
        now = time.time()
        twin_nodes = twin.snapshot().get("nodes", {})
        sensor_node = twin_nodes.get(SENSOR_NODE, {})
        actuator_node = twin_nodes.get(ACTUATOR_NODE, {})
        box_1 = self._box_status("box_1", now)
        box_2 = self._box_status("box_2", now)

        buzzer = self._actual_actuator(
            actuator_node,
            "buzzer",
            box_1["telemetry"].get("buzzer"),
        )
        light_on = self._actual_actuator(
            actuator_node,
            "led",
            box_2["telemetry"].get("led"),
        )
        door_open = self._actual_actuator(
            actuator_node,
            "servo",
            box_2["telemetry"].get("servo"),
        )

        light_state = actuator_node.get("actuators", {}).get("led", {})
        servo_state = actuator_node.get("actuators", {}).get("servo", {})
        light_mode = box_2["telemetry"].get("light_mode") or light_state.get(
            "actual_mode"
        )
        door_angle = box_2["telemetry"].get("door_angle")
        if door_angle is None:
            door_angle = servo_state.get("commanded_angle")

        active_safety = list(self.active_safety.values())
        has_real_hazard = any(
            event.get("simulated") is not True
            for event in active_safety
        )
        safety_status = (
            "critical"
            if has_real_hazard
            else "drill"
            if active_safety
            else "normal"
        )

        return {
            "schema_version": self.SCHEMA_VERSION,
            "generated_at": utc_timestamp(now),
            "system": {
                "status": safety_status,
                "started_at": utc_timestamp(self.started_at),
                "services": copy.deepcopy(self.services),
            },
            "voice": copy.deepcopy(self.voice),
            "nodes": {
                "box_1": box_1,
                "box_2": box_2,
            },
            "environment": {
                name: self._sensor_value(sensor_node, name)
                for name in (
                    "temperature",
                    "humidity",
                    "gas",
                    "fire_detected",
                    "dht_valid",
                )
            },
            "actuators": {
                "buzzer": {
                    "state": buzzer,
                    "local_reflex_active": bool(
                        box_1["telemetry"].get("gas")
                        or box_1["telemetry"].get("fire_detected")
                    ),
                },
                "light": {
                    "state": light_on,
                    "mode": light_mode or ("off" if light_on is False else None),
                },
                "door": {
                    "state": door_open,
                    "angle": door_angle,
                },
            },
            "safety": {
                "status": safety_status,
                "active": copy.deepcopy(active_safety),
                "recent": [
                    copy.deepcopy(item)
                    for item in self.events
                    if item.get("event") == "safety_alert"
                ][:25],
            },
            "commands": [copy.deepcopy(item) for item in self.commands][:25],
            "events": [copy.deepcopy(item) for item in self.events][:50],
        }
