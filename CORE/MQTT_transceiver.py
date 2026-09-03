"""
==============================================================================
VENUS CORE: MQTT TRANSCEIVER MODULE

UNS Architecture:

venus/
 |
 +-- living_room/
        |
        +-- sensor_node_01/
        |       |
        |       +-- telemetry
        |       +-- status
        |
        +-- actuator_node_01/
                |
                +-- command
                +-- ack
                +-- status


Responsibilities:
-----------------
1. Maintain MQTT connection
2. Receive telemetry
3. Forward ACK
4. Publish commands

NOT responsible for:
- Safety decisions
- AI reasoning
- Hardware control logic
- Digital Twin decisions

==============================================================================

"""

import asyncio
import json
import time
import aiomqtt


from CORE.command_protocol import (
    ensure_command_id,
    process_execution_ack,
    expire_timed_out_commands,
)


# ============================================================================
# MQTT POLICY
# ============================================================================

COMMAND_QOS = 1

ACK_TIMEOUT_SECONDS = 5.0

ACK_TIMEOUT_CHECK_INTERVAL = 0.5



# ============================================================================
# MQTT TRANSCEIVER
# ============================================================================


async def start_mqtt_transceiver(
    twin,
    outbound_queue,
    event_bus,
    broker_ip,
    broker_port=1883,
    monitoring_state=None,
):


    reconnect_interval = 1

    max_backoff = 60


    pending_commands = {}

    loop = asyncio.get_running_loop()



    # ========================================================================
    # COMMAND TIMEOUT MONITOR
    # ========================================================================

    async def monitor_command_timeouts():

        while True:

            await asyncio.sleep(
                ACK_TIMEOUT_CHECK_INTERVAL
            )

            current_time = loop.time()

            # Preserve command information before
            # expire_timed_out_commands removes it.
            expiring_commands = {
                command_id: dict(command)
                for command_id, command in pending_commands.items()
                if (
                    current_time
                    - command["published_at"]
                    >= ACK_TIMEOUT_SECONDS
                )
            }

            expired = expire_timed_out_commands(
                pending_commands,
                twin,
                current_time,
                ACK_TIMEOUT_SECONDS,
            )

            for command_id in expired:

                command = expiring_commands.get(
                    command_id,
                    {}
                )

                print(
                    f"⏱️ [COMMAND TIMEOUT] "
                    f"No ACK received: {command_id}"
                )

                await event_bus.publish(
                    {
                        "event": "command_timeout",
                        "command_id": command_id,
                        "node_path": command.get(
                            "node_path"
                        ),
                        "target": command.get(
                            "target"
                        ),
                        "state": command.get(
                            "state"
                        ),
                        "mode":
                            command.get("mode"),

                        "source":
                            command.get("source"),

                        "timestamp":
                            time.time(),
                    }
                )



    timeout_task = asyncio.create_task(
        monitor_command_timeouts()
    )



    # ========================================================================
    # MQTT MAIN LOOP
    # ========================================================================

    while True:


        try:


            async with aiomqtt.Client(
                hostname=broker_ip,
                port=broker_port,
            ) as client:



                print(
                    f"📡 [NETWORK] Connected to UNS broker {broker_ip}"
                )

                if monitoring_state is not None:
                    monitoring_state.set_service(
                        "mqtt",
                        True,
                        broker=broker_ip,
                    )



                reconnect_interval = 1



                # Subscribe to VENUS namespace

                await client.subscribe(
                    "venus/#"
                )



                # ============================================================
                # RX HANDLER
                # ============================================================

                async def listen_uplink():


                    async for message in client.messages:


                        try:


                            topic = str(
                                message.topic
                            )


                            parts = topic.split("/")
                            node_path = "/".join(
                                parts[:-1]
                            )



                            payload = (
                                message.payload
                                .decode("utf-8")
                            )



                            # ------------------------------------------------
                            # SENSOR TELEMETRY
                            #
                            # venus/
                            # living_room/
                            # sensor_node_01/
                            # telemetry
                            # ------------------------------------------------

                            # telemetry

                            if (
                                len(parts) >= 4
                                and parts[-1] == "telemetry"
                            ):

                                telemetry = json.loads(
                                    payload
                                )

                                if monitoring_state is not None:
                                    monitoring_state.record_telemetry(
                                        node_path,
                                        telemetry,
                                    )


                                # -------------------------------
                                # Sensor telemetry
                                # -------------------------------

                                if "sensors" in telemetry:

                                    for sensor_name, value in telemetry["sensors"].items():

                                        twin.update_sensor_state(
                                            node_path,
                                            sensor_name,
                                            value,
                                        )


                                # -------------------------------
                                # Actuator telemetry
                                # -------------------------------

                                if "actuators" in telemetry:

                                    for actuator_name, value in telemetry["actuators"].items():

                                        twin.update_observed_actuator_state(
                                            node_path,
                                            actuator_name,
                                            value,
                                        )


                                print(
                                    f"📥 [TELEMETRY] {node_path}"
                                )



                            # ------------------------------------------------
                            # ACTUATOR ACK
                            #
                            # venus/
                            # living_room/
                            # actuator_node_01/
                            # ack
                            # ------------------------------------------------

                            elif (
                                len(parts) >= 4
                                and parts[-1] == "ack"
                            ):

                                ack_preview = json.loads(
                                    payload
                                )

                                pending_metadata = dict(
                                    pending_commands.get(
                                        ack_preview.get("command_id"),
                                        {},
                                    )
                                )

                                result = process_execution_ack(
                                    pending_commands,
                                    twin,
                                    node_path,
                                    payload,
                                )



                                if result == "executed":


                                    ack = json.loads(
                                        payload
                                    )


                                    print(
                                        f"✅ [ACK] "
                                        f"{ack['command_id']} executed"
                                    )


                                    await event_bus.publish(
                                        {
                                            "event":
                                            "command_executed",

                                            "command_id":
                                            ack["command_id"],

                                            "node_path":
                                            node_path,

                                            "target":
                                            ack["target"],

                                            "state":
                                            ack["state"],

                                            "mode":
                                                ack.get("mode"),

                                            "angle":
                                                ack.get("angle"),

                                            "source":
                                                pending_metadata.get("source"),

                                            "timestamp":
                                                time.time(),
                                        }
                                    )



                                elif result == "failed":

                                    ack = json.loads(
                                        payload
                                    )

                                    print(
                                        f"❌ [ACK] Device failure: "
                                        f"{ack.get('command_id')}"
                                    )

                                    await event_bus.publish(
                                        {
                                            "event": "command_failed",
                                            "command_id": ack.get(
                                                "command_id"
                                            ),
                                            "node_path": node_path,
                                            "target": ack.get(
                                                "target"
                                            ),
                                            "state": ack.get(
                                                "state"
                                            ),
                                            "mode":
                                                ack.get("mode"),

                                            "angle":
                                                ack.get("angle"),

                                            "source":
                                                pending_metadata.get("source"),

                                            "timestamp":
                                                time.time(),
                                        }
                                    )



                                else:


                                    print(
                                        f"⚠️ [ACK] "
                                        f"{result}: {node_path}"
                                    )



                        except Exception as e:


                            print(
                                f"[WARN] RX processing failed: {e}"
                            )



                # ============================================================
                # TX HANDLER
                # ============================================================


                async def process_downlink():


                    while True:


                        topic, payload = (
                            await outbound_queue.get()
                        )


                        command_id = None


                        try:


                            command_id, payload = (
                                ensure_command_id(
                                    payload
                                )
                            )


                            data = json.loads(
                                payload
                            )



                            # Track actuator commands

                            if command_id:


                                parts = topic.split("/")


                                if (
                                    len(parts) >= 4
                                    and parts[-1]
                                    == "command"
                                ):


                                    node_path = "/".join(
                                        parts[:-1]
                                    )



                                    pending_commands[
                                        command_id
                                    ] = {

                                        "node_path":
                                        node_path,


                                        "target":
                                        data["device"]["name"],


                                        "state":
                                        data["action"]["value"],

                                        "mode":
                                            data["action"].get(
                                                "mode"
                                            ),

                                        "source":
                                            data.get(
                                                "source",
                                                "venus_core",
                                            ),


                                        "published_at":
                                        loop.time(),
                                    }



                            qos = (
                                COMMAND_QOS
                                if command_id
                                else 0
                            )



                            await client.publish(
                                topic,
                                payload,
                                qos=qos,
                            )

                            if command_id:
                                command = pending_commands.get(
                                    command_id,
                                    {},
                                )
                                await event_bus.publish(
                                    {
                                        "event": "command_dispatched",
                                        "command_id": command_id,
                                        "node_path": command.get("node_path"),
                                        "target": command.get("target"),
                                        "state": command.get("state"),
                                        "mode": command.get("mode"),
                                        "source": command.get("source"),
                                        "timestamp": time.time(),
                                    }
                                )



                            print(
                                f"⚡ [MQTT TX] "
                                f"{topic}"
                            )



                        except Exception as e:


                            print(
                                f"⚠️ [TX ERROR] {e}"
                            )


                            if command_id:

                                pending_commands.pop(
                                    command_id,
                                    None
                                )



                        finally:


                            outbound_queue.task_done()



                rx_task = asyncio.create_task(
                    listen_uplink()
                )


                tx_task = asyncio.create_task(
                    process_downlink()
                )



                await asyncio.gather(
                    rx_task,
                    tx_task,
                )



        except aiomqtt.MqttError as error:

            if monitoring_state is not None:
                monitoring_state.set_service(
                    "mqtt",
                    False,
                    error=str(error),
                )


            print(
                f"🚨 [MQTT ERROR] {error}"
            )


            print(
                f"🔄 Retry in {reconnect_interval}s"
            )


            await asyncio.sleep(
                reconnect_interval
            )


            reconnect_interval = min(
                reconnect_interval * 2,
                max_backoff,
            )
