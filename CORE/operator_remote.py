"""MQTT service for the physical VENUS operator remote.

The registered remote can originate explicitly labelled emergency drills. A
drill enters the normal Event Bus and Safety Response path without modifying
sensor telemetry or pretending to be a physical hazard.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import aiomqtt

from CORE.safety_event import SafetyEvent


REMOTE_ID = "operator_remote_01"
TOPIC_ROOT = f"venus/interface/{REMOTE_ID}"

REMOTE_STATUS_TOPIC = f"{TOPIC_ROOT}/status"
CORE_STATUS_TOPIC = f"{TOPIC_ROOT}/core_status"
SIMULATION_REQUEST_TOPIC = f"{TOPIC_ROOT}/simulation/request"
SIMULATION_STATE_TOPIC = f"{TOPIC_ROOT}/simulation/state"
LATENCY_PING_TOPIC = f"{TOPIC_ROOT}/latency/ping"
LATENCY_PONG_TOPIC = f"{TOPIC_ROOT}/latency/pong"

VALID_SIMULATIONS = {"fire", "gas"}
MIN_SIMULATION_SECONDS = 3
MAX_SIMULATION_SECONDS = 60


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class OperatorRemoteService:
    """Handle simulation-only events and Core latency diagnostics."""

    def __init__(self, broker_ip: str, event_bus, broker_port: int = 1883) -> None:
        self.broker_ip = broker_ip
        self.broker_port = broker_port
        self.event_bus = event_bus
        self._client: aiomqtt.Client | None = None
        self._active_simulation: dict[str, Any] | None = None
        self._simulation_task: asyncio.Task | None = None
        self._latest_simulation_state: dict[str, Any] | None = None
        self._recent_request_ids: deque[str] = deque(maxlen=64)
        self._recent_request_id_set: set[str] = set()

    @staticmethod
    def _decode_payload(raw_payload: bytes | bytearray | memoryview) -> Any:
        try:
            return json.loads(bytes(raw_payload).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _remember_request(self, request_id: str) -> bool:
        if request_id in self._recent_request_id_set:
            return False
        if len(self._recent_request_ids) == self._recent_request_ids.maxlen:
            expired = self._recent_request_ids.popleft()
            self._recent_request_id_set.discard(expired)
        self._recent_request_ids.append(request_id)
        self._recent_request_id_set.add(request_id)
        return True

    async def _publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        retain: bool = False,
    ) -> None:
        if self._client is None:
            return
        await self._client.publish(
            topic,
            json.dumps(payload),
            qos=1,
            retain=retain,
        )

    async def _publish_core_status(self, status: str) -> None:
        await self._publish(
            CORE_STATUS_TOPIC,
            {
                "status": status,
                "service": "operator_remote",
                "timestamp": utc_timestamp(),
            },
            retain=True,
        )

    async def _publish_simulation_state(
        self,
        *,
        request_id: str,
        simulation_type: str,
        status: str,
        duration_seconds: int = 0,
        reason: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "request_id": request_id,
            "simulation_type": simulation_type,
            "status": status,
            "simulated": True,
            "drill": True,
            "actuators_enabled": True,
            "duration_seconds": duration_seconds,
            "timestamp": utc_timestamp(),
        }
        if reason:
            payload["reason"] = reason
        self._latest_simulation_state = payload
        await self._publish(SIMULATION_STATE_TOPIC, payload, retain=True)

    async def _reject_simulation(
        self,
        request_id: str,
        simulation_type: str,
        reason: str,
    ) -> None:
        await self._publish_simulation_state(
            request_id=request_id,
            simulation_type=simulation_type,
            status="rejected",
            reason=reason,
        )
        print(f"[REMOTE SIMULATION] Rejected {request_id}: {reason}")

    async def _publish_drill_event(
        self,
        *,
        request_id: str,
        simulation_type: str,
        active: bool,
        reason: str,
    ) -> None:
        event_type = {
            ("fire", True): "FLAME_DETECTED",
            ("fire", False): "FLAME_CLEARED",
            ("gas", True): "GAS_DETECTED",
            ("gas", False): "GAS_CLEARED",
        }[(simulation_type, active)]
        event = SafetyEvent(
            event_type=event_type,
            severity="CRITICAL" if active else "INFO",
            source=TOPIC_ROOT,
            details={
                "condition": "operator_emergency_drill",
                "simulation_type": simulation_type,
                "reason": reason,
            },
            simulated=True,
            origin_type="operator_drill",
            drill_id=request_id,
            actuators_enabled=True,
        )
        await self.event_bus.publish(event.to_dict())

    async def _clear_simulation_after(
        self,
        request_id: str,
        simulation_type: str,
        duration_seconds: int,
    ) -> None:
        try:
            await asyncio.sleep(duration_seconds)
            active = self._active_simulation
            if active is None or active.get("request_id") != request_id:
                return
            await self._publish_drill_event(
                request_id=request_id,
                simulation_type=simulation_type,
                active=False,
                reason="automatic_timeout",
            )
            self._active_simulation = None
            await self._publish_simulation_state(
                request_id=request_id,
                simulation_type=simulation_type,
                status="cleared",
                duration_seconds=duration_seconds,
                reason="automatic_timeout",
            )
            print(
                f"[REMOTE DRILL] {simulation_type.upper()} emergency drill "
                "cleared automatically."
            )
        except asyncio.CancelledError:
            raise

    async def _cancel_active_simulation(
        self,
        *,
        request_id: str,
        reason: str,
    ) -> None:
        active = self._active_simulation
        if self._simulation_task is not None:
            self._simulation_task.cancel()
            await asyncio.gather(self._simulation_task, return_exceptions=True)
            self._simulation_task = None

        if active is None:
            await self._publish_simulation_state(
                request_id=request_id,
                simulation_type="none",
                status="cleared",
                reason="no_active_simulation",
            )
            return

        self._active_simulation = None
        await self._publish_drill_event(
            request_id=str(active["request_id"]),
            simulation_type=str(active["simulation_type"]),
            active=False,
            reason=reason,
        )
        await self._publish_simulation_state(
            request_id=request_id,
            simulation_type=str(active["simulation_type"]),
            status="cleared",
            duration_seconds=int(active["duration_seconds"]),
            reason=reason,
        )
        print("[REMOTE DRILL] Emergency drill cancelled by physical remote.")

    async def _handle_simulation_request(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            await self._reject_simulation("unknown", "unknown", "invalid_json")
            return

        request_id = str(payload.get("request_id", "")).strip()
        simulation_type = str(payload.get("simulation_type", "")).lower()
        action = str(payload.get("action", "start")).lower()

        if not request_id:
            await self._reject_simulation("unknown", simulation_type, "missing_request_id")
            return
        if payload.get("actuators_enabled") is not True:
            await self._reject_simulation(
                request_id,
                simulation_type,
                "full_drill_requires_explicit_actuator_permission",
            )
            return

        if action == "cancel":
            await self._cancel_active_simulation(
                request_id=request_id,
                reason="remote_cancelled",
            )
            return
        if action != "start":
            await self._reject_simulation(request_id, simulation_type, "invalid_action")
            return
        if simulation_type not in VALID_SIMULATIONS:
            await self._reject_simulation(
                request_id,
                simulation_type,
                "unsupported_simulation_type",
            )
            return

        try:
            duration_seconds = int(payload.get("duration_seconds", 10))
        except (TypeError, ValueError):
            await self._reject_simulation(request_id, simulation_type, "invalid_duration")
            return
        duration_seconds = max(
            MIN_SIMULATION_SECONDS,
            min(duration_seconds, MAX_SIMULATION_SECONDS),
        )

        if not self._remember_request(request_id):
            active = self._active_simulation
            is_still_active = (
                active is not None and active.get("request_id") == request_id
            )
            await self._publish_simulation_state(
                request_id=request_id,
                simulation_type=simulation_type,
                status="active" if is_still_active else "rejected",
                duration_seconds=duration_seconds,
                reason=(
                    "duplicate_active_request"
                    if is_still_active
                    else "duplicate_completed_request"
                ),
            )
            return

        if self._active_simulation is not None:
            await self._reject_simulation(
                request_id,
                simulation_type,
                "another_drill_is_active",
            )
            return

        self._active_simulation = {
            "request_id": request_id,
            "simulation_type": simulation_type,
            "duration_seconds": duration_seconds,
            "started_monotonic": time.monotonic(),
        }
        await self._publish_drill_event(
            request_id=request_id,
            simulation_type=simulation_type,
            active=True,
            reason="operator_confirmed",
        )
        await self._publish_simulation_state(
            request_id=request_id,
            simulation_type=simulation_type,
            status="active",
            duration_seconds=duration_seconds,
        )
        self._simulation_task = asyncio.create_task(
            self._clear_simulation_after(
                request_id,
                simulation_type,
                duration_seconds,
            )
        )
        print(
            f"[REMOTE DRILL] {simulation_type.upper()} emergency drill active "
            f"for {duration_seconds}s (physical response enabled)."
        )

    async def _handle_latency_ping(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        request_id = str(payload.get("request_id", "")).strip()
        if not request_id:
            return
        received_unix_ms = int(time.time() * 1000)
        await self._publish(
            LATENCY_PONG_TOPIC,
            {
                "request_id": request_id,
                "remote_sent_ms": payload.get("remote_sent_ms"),
                "core_received_unix_ms": received_unix_ms,
                "core_sent_unix_ms": int(time.time() * 1000),
                "status": "pong",
                "timestamp": utc_timestamp(),
            },
        )

    async def run(self) -> None:
        reconnect_delay = 1.0
        while True:
            try:
                offline_will = aiomqtt.Will(
                    CORE_STATUS_TOPIC,
                    json.dumps({"status": "offline", "service": "operator_remote"}),
                    qos=1,
                    retain=True,
                )
                async with aiomqtt.Client(
                    hostname=self.broker_ip,
                    port=self.broker_port,
                    identifier="venus-core-operator-remote",
                    will=offline_will,
                ) as client:
                    self._client = client
                    reconnect_delay = 1.0
                    await client.subscribe(SIMULATION_REQUEST_TOPIC, qos=1)
                    await client.subscribe(LATENCY_PING_TOPIC, qos=1)
                    await client.subscribe(REMOTE_STATUS_TOPIC, qos=1)
                    await self._publish_core_status("online")
                    if self._latest_simulation_state is not None:
                        await self._publish(
                            SIMULATION_STATE_TOPIC,
                            self._latest_simulation_state,
                            retain=True,
                        )
                    print(
                        "[OPERATOR REMOTE] Emergency drill and latency service ready "
                        f"at {self.broker_ip}:{self.broker_port}"
                    )

                    async for message in client.messages:
                        topic = str(message.topic)
                        payload = self._decode_payload(message.payload)
                        if topic == SIMULATION_REQUEST_TOPIC:
                            await self._handle_simulation_request(payload)
                        elif topic == LATENCY_PING_TOPIC:
                            await self._handle_latency_ping(payload)

            except asyncio.CancelledError:
                if self._client is not None:
                    with contextlib.suppress(Exception):
                        await self._publish_core_status("offline")
                raise
            except aiomqtt.MqttError as error:
                print(f"[OPERATOR REMOTE] MQTT unavailable: {error}")
            finally:
                self._client = None

            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2.0, 30.0)
