"""
VENUS CPS
Command Gateway

Responsibility:
    - Receive validated requests from external interfaces
    - Create internal VENUS command objects
    - Send commands to Command Protocol

Important:
    This layer does NOT directly publish MQTT.
"""


import time
import uuid

from CORE.Venus.tool_schema import validate_tool_call


class CommandGateway:


    def __init__(
        self,
        digital_twin,
        command_protocol,
    ):

        self.digital_twin = digital_twin

        self.command_protocol = command_protocol



    async def submit_actuator_request(
        self,
        node_path,
        target,
        state,
        mode=None,
        source="venus_core",
    ):

        """
        Entry point for actuator commands.

        Example:

        User:
            "Turn on the light"

        becomes:

        VENUS command object
        """



        # -------------------------------------------------
        # Step 1
        #
        # Validate AI generated command
        #
        # This checks:
        # - node exists
        # - actuator exists
        # - action allowed
        # -------------------------------------------------

        validated = validate_tool_call(

            "set_actuator",

            {

                "node_path":
                    node_path,

                "target":
                    target,

                "state":
                    state,

                "mode":
                    mode,

            },

            self.digital_twin.snapshot(),

        )



        if validated is None:

            return {

                "status":
                    "rejected",

                "reason":
                    "validation_failed"

            }



        # -------------------------------------------------
        # Step 2
        #
        # Create VENUS command envelope
        #
        # This is the internal CPS command format
        # -------------------------------------------------

        command = {


            "command_id":
                str(uuid.uuid4()),



            "timestamp":
                time.time(),



            "source":
                source,



            "node_path":
                validated["node_path"],



            "device":
                validated["device"],



            "action":
                validated["action"],


        }



        # -------------------------------------------------
        # Step 3
        #
        # Update Digital Twin desired state
        #
        # Important:
        #
        # This is NOT actual hardware state.
        # It is VENUS intention.
        # -------------------------------------------------

        self.digital_twin.update_desired_actuator_state(

            validated["node_path"],

            validated["device"]["name"],

            validated["action"]["value"],

            mode=validated["action"].get(
                "mode"
            ),

        )



        # -------------------------------------------------
        # Step 4
        #
        # Pass to Command Protocol
        #
        # Command Protocol handles:
        # - MQTT topic
        # - serialization
        # - ACK tracking
        # -------------------------------------------------

        await self.command_protocol.send_command(

            command

        )



        return {

            "status":
                "accepted",

            "command_id":
                command["command_id"]

        }
