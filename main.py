# ==============================================================================
# VENUS CORE: RUNTIME KERNEL (main.py)
# ==============================================================================
# Description: The root execution entry point (PID 1). Bootstraps memory, 
# network queues, and launches the Safety Watchdog, MQTT Transceiver, and AI Agent 
# as concurrent, non-blocking tasks on Python's asyncio event loop.
# ==============================================================================

import asyncio  # Asynchronous framework used to run parallel non-blocking loops
import os       # Operating system module to inspect environment variables
import sys      # System module for process exit codes and terminal handling

# ------------------------------------------------------------------------------
# MODULE IMPORTS FROM THE CORE INFRASTRUCTURE AND COGNITIVE DOMAINS
# ------------------------------------------------------------------------------
from CORE.digital_twin import DynamicDigitalTwin
from CORE.safety_watchdog import start_safety_watchdog
from CORE.MQTT_transceiver import start_mqtt_transceiver
from CORE.Venus.agent import VenusAgent
from CORE.event_bus import VenusEventBus

from dotenv import load_dotenv
load_dotenv(".env.local")  # Loads variables from .env into os.environ

from CORE.command_gateway import CommandGateway
from CORE.api_server import VenusAPIServer

from CORE.safety_response import SafetyResponseHandler
from CORE.mqtt_command_protocol import MQTTCommandProtocol
from CORE.monitoring_state import MonitoringState
from CORE.dashboard_server import VenusDashboardServer
from CORE.operator_auth import OperatorAuthManager
from CORE.operator_remote import OperatorRemoteService

