"""Translate trusted safety events into coordinated actuator commands."""

from __future__ import annotations

from typing import Any

from CORE.command_gateway import CommandGateway


ACTUATOR_NODE = "venus/living_room/actuator_node_01"
OPERATOR_REMOTE_SOURCE = "venus/interface/operator_remote_01"
DETECTED_EVENTS = {"FLAME_DETECTED", "GAS_DETECTED"}
CLEARED_EVENTS = {"FLAME_CLEARED", "GAS_CLEARED"}


class SafetyResponseHandler:
    """Execute real emergency policies and explicitly labelled drill policies."""

    def __init__(self, command_gateway: CommandGateway) -> None:
        self.command_gateway = command_gateway
        self._active_real_hazards: set[tuple[str, str]] = set()
        self._active_drills: set[str] = set()
        self._drill_restore_states: dict[str, dict[str, bool]] = {}

    @staticmethod
    def _hazard_family(event_type: str) -> str:
        if event_type.startswith("FLAME_") or event_type.startswith("FIRE_"):
            return "FIRE"
        if event_type.startswith("GAS_"):
            return "GAS"
        return event_type

    @staticmethod
    def _is_trusted_drill(event: dict[str, Any]) -> bool:
        return (
            event.get("simulated") is True
            and event.get("origin_type") == "operator_drill"
            and event.get("source") == OPERATOR_REMOTE_SOURCE
            and isinstance(event.get("drill_id"), str)
            and bool(event["drill_id"].strip())
            and event.get("actuators_enabled") is True
        )

    def _observed_actuator_state(self, target: str) -> bool:
        snapshot = self.command_gateway.digital_twin.snapshot()
        actuator = (
            snapshot.get("nodes", {})
            .get(ACTUATOR_NODE, {})
            .get("actuators", {})
            .get(target, {})
        )
        actual = actuator.get("actual")
        if isinstance(actual, bool):
            return actual
        desired = actuator.get("desired")
        return desired if isinstance(desired, bool) else False

    async def _command(self, target: str, state: bool, source: str) -> None:
        result = await self.command_gateway.submit_actuator_request(
            node_path=ACTUATOR_NODE,
            target=target,
            state=state,
            source=source,
        )
        if result.get("status") != "accepted":
            print(
                f"[SAFETY RESPONSE] {target} command rejected: "
                f"{result.get('reason', 'unknown')}"
            )

    async def _activate_real_emergency(self, event_type: str) -> None:
        # Box 1's PIC owns the immediate local buzzer reflex. Core performs the
        # cross-node action by opening the Box 2 servo.
        await self._command("servo", True, "safety_response")
        print(
            f"🚨 [VENUS ALERT] Real {self._hazard_family(event_type).lower()} "
            "hazard detected. Cross-node emergency response activated."
        )

    async def _activate_drill(self, event: dict[str, Any]) -> None:
        drill_id = str(event["drill_id"])
        self._drill_restore_states[drill_id] = {
            "buzzer": self._observed_actuator_state("buzzer"),
            "servo": self._observed_actuator_state("servo"),
        }
        self._active_drills.add(drill_id)

        # A drill enters the same command gateway as real orchestration, while
        # its event remains labelled as simulated throughout the Core.
        await self._command("buzzer", True, "safety_drill")
        await self._command("servo", True, "safety_drill")
        print(
            f"🧪 [SAFETY DRILL] {self._hazard_family(event['event_type'])} "
            f"drill {drill_id} activated the buzzer and servo."
        )

    async def _clear_drill(self, event: dict[str, Any]) -> None:
        drill_id = str(event["drill_id"])
        restore = self._drill_restore_states.pop(
            drill_id,
            {"buzzer": False, "servo": False},
        )
        self._active_drills.discard(drill_id)

        # A drill must never switch off hardware while a genuine hazard or a
        # different drill is still active.
        if self._active_real_hazards or self._active_drills:
            print(
                f"🧪 [SAFETY DRILL] {drill_id} cleared; actuator recovery "
                "deferred because another safety condition is active."
            )
            return

        await self._command("buzzer", restore["buzzer"], "safety_drill_recovery")
        await self._command("servo", restore["servo"], "safety_drill_recovery")
        print(
            f"✅ [SAFETY DRILL] {drill_id} cleared and pre-drill actuator "
            "states were restored."
        )

    async def handle_event(self, event: dict[str, Any]) -> None:
        if event.get("event") not in {None, "safety_alert"}:
            return

        event_type = str(event.get("event_type", ""))
        if event_type not in DETECTED_EVENTS | CLEARED_EVENTS:
            return

        simulated = event.get("simulated") is True
        if simulated and not self._is_trusted_drill(event):
            print("⚠️ [SAFETY RESPONSE] Rejected untrusted simulated event.")
            return

        family = self._hazard_family(event_type)
        is_detected = event_type in DETECTED_EVENTS

        if simulated:
            if is_detected:
                await self._activate_drill(event)
            else:
                await self._clear_drill(event)
            return

        real_key = (str(event.get("source", "unknown")), family)
        if is_detected:
            if real_key in self._active_real_hazards:
                return
            self._active_real_hazards.add(real_key)
            print(
                f"🛡️ [SAFETY RESPONSE] Real {event_type} "
                f"from {real_key[0]}"
            )
            await self._activate_real_emergency(event_type)
        else:
            self._active_real_hazards.discard(real_key)
            # Preserve the existing real-hazard recovery policy: clearing the
            # Watchdog latch does not automatically close the servo.
            print(
                f"✅ [SAFETY RESPONSE] Real {family.lower()} hazard cleared; "
                "automatic actuator closure remains disabled."
            )
