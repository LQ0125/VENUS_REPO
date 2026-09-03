# ==============================================================================
# VENUS CORE: REMOTE API SERVER
# ==============================================================================
# Purpose:
# Provides a remote interface for VENUS Sidecar clients.
#
# Sidecar should request actions here.
# Core remains responsible for:
# - validation
# - command generation
# - MQTT delivery
# - Digital Twin updates
# ==============================================================================


import asyncio
import json
import uuid
import websockets


class VenusAPIServer:

    def __init__(
        self,
        command_gateway,
        event_bus,
        monitoring_state=None,
        host="0.0.0.0",
        port=8000,
    ):
        self.command_gateway = command_gateway
        self.event_bus = event_bus
        self.monitoring_state = monitoring_state
        self.host = host
        self.port = port

    def build_telemetry_response(self) -> dict:
        """Return physical telemetry and separately classified safety state."""
        twin = self.command_gateway.digital_twin
        return {
            "success": True,
            "telemetry": twin.snapshot(),
            "safety": (
                self.monitoring_state.snapshot(twin).get("safety")
                if self.monitoring_state is not None
                else {
                    "status": "unavailable",
                    "active": [],
                    "recent": [],
                }
            ),
        }

    async def submit_microphone_request(
        self,
        *,
        state: bool,
        source: str,
    ) -> dict:
        """Send an explicit microphone state request to the live sidecar."""
        sidecar_online = bool(
            self.monitoring_state
            and self.monitoring_state.services
            .get("voice_sidecar", {})
            .get("online")
        )
        if not sidecar_online or not self.event_bus.listeners:
            return {
                "status": "rejected",
                "reason": "sidecar_offline",
            }

        command_id = str(uuid.uuid4())
        await self.event_bus.publish(
            {
                "event": "microphone_control_requested",
                "command_id": command_id,
                "state": bool(state),
                "source": source,
            }
        )
        return {
            "status": "accepted",
            "command_id": command_id,
            "state": bool(state),
        }


    async def handle_client(
        self,
        websocket,
    ):

        print(
            "🌐 [API] Sidecar connected."
        )
        self.event_bus.register(
            websocket
        )
        if self.monitoring_state is not None:
            self.monitoring_state.sidecar_connected()
        await websocket.send(
            json.dumps(
                {
                    "event": "connection_ready",
                    "message": "VENUS Core event channel established."
                }
            )
        )
        

        try:

            async for message in websocket:

                try:

                    request = json.loads(message)


                    action = request.get(
                        "action"
                    )


                    if action == "set_actuator":

                        result = await self.command_gateway.submit_actuator_request(
                            node_path=request["node_path"],
                            target=request["target"],
                            state=request["state"],
                            mode=request.get("mode"),
                            source="voice_sidecar",
                        )


                        response = {
                            "success": result.get("status") == "accepted",
                            "result": result,
                        }

                    elif action == "get_telemetry":
                        response = self.build_telemetry_response()

                    elif action == "report_voice_status":

                        microphone_enabled = request.get(
                            "microphone_enabled"
                        )
                        controller_online = request.get(
                            "controller_online"
                        )
                        controller_id = request.get(
                            "controller_id"
                        )
                        changed_by = request.get(
                            "changed_by"
                        )

                        if self.monitoring_state is None:
                            raise RuntimeError(
                                "Core monitoring state is unavailable."
                            )

                        if (
                            not isinstance(microphone_enabled, bool)
                            or not isinstance(controller_online, bool)
                            or not isinstance(controller_id, str)
                            or not controller_id
                            or len(controller_id) > 64
                            or not isinstance(changed_by, str)
                            or not changed_by
                            or len(changed_by) > 64
                        ):
                            raise ValueError(
                                "Invalid voice status payload."
                            )

                        self.monitoring_state.update_voice_status(
                            microphone_enabled=microphone_enabled,
                            controller_online=controller_online,
                            controller_id=controller_id,
                            changed_by=changed_by,
                        )

                        response = {
                            "event": "voice_status_updated",
                            "success": True,
                        }


                    else:

                        response = {
                            "success": False,
                            "message":
                                "Unknown action."
                        }


                    await websocket.send(
                        json.dumps(response)
                    )


                except Exception as error:

                    await websocket.send(
                        json.dumps(
                            {
                                "success": False,
                                "message": str(error),
                            }
                        )
                    )


        except websockets.exceptions.ConnectionClosed:

            pass

        finally:

            self.event_bus.unregister(
                websocket
            )

            if self.monitoring_state is not None:
                self.monitoring_state.sidecar_disconnected()

            print(
                "🌐 [API] Sidecar disconnected."
            )
        


    async def start(self):

        print(
            f"🌐 [API] Starting WebSocket server "
            f"on {self.host}:{self.port}"
        )


        async with websockets.serve(
            self.handle_client,
            self.host,
            self.port,
        ):

            await asyncio.Future()
