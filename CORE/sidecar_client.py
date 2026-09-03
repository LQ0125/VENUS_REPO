# ==============================================================================
# VENUS SIDECAR CLIENT
# ==============================================================================
# Purpose:
# Persistent WebSocket client between VENUS Sidecar and VENUS Core.
#
# Supports:
#   1. Command requests
#   2. Telemetry requests
#   3. Core pushed events
#
# Phase 6D:
# Enables Core -> Sidecar communication.
# ==============================================================================


import asyncio
import json
import websockets


class VenusCoreClient:

    def __init__(
        self,
        core_url: str,
    ):

        self.core_url = core_url

        # Persistent websocket connection
        self.websocket = None


        # --------------------------------------------------------------
        # Separate communication channels
        #
        # Responses:
        #   command result
        #   telemetry reply
        #
        # Events:
        #   safety alert
        #   command completed
        #   system notification
        # --------------------------------------------------------------

        self.response_queue = asyncio.Queue()

        self.event_queue = asyncio.Queue()

        # Dashboard microphone requests are delivered separately so consuming
        # them cannot interfere with safety or actuator events.
        self.voice_command_queue = asyncio.Queue()

        # Completed command events indexed by command_id.
        # This prevents an ACK from being lost if it arrives before
        # send_command starts waiting for it.

        self.command_results = {}

        self.command_result_condition = asyncio.Condition()



    async def connect(self):
        """
        Establish persistent websocket connection to VENUS Core.
        """

        if self.websocket is not None:

            return


        self.websocket = await websockets.connect(
            self.core_url
        )


        print(
            "🔗 [SIDECAR] Connected to VENUS Core."
        )


        # Start background receiver

        asyncio.create_task(
            self.listen()
        )



    async def listen(self):
        """
        Permanent websocket listener.

        Separates:
        - Core events
        - Command responses
        """

        try:

            async for message in self.websocket:

                data = json.loads(
                    message
                )


                # --------------------------------------------------
                # Core pushed events
                #
                # Example:
                #
                # {
                #   "event":"command_executed"
                # }
                #
                # --------------------------------------------------

                if "event" in data:

                    event_name = data.get(
                        "event"
                    )

                    command_id = data.get(
                        "command_id"
                    )

                    if (
                        event_name
                        in {
                            "command_executed",
                            "command_failed",
                            "command_timeout",
                        }
                        and isinstance(command_id, str)
                    ):

                        async with self.command_result_condition:

                            self.command_results[
                                command_id
                            ] = data

                            self.command_result_condition.notify_all()

                    elif event_name == "voice_status_updated":

                        # Fire-and-forget monitoring acknowledgement.  Do not
                        # retain it in the sidecar event queue.
                        continue

                    elif event_name == "microphone_control_requested":

                        await self.voice_command_queue.put(
                            data
                        )

                    else:

                        # Safety alerts and other unsolicited events.
                        await self.event_queue.put(
                            data
                        )

                else:

                    await self.response_queue.put(
                        data
                    )

        except websockets.exceptions.ConnectionClosed:

            print(
                "⚠️ [SIDECAR] Core websocket disconnected."
            )


        except Exception as e:

            print(
                f"❌ [SIDECAR] WebSocket listener error: {e}"
            )



    async def send_command(
        self,
        node_path: str,
        target: str,
        state: bool,
        mode: str = "",
    ) -> dict:
        """
        Submit a command, then wait for hardware execution,
        failure, or timeout.
        """

        if self.websocket is None:

            raise RuntimeError(
                "Core websocket is not connected."
            )

        request = {
            "action": "set_actuator",
            "node_path": node_path,
            "target": target,
            "state": state,
        }

        if mode:

            request["mode"] = mode

        await self.websocket.send(
            json.dumps(request)
        )

        # This only confirms Core accepted the request.
        response = await self.response_queue.get()

        if not response.get("success"):

            return response

        command_id = (
            response
            .get("result", {})
            .get("command_id")
        )

        if not command_id:

            return {
                "success": False,
                "result": {
                    "status": "missing_command_id"
                }
            }

        try:

            final_result = (
                await self.wait_for_command_result(
                    command_id,
                    timeout=7.0,
                )
            )

        except asyncio.TimeoutError:

            return {
                "success": False,
                "result": {
                    "event": "command_timeout",
                    "command_id": command_id,
                }
            }

        return {
            "success": (
                final_result.get("event")
                == "command_executed"
            ),
            "result": final_result,
        }



    async def get_telemetry(self):
        """
        Request Digital Twin telemetry snapshot.
        """

        if self.websocket is None:

            raise RuntimeError(
                "Core websocket is not connected."
            )


        request = {

            "action": "get_telemetry"

        }


        await self.websocket.send(
            json.dumps(request)
        )


        response = await self.response_queue.get()


        return response


    async def report_voice_status(
        self,
        *,
        microphone_enabled: bool,
        controller_online: bool,
        controller_id: str,
        changed_by: str,
    ) -> None:
        """Report authoritative sidecar microphone state to Core monitoring.

        Core responds with an event-shaped acknowledgement so it cannot be
        mistaken for an actuator or telemetry response in response_queue.
        """

        if self.websocket is None:
            raise RuntimeError(
                "Core websocket is not connected."
            )

        await self.websocket.send(
            json.dumps(
                {
                    "action": "report_voice_status",
                    "microphone_enabled": bool(microphone_enabled),
                    "controller_online": bool(controller_online),
                    "controller_id": controller_id,
                    "changed_by": changed_by,
                }
            )
        )



    async def get_event(self):
        """
        Wait for Core pushed event.

        Example:

        {
            "event":"safety_alert",
            "type":"fire"
        }

        """

        event = await self.event_queue.get()


        return event

    async def get_voice_command(self):
        """Wait for an authenticated Core-to-sidecar microphone request."""

        return await self.voice_command_queue.get()

    async def wait_for_command_result(
        self,
        command_id: str,
        timeout: float = 7.0,
    ):

        async def wait_for_result():

            async with self.command_result_condition:

                await self.command_result_condition.wait_for(
                    lambda: (
                        command_id
                        in self.command_results
                    )
                )

                return self.command_results.pop(
                    command_id
                )

        return await asyncio.wait_for(
            wait_for_result(),
            timeout=timeout,
        )