# ------------------------------------------------------------------------------
# SYSTEM CONFIGURATION PARAMETERS
# ------------------------------------------------------------------------------
# MQTT now runs on this Mac by default. Override this only when the broker is
# hosted on another machine.
BROKER_IP = os.environ.get("MQTT_BROKER_IP", "127.0.0.1")
DASHBOARD_HOST = os.environ.get("VENUS_DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.environ.get("VENUS_DASHBOARD_PORT", "8080"))
OPERATOR_HOST = os.environ.get("VENUS_OPERATOR_HOST", "127.0.0.1")
OPERATOR_PORT = int(os.environ.get("VENUS_OPERATOR_PORT", "8081"))


async def boot_sequence() -> None:
    """
    Initializes shared memory primitives, launches background safety & network daemons,
    and maintains the non-blocking command interface.
    """
    print("\n[SYSTEM] Booting Venus Core Runtime Kernel...")

    # --------------------------------------------------------------------------
    # 1. SHARED PRIMITIVE INSTANTIATION (DEPENDENCY INJECTION)
    # --------------------------------------------------------------------------
    # Create single instances of the memory state (RAM matrix) and the output queue (Tx FIFO).
    # Passing these references down to child tasks `ensures every module operates on 
    # the exact same shared memory space without creating data duplicates.
    system_twin = DynamicDigitalTwin()

    # ==========================================================
    # CPS NODE REGISTRATION
    #
    # Physical assets known by Venus
    # ==========================================================


    system_twin.register_node(
        "venus/living_room/sensor_node_01"
    )


    system_twin.register_node(
        "venus/living_room/actuator_node_01"
    )

    system_twin.nodes[
        "venus/living_room/actuator_node_01"
    ]["actuators"] = {

        "led": {},
        "servo": {},
        "buzzer": {}

    }

    
    outbound_queue = asyncio.Queue()
    event_bus = VenusEventBus()
    monitoring_state = MonitoringState()

    # Dashboard monitoring is an observer only. It receives events but has no
    # reference to the outbound command queue or Command Gateway.
    event_bus.subscribe(
        monitoring_state.handle_event
    )

    mqtt_command_protocol = MQTTCommandProtocol(
        outbound_queue
    )

    command_gateway = CommandGateway(
        system_twin,
        mqtt_command_protocol,
    )

    operator_auth = OperatorAuthManager.from_environment()

    # ==========================================================
    # Phase 4C:
    # Register Safety Event Response Handler
    #
    # Safety Watchdog publishes events.
    # Safety Response converts events into commands.
    # ==========================================================

    safety_response = SafetyResponseHandler(
        command_gateway
    )


    event_bus.subscribe(
        safety_response.handle_event
    )

    api_server = VenusAPIServer(
        command_gateway,
        event_bus,
        monitoring_state=monitoring_state,
    )

    dashboard_server = VenusDashboardServer(
        system_twin,
        monitoring_state,
        command_gateway=command_gateway,
        voice_control_gateway=api_server,
        operator_auth=operator_auth,
        host=DASHBOARD_HOST,
        port=DASHBOARD_PORT,
        operator_host=OPERATOR_HOST,
        operator_port=OPERATOR_PORT,
    )

    operator_remote = OperatorRemoteService(
        BROKER_IP,
        event_bus,
    )

    # --------------------------------------------------------------------------
    # 2. COGNITIVE AGENT INITIALIZATION
    # --------------------------------------------------------------------------
    # Instantiates the Gemini LLM agent. Traps missing API key errors early during boot.
    try:
        venus_ai = VenusAgent()
    except ValueError as e:
        print(f"🚨 [FATAL ERROR] {e}")
        print("Please export your API key using: export GEMINI_API_KEY='your_key'")
        sys.exit(1)

    print("[SYSTEM] Shared memory primitives and AI core initialized.")

    # --------------------------------------------------------------------------
    # 3. BACKGROUND DAEMON SPAWNING
    # --------------------------------------------------------------------------
    # asyncio.create_task pushes these loops to run perpetually in the background.
    
    # Task A: Network Gateway (Listens for Wi-Fi sensor packets and pushes commands out)
    mqtt_task = asyncio.create_task(
        start_mqtt_transceiver(
            system_twin,
            outbound_queue,
            event_bus,
            BROKER_IP,
            monitoring_state=monitoring_state,
        )
    )

    api_task = asyncio.create_task(
        api_server.start()
    )

    dashboard_task = asyncio.create_task(
        dashboard_server.start()
    )

    operator_remote_task = asyncio.create_task(
        operator_remote.run()
    )

    # Task B: 50ms Deterministic Safety Watchdog (Evaluates hazards and fires macros)
    watchdog_task = asyncio.create_task(
        start_safety_watchdog(
            system_twin,
            event_bus
        )
    )

    print("\n==================================================")
    print(" 🟢 VENUS CORE ONLINE AND MONITORING THE NETWORK ")
    print("==================================================\n")

    # --------------------------------------------------------------------------
    # 4. FOREGROUND INTERACTIVE COMMAND LOOP
    # --------------------------------------------------------------------------
    # Accepts user commands via the terminal while background safety tasks run simultaneously.
    try:
        while True:
            # IMPORTANT: Standard input() normally freezes the entire Python process.
            # asyncio.to_thread offloads input() to a side thread so the 50ms Watchdog 
            # and MQTT loops never pause while you are typing a message!
            user_input = await asyncio.to_thread(input, "Sir, your command: ")

            # Check for explicit user exit requests
            if user_input.strip().lower() in ["exit", "quit", "shutdown"]:
                print("[SYSTEM] Shutdown sequence requested by user.")
                break

            # Send non-empty commands to the AI reasoning engine
            if user_input.strip():
                reply = await venus_ai.process_intent(
                    user_input, 
                    system_twin, 
                    outbound_queue
                )
                # Output Venus's voice-optimized response
                print(f"\n[VENUS]: {reply}\n")

    except asyncio.CancelledError:
        # Triggered if the outer execution loop requests task cancellation
        print("\n[SYSTEM] Interactive loop cancelled.")

    finally:
        # ----------------------------------------------------------------------
        # 5. GRACEFUL TEARDOWN PROTOCOL
        # ----------------------------------------------------------------------
        # Guarantees network sockets and background daemons are cancelled cleanly.
        print("\n[SYSTEM] Executing graceful shutdown sequence...")
        
        # Send explicit cancellation signals to background tasks
        mqtt_task.cancel()
        watchdog_task.cancel()
        api_task.cancel()
        dashboard_task.cancel()
        operator_remote_task.cancel()

        # Await task acknowledgments to ensure clean resource release
        await asyncio.gather(
            mqtt_task,
            watchdog_task,
            api_task,
            dashboard_task,
            operator_remote_task,
            return_exceptions=True,
        )
        print("[SYSTEM] All background daemons closed safely. Goodbye, Sir.")
       


if __name__ == "__main__":
    # --------------------------------------------------------------------------
    # MAIN EXECUTION ENTRY POINT
    # --------------------------------------------------------------------------
    # Starts the asynchronous event loop and traps OS-level keyboard interrupts (CTRL+C).
    try:
        asyncio.run(boot_sequence())
    except KeyboardInterrupt:
        print("\n[SYSTEM] Manual keyboard interrupt detected. Shutting down gracefully.")
        sys.exit(0)
