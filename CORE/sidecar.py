# ==============================================================================
# VENUS CPS: LIVEKIT VOICE SIDECAR (CORE/sidecar.py)
# ==============================================================================
import os
import sys
import json
import asyncio
from dotenv import load_dotenv

# Path injection for local module execution
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livekit.agents import (
    Agent, 
    AgentServer, 
    AgentSession, 
    JobContext, 
    cli, 
    function_tool, 
    RunContext
)
from livekit.plugins import google
from CORE.Venus.prompt_builder import build_system_instruction
from CORE.Venus.tool_schema import ACTUATOR_NODE
from CORE.sidecar_client import VenusCoreClient
from CORE.safety_voice import safety_event_announcement
from CORE.voice_control import GatedAudioInput, WirelessMicrophoneController

# ==============================================================================
# SYSTEM PRE-FLIGHT: ENVIRONMENTAL SANITIZATION
# ==============================================================================
if "LIVEKIT_LOG_LEVEL" in os.environ:
    del os.environ["LIVEKIT_LOG_LEVEL"]

if "GEMINI_API_KEY" in os.environ:
    del os.environ["GEMINI_API_KEY"]

# ------------------------------------------------------------------------------
# 1. NETWORK & MEMORY PRIMITIVES
# ------------------------------------------------------------------------------
load_dotenv(".env.local")

VENUS_CORE_WS = os.environ.get(
    "VENUS_CORE_WS",
    "ws://100.91.99.14:8000"
)

MQTT_BROKER_IP = os.environ.get(
    "MQTT_BROKER_IP",
    "127.0.0.1",
)
MQTT_BROKER_PORT = int(
    os.environ.get("MQTT_BROKER_PORT", "1883")
)

VENUS_MIC_START_MUTED = os.environ.get(
    "VENUS_MIC_START_MUTED",
    "false",
).strip().lower() in {"1", "true", "yes", "on"}

core_client = VenusCoreClient(
    VENUS_CORE_WS
)

# Only one voice actuator command may execute at a time.
#
# This is necessary because Box 2 currently tracks only one
# pending PIC command. It also prevents simultaneous sidecar
# requests from consuming each other's WebSocket responses.
ACTUATOR_COMMAND_LOCK = asyncio.Lock()

VENUS_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not VENUS_API_KEY or not VENUS_API_KEY.startswith("AQ."):
    print("🚨 [FATAL] High-security AQ. Authentication Key missing or corrupted in .env.local")
    sys.exit(1)

# ------------------------------------------------------------------------------
# 3. VOICE HARDWARE TOOLS
# ------------------------------------------------------------------------------
@function_tool(
    name="set_actuator",
    description=(
        "Controls one registered VENUS actuator and waits for physical "
        "confirmation. Use target led, servo, or buzzer. For led, mode may "
        "be warm_white, natural_white, daylight, or off."
    )
)
async def set_actuator(
    context: RunContext,
    target: str,
    state: bool,
    mode: str = "",
) -> str:

    async with ACTUATOR_COMMAND_LOCK:

        response = await core_client.send_command(
            node_path=ACTUATOR_NODE,
            target=target,
            state=state,
            mode=mode,
        )

    result = response.get(
        "result",
        {}
    )

    event_name = result.get(
        "event"
    )

    normalized_target = target.lower().strip()
    confirmed_target = str(
        result.get("target") or normalized_target
    ).lower().strip()
    success = bool(
        response.get("success")
        and event_name == "command_executed"
    )
    status = (
        "executed"
        if success
        else {
            "command_timeout": "timeout",
            "command_failed": "failed",
        }.get(event_name, "rejected")
    )

    physical_state = None
    confirmed_mode = None
    if success and confirmed_target == "led":
        confirmed_mode = result.get("mode") or mode or "natural_white"
        physical_state = confirmed_mode if state else "off"
    elif success and confirmed_target == "servo":
        physical_state = "open" if state else "closed"
    elif success and confirmed_target == "buzzer":
        physical_state = "active" if state else "inactive"

    return json.dumps(
        {
            "success": success,
            "status": status,
            "target": confirmed_target,
            "requested_state": bool(state),
            "physical_state": physical_state,
            "mode": confirmed_mode,
            "command_id": result.get("command_id"),
        }
    )

