# ==============================================================================
# VENUS CORE: SAFETY WATCHDOG MODULE (core/safety_watchdog.py)
# ==============================================================================
# Description: The Tier 2 Deterministic Reflex System. Operating on a strict 
# 50ms asynchronous loop, it evaluates the Digital Twin RAM matrix. It respects 
# Tier 1 local PIC reflexes but acts as the global orchestrator to dispatch
# cross-system emergency commands.
# ==============================================================================

import asyncio  # Required to use asyncio.sleep for the non-blocking execution loop
from CORE.digital_twin import DynamicDigitalTwin # Type hint import for the shared memory matrix
from CORE.safety_event import SafetyEvent
from CORE.event_bus import VenusEventBus

async def start_safety_watchdog(
    twin: DynamicDigitalTwin,
    event_bus: VenusEventBus
) -> None:
    """
    Continuously monitors the RAM matrix for gas and fire hazards across all active UNS nodes.
    Requires the shared RAM twin and the outbound network queue as dependencies.
    """
    print("🛡️ [WATCHDOG] Tier 2 Deterministic Reflex System Initialized (UNS Mode).")

    def update_node_health(
            uns_node_path: str,
            healthy: bool
        ):
            """
            Updates the health state of a CPS node.

            Only prints when the state changes to avoid console spam.
            """

            previous_state = _node_health.get(
                uns_node_path,
                "unknown"
            )

            new_state = (
                "healthy"
                if healthy
                else "degraded"
            )

            _node_health[uns_node_path] = new_state


            if previous_state != new_state:

                if new_state == "degraded":

                    print(
                        f"⚠️ [WATCHDOG] Node {uns_node_path} "
                        f"is DEGRADED. Telemetry unavailable."
                    )

                elif new_state == "healthy":

                    print(
                        f"✅ [WATCHDOG] Node {uns_node_path} "
                        f"telemetry restored."
                    )

    # --------------------------------------------------------------------------
    # BUG FIX: NODE-SPECIFIC STATE LATCHING FLAGS
    # --------------------------------------------------------------------------
    # Instead of a single True/False flag, we use dictionaries to track alarms 
    # for each room separately. Format: {"residential/dorm_a/living_room": True}
    _gas_alerts: dict[str, bool] = {}
    _fire_alerts: dict[str, bool] = {}

    # Temporary counter used to reject short KY-026 noise spikes.
    _flame_confirmation_counter: dict[str, int] = {}

    # ------------------------------------------------------------------
    # PHASE 5D: NODE HEALTH STATE
    # ------------------------------------------------------------------

    # Tracks whether each UNS node has trustworthy telemetry.
    #
    # healthy:
    #   telemetry is fresh
    #
    # degraded:
    #   telemetry is stale/unavailable
    _node_health: dict[str, str] = {}

    MAX_SENSOR_AGE_SECONDS = 10

    # ------------------------------------------------------------------
    # PHASE 5C: FLAME SENSOR DEBOUNCE
    # ------------------------------------------------------------------

    # Number of consecutive watchdog cycles where flame must remain active
    # before confirming a fire event.
    FLAME_CONFIRM_CYCLES = 3

    while True:
        # Step 1: Read a thread-safe O(1) snapshot of the physical world from RAM
        snapshot = twin.snapshot()
        state_matrix = snapshot["nodes"]

        # Step 2: Iterate dynamically through every registered UNS node path in memory
        for uns_node_path, node_data in state_matrix.items():

            sensors = node_data.get("sensors", {})

            # Actuator-only nodes are not safety telemetry sources.
            if "gas" not in sensors and "fire_detected" not in sensors:

                continue

            sensor_age = twin.get_sensor_age(
                uns_node_path
            )

            if sensor_age > MAX_SENSOR_AGE_SECONDS:

                update_node_health(
                    uns_node_path,
                    healthy=False
                )

                continue


            else:

                update_node_health(
                    uns_node_path,
                    healthy=True
                )

            if _node_health.get(uns_node_path) == "degraded":

                continue


            # Read sensor values through the Digital Twin API.
            # MQ2 uses its digital trigger output: False=normal, True=triggered.
            gas_detected = bool(
                twin.get_sensor_value(
                    uns_node_path,
                    "gas"
                )
            )

            fire_detected = bool(
                twin.get_sensor_value(
                    uns_node_path,
                    "fire_detected"
                )
            )

            # Fetch the current latch status for this specific node (defaults to False)
            is_gas_latched = _gas_alerts.get(uns_node_path, False)
            is_fire_latched = _fire_alerts.get(uns_node_path, False)

            # ==================================================================
            # HAZARD 1: COMBUSTIBLE GAS LEAK (MQ2 DIGITAL OUTPUT)
            # ==================================================================

            if gas_detected and not is_gas_latched:

                _gas_alerts[uns_node_path] = True

                print(
                    f"🚨 [WATCHDOG] Gas emergency detected "
                    f"on {uns_node_path}!"
                )

                event = SafetyEvent(

                    event_type="GAS_DETECTED",

                    severity="CRITICAL",

                    source=uns_node_path,

                    details={
                        "sensor": "MQ2",
                        "condition": "gas_detected"
                    }

                )


                await event_bus.publish(
                    event.to_dict()
                )


            elif not gas_detected and is_gas_latched:

                _gas_alerts[uns_node_path] = False

                print(
                    f"✅ [WATCHDOG] Gas hazard cleared "
                    f"on {uns_node_path}. Re-arming."
                )

                clear_event = SafetyEvent(
                    event_type="GAS_CLEARED",
                    severity="INFO",
                    source=uns_node_path,
                    details={
                        "sensor": "MQ2",
                        "condition": "gas_clear"
                    }
                )

                await event_bus.publish(
                    clear_event.to_dict()
                )

            # Clearing the latch deliberately sends no servo-close command.
            # ==================================================================
            # HAZARD 2: ACTIVE FLAME DETECTED (KY026)
            # PHASE 5C: FLAME SENSOR DEBOUNCE
            # ==================================================================

            flame_confirmed = False


            if fire_detected:

                current_count = _flame_confirmation_counter.get(
                    uns_node_path,
                    0
                )

                current_count += 1

                _flame_confirmation_counter[uns_node_path] = current_count


                if current_count >= FLAME_CONFIRM_CYCLES:

                    flame_confirmed = True


            else:

                # Reset counter when flame disappears
                _flame_confirmation_counter[uns_node_path] = 0



            if flame_confirmed and not is_fire_latched:


                print(
                    f"🚨 [WATCHDOG] CONFIRMED FIRE DETECTED "
                    f"on {uns_node_path}!"
                )

                event = SafetyEvent(

                    event_type="FLAME_DETECTED",

                    severity="CRITICAL",

                    source=uns_node_path,

                    details={
                        "sensor": "KY026",
                        "condition": "flame_confirmed"
                    }

                )


                await event_bus.publish(
                    event.to_dict()
                )

                _fire_alerts[uns_node_path] = True


            elif not fire_detected and is_fire_latched:

                print(
                    f"✅ [WATCHDOG] Fire extinguished "
                    f"on {uns_node_path}. Re-arming."
                )

                _fire_alerts[uns_node_path] = False

                clear_event = SafetyEvent(
                    event_type="FLAME_CLEARED",
                    severity="INFO",
                    source=uns_node_path,
                    details={
                        "sensor": "KY026",
                        "condition": "flame_clear"
                    }
                )

                await event_bus.publish(
                    clear_event.to_dict()
                )

        # Step 3: Yield execution back to the CPU for 50 milliseconds
        # This keeps the CPU idle and allows the MQTT and AI loops to run simultaneously
        await asyncio.sleep(0.05)
