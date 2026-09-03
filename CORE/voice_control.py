"""Multi-source microphone control for the VENUS voice sidecar.

The optional ESP32 remote sends button events through MQTT, while authenticated
dashboard commands arrive through VENUS Core.  The sidecar remains the
authority for the actual state and reports it to both interfaces.  Muting gates
audio inside the VENUS process; it does not change the macOS microphone setting.
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
from livekit.agents.voice import io as voice_io


VOICE_REMOTE_ID = "voice_remote_01"
VOICE_TOPIC_ROOT = f"venus/interface/{VOICE_REMOTE_ID}"
VOICE_BUTTON_TOPIC = f"{VOICE_TOPIC_ROOT}/button"
VOICE_MIC_STATE_TOPIC = f"{VOICE_TOPIC_ROOT}/mic_state"
VOICE_REMOTE_STATUS_TOPIC = f"{VOICE_TOPIC_ROOT}/status"
VOICE_SIDECAR_STATUS_TOPIC = f"{VOICE_TOPIC_ROOT}/sidecar_status"


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class GatedAudioInput(voice_io.AudioInput):
    """Continuously drain console audio while forwarding it only when enabled.

    LiveKit's TCP console input keeps producing frames even when its ordinary
    attach/detach hook is disabled.  Draining muted frames here prevents both
    audio leakage to Gemini and an old-audio backlog on the next unmute.
    """

    def __init__(self, source: voice_io.AudioInput) -> None:
        super().__init__(label="VENUS physical microphone gate", source=source)
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    async def __anext__(self):
        if self.source is None:
            raise StopAsyncIteration

        while True:
            frame = await self.source.__anext__()
            if self._enabled:
                return frame


class WirelessMicrophoneController:
    """Translate deduplicated MQTT button presses into sidecar mic state."""

    def __init__(
        self,
        *,
        session,
        audio_gate: GatedAudioInput,
        core_client,
        broker_ip: str,
        broker_port: int = 1883,
        controller_timeout_seconds: float = 15.0,
        start_enabled: bool = True,
    ) -> None:
        self.session = session
        self.audio_gate = audio_gate
        self.core_client = core_client
        self.broker_ip = broker_ip
        self.broker_port = broker_port
        self.controller_timeout_seconds = controller_timeout_seconds
        self.start_enabled = bool(start_enabled)

        self.microphone_enabled = False
        self.controller_online = False
        self._last_controller_seen: float | None = None
        self._client: aiomqtt.Client | None = None
        self._recent_event_ids: deque[str] = deque(maxlen=64)
        self._recent_event_id_set: set[str] = set()

    def _remember_event(self, event_id: str) -> bool:
        """Return False for a duplicate event without toggling twice."""
        if event_id in self._recent_event_id_set:
            return False

        if len(self._recent_event_ids) == self._recent_event_ids.maxlen:
            expired = self._recent_event_ids.popleft()
            self._recent_event_id_set.discard(expired)

        self._recent_event_ids.append(event_id)
        self._recent_event_id_set.add(event_id)
        return True

    async def _report_to_core(self, *, changed_by: str) -> None:
        try:
            await self.core_client.report_voice_status(
                microphone_enabled=self.microphone_enabled,
                controller_online=self.controller_online,
                controller_id=VOICE_REMOTE_ID,
                changed_by=changed_by,
            )
        except Exception as error:
            print(f"⚠️ [MICROPHONE] Core status report failed: {error}")

    async def _publish_state(
        self,
        *,
        changed_by: str,
        ack_event_id: str | None = None,
    ) -> None:
        if self._client is None:
            return

        payload: dict[str, Any] = {
            "microphone_enabled": self.microphone_enabled,
            "state": "LISTENING" if self.microphone_enabled else "MUTED",
            "controller_online": self.controller_online,
            "controller_id": VOICE_REMOTE_ID,
            "changed_by": changed_by,
            "timestamp": utc_timestamp(),
        }
        if ack_event_id:
            payload["ack_event_id"] = ack_event_id

        await self._client.publish(
            VOICE_MIC_STATE_TOPIC,
            json.dumps(payload),
            qos=1,
            retain=True,
        )

    async def _publish_sidecar_status(self, status: str) -> None:
        if self._client is None:
            return
        await self._client.publish(
            VOICE_SIDECAR_STATUS_TOPIC,
            json.dumps(
                {
                    "status": status,
                    "microphone_enabled": (
                        self.microphone_enabled if status == "online" else False
                    ),
                    "timestamp": utc_timestamp(),
                }
            ),
            qos=1,
            retain=True,
        )

    async def set_microphone(
        self,
        enabled: bool,
        *,
        changed_by: str,
        ack_event_id: str | None = None,
    ) -> None:
        enabled = bool(enabled)
        changed = enabled != self.microphone_enabled
        self.microphone_enabled = enabled
        self.audio_gate.set_enabled(enabled)

        # This keeps LiveKit's speaking/listening state aligned with the custom
        # gate.  The gate itself is what blocks TCP console audio frames.
        self.session.input.set_audio_enabled(enabled)

        if changed:
            label = "LISTENING" if enabled else "MUTED"
            print(f"🎙️ [MICROPHONE] {label} ({changed_by})")

        await self._publish_state(
            changed_by=changed_by,
            ack_event_id=ack_event_id,
        )
        await self._report_to_core(changed_by=changed_by)

    async def _set_controller_online(self, online: bool, *, reason: str) -> None:
        if online:
            self._last_controller_seen = time.monotonic()

        if online == self.controller_online:
            return

        self.controller_online = online
        print(
            "📡 [VOICE REMOTE] "
            f"{'ONLINE' if online else 'OFFLINE'} ({reason})"
        )

        # The ESP32 is an optional control surface.  Its availability must not
        # override a microphone state selected by the dashboard or startup
        # policy.
        await self._publish_state(changed_by=reason)
        await self._report_to_core(changed_by=reason)

    @staticmethod
    def _decode_payload(raw_payload: bytes | bytearray | memoryview) -> Any:
        text = bytes(raw_payload).decode("utf-8").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    async def _handle_status(self, payload: Any) -> None:
        if isinstance(payload, dict):
            status = str(payload.get("status", "")).lower()
            device_id = str(payload.get("device_id", VOICE_REMOTE_ID))
        else:
            status = str(payload).lower()
            device_id = VOICE_REMOTE_ID

        if device_id != VOICE_REMOTE_ID:
            return
        if status == "online":
            await self._set_controller_online(True, reason="controller_heartbeat")
        elif status == "offline":
            await self._set_controller_online(False, reason="controller_offline")

    async def _handle_button(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            print("⚠️ [VOICE REMOTE] Ignored non-JSON button event.")
            return

        if str(payload.get("event", "")).upper() != "BUTTON_PRESS":
            return

        device_id = str(payload.get("device_id", ""))
        event_id = str(payload.get("event_id", ""))
        if device_id != VOICE_REMOTE_ID or not event_id:
            print("⚠️ [VOICE REMOTE] Ignored invalid button event.")
            return

        await self._set_controller_online(True, reason="button_event")

        if not self._remember_event(event_id):
            await self._publish_state(
                changed_by="duplicate_button_event",
                ack_event_id=event_id,
            )
            return

        await self.set_microphone(
            not self.microphone_enabled,
            changed_by="physical_button",
            ack_event_id=event_id,
        )

    async def _watch_controller(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if (
                self.controller_online
                and self._last_controller_seen is not None
                and time.monotonic() - self._last_controller_seen
                > self.controller_timeout_seconds
            ):
                await self._set_controller_online(
                    False,
                    reason="controller_timeout",
                )

    async def run(self) -> None:
        """Maintain the broker connection until the sidecar shuts down."""
        reconnect_delay = 1.0
        await self.set_microphone(
            self.start_enabled,
            changed_by="sidecar_startup",
        )

        while True:
            watchdog_task: asyncio.Task | None = None
            try:
                offline_will = aiomqtt.Will(
                    VOICE_SIDECAR_STATUS_TOPIC,
                    json.dumps(
                        {
                            "status": "offline",
                            "microphone_enabled": False,
                        }
                    ),
                    qos=1,
                    retain=True,
                )
                async with aiomqtt.Client(
                    hostname=self.broker_ip,
                    port=self.broker_port,
                    identifier="venus-sidecar-voice-control",
                    will=offline_will,
                ) as client:
                    self._client = client
                    reconnect_delay = 1.0
                    await client.subscribe(VOICE_BUTTON_TOPIC, qos=1)
                    await client.subscribe(VOICE_REMOTE_STATUS_TOPIC, qos=1)
                    await self._publish_sidecar_status("online")
                    await self._publish_state(changed_by="mqtt_connected")
                    await self._report_to_core(changed_by="mqtt_connected")
                    print(
                        "📡 [MICROPHONE] Wireless controller channel ready at "
                        f"{self.broker_ip}:{self.broker_port}"
                    )

                    watchdog_task = asyncio.create_task(
                        self._watch_controller()
                    )
                    try:
                        async for message in client.messages:
                            topic = str(message.topic)
                            payload = self._decode_payload(message.payload)
                            if topic == VOICE_BUTTON_TOPIC:
                                await self._handle_button(payload)
                            elif topic == VOICE_REMOTE_STATUS_TOPIC:
                                await self._handle_status(payload)
                    finally:
                        # Publish a retained offline state before a graceful
                        # disconnect.  Unexpected disconnects use the MQTT LWT.
                        if asyncio.current_task().cancelling():
                            with contextlib.suppress(Exception):
                                await self._publish_sidecar_status("offline")

            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as error:
                print(f"⚠️ [MICROPHONE] MQTT unavailable: {error}")
            finally:
                self._client = None
                if watchdog_task is not None:
                    watchdog_task.cancel()
                    await asyncio.gather(watchdog_task, return_exceptions=True)
                await self._set_controller_online(
                    False,
                    reason="mqtt_disconnected",
                )

            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2.0, 30.0)