@function_tool(
    name="get_sensor_telemetry",
    description=(
        "Reads current environmental telemetry, actuator state, and the "
        "separate VENUS safety state, including real hazards and drills."
    )
)
async def get_sensor_telemetry(context: RunContext) -> str:
    """
    Requests current Digital Twin state from VENUS Core.
    """

    response = await core_client.get_telemetry()

    if not response:
        return json.dumps(
            {
                "success": False,
                "status": "unavailable",
                "message": "No telemetry received from VENUS Core.",
            }
        )

    print(
        f"📊 [VOICE READ] Core telemetry accessed: {response}"
    )

    return json.dumps(response)

# ------------------------------------------------------------------------------
# 4. LIVEKIT WEBRTC ROUTER ENTRYPOINT
# ------------------------------------------------------------------------------
server = AgentServer()

@server.rtc_session(agent_name="venus_sidecar")
async def entrypoint(ctx: JobContext):

    await ctx.connect()

    await core_client.connect()

    # Event consumption is handled by actuator tools.
    # Do not create another event queue consumer.

    system_instruction = build_system_instruction("voice")

    print("[SIDECAR] Binding Gemini Live Multimodal Engine...")

    model = google.realtime.RealtimeModel(
        model="models/gemini-3.1-flash-live-preview",
        voice="Aoede", 
        temperature=0.5, 
        api_key=VENUS_API_KEY
    )

    session = AgentSession(llm=model)
    # Start fail-closed.  The console runner attaches its audio source during
    # session.start; it is wrapped by the physical gate immediately afterward.
    session.input.set_audio_enabled(False)
    agent = Agent(
        instructions=system_instruction, 
        tools=[set_actuator, get_sensor_telemetry]
    )

    await session.start(room=ctx.room, agent=agent)

    console_audio = session.input.audio
    if console_audio is None:
        raise RuntimeError(
            "VENUS console microphone input is unavailable."
        )

    microphone_gate = GatedAudioInput(console_audio)
    session.input.audio = microphone_gate
    session.input.set_audio_enabled(False)

    microphone_controller = WirelessMicrophoneController(
        session=session,
        audio_gate=microphone_gate,
        core_client=core_client,
        broker_ip=MQTT_BROKER_IP,
        broker_port=MQTT_BROKER_PORT,
        start_enabled=not VENUS_MIC_START_MUTED,
    )
    microphone_task = asyncio.create_task(
        microphone_controller.run(),
        name="venus-wireless-microphone-controller",
    )

    async def consume_core_microphone_commands() -> None:
        while True:
            command = await core_client.get_voice_command()
            requested_state = command.get("state")
            if not isinstance(requested_state, bool):
                print("⚠️ [MICROPHONE] Ignored invalid Core request.")
                continue

            await microphone_controller.set_microphone(
                requested_state,
                changed_by="dashboard_operator",
            )

    core_microphone_task = asyncio.create_task(
        consume_core_microphone_commands(),
        name="venus-core-microphone-commands",
    )

    async def consume_core_safety_events() -> None:
        while True:
            event = await core_client.get_event()
            announcement = safety_event_announcement(event)
            if announcement is None:
                continue
            # Gemini Live preview rejects AgentSession.generate_reply(). A
            # deterministic session.say() is compatible and prevents the model
            # from reinterpreting a drill as a real hazard.
            speech = session.say(
                announcement,
                allow_interruptions=True,
            )
            await speech

    core_safety_task = asyncio.create_task(
        consume_core_safety_events(),
        name="venus-core-safety-events",
    )

    async def stop_microphone_controller() -> None:
        microphone_task.cancel()
        core_microphone_task.cancel()
        core_safety_task.cancel()
        await asyncio.gather(
            microphone_task,
            core_microphone_task,
            core_safety_task,
            return_exceptions=True,
        )

    ctx.add_shutdown_callback(stop_microphone_controller)

    print(
    "🟢 [SIDECAR LIVE] Voice pipeline operational with VENUS Core access."
)

if __name__ == "__main__":
    cli.run_app(server)
