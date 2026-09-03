# ==============================================================================
# VENUS CORE: COMMAND DELIVERY PROTOCOL (Phase 4B)
# ==============================================================================
#
# Responsibility:
#
#     Command Protocol manages the lifecycle of a physical command.
#
# It is responsible for:
#
#     1. Command correlation using command_id
#     2. ACK validation
#     3. Confirming physical execution
#     4. Updating Digital Twin actual state
#
#
# IMPORTANT CPS RULE:
#
#     Desired State  -> Command Gateway
#     Actual State   -> Hardware ACK
#
# The Digital Twin should only update actual_state after physical confirmation.
#
# ==============================================================================


import json
import uuid

from typing import Dict, Any, Optional, Tuple, List

from CORE.digital_twin import DynamicDigitalTwin



# ==============================================================================
# COMMAND ID GENERATION
# ==============================================================================


def ensure_command_id(payload: str) -> Tuple[Optional[str], str]:
    """
    Ensure every physical actuator command has a unique command_id.

    Why?

    Distributed systems require correlation.

    Example:

        Venus sends:

        command_id = abc123


        Hardware replies:

        command_id = abc123


    Venus knows exactly which command succeeded.
    """

    command_data = json.loads(payload)


    # Only physical actuator commands need ACK tracking

    device = command_data.get("device")
    action = command_data.get("action")

    if (
        not isinstance(device, dict)
        or device.get("type") != "actuator"
        or not isinstance(action, dict)
        or action.get("operation") != "set"
    ):
        return None, payload



    command_id = command_data.get("command_id")


    if not isinstance(command_id, str) or not command_id.strip():

        command_id = str(uuid.uuid4())

        command_data["command_id"] = command_id



    return command_id, json.dumps(command_data)





# ==============================================================================
# EXECUTION ACKNOWLEDGEMENT HANDLING
# ==============================================================================


def process_execution_ack(
    pending_commands: Dict[str, Dict[str, Any]],
    twin: DynamicDigitalTwin,
    node_path: str,
    ack_payload: str,
) -> str:
    """
    Process hardware execution acknowledgement.

    ACK lifecycle:

        ESP32/PIC

            |

            v

        MQTT

            |

            v

        VENUS Core

            |

            v

        Command Protocol


    Only successful ACK updates physical reality.
    """

    try:

        ack_data = json.loads(ack_payload)


    except json.JSONDecodeError:

        return "malformed"



    command_id = ack_data.get("command_id")

    status = ack_data.get("status")



    if not isinstance(command_id, str):

        return "malformed"



    if status not in {"executed", "failed"}:

        return "malformed"



    pending = pending_commands.get(command_id)



    # Unknown command

    if pending is None:

        return "ignored"



    # Prevent another node from answering this command

    if pending["node_path"] != node_path:

        return "ignored"




    # --------------------------------------------------------------------------
    # FAILURE CASE
    # --------------------------------------------------------------------------

    if status == "failed":


        #
        # Hardware rejected execution.
        #
        # Example:
        #
        # Desired:
        #     window = OPEN
        #
        # Actual:
        #     CLOSED
        #
        # Status:
        #     FAILED
        #

        twin.update_actuator_failure(

            node_path,

            pending["target"]

        )


        pending_commands.pop(command_id, None)


        return "failed"





    # --------------------------------------------------------------------------
    # SUCCESS CASE
    # --------------------------------------------------------------------------

    ack_target = ack_data.get("target")

    ack_state = ack_data.get("state")



    # Validate returned target

    if ack_target != pending["target"]:

        return "ignored"



    # Validate returned state

    if not isinstance(ack_state, bool):

        return "malformed"



    if ack_state != pending["state"]:

        return "ignored"

    expected_mode = pending.get(
        "mode"
    )


    ack_mode = ack_data.get(
        "mode"
    )


    if expected_mode is not None:

        if ack_mode != expected_mode:

            return "ignored"


    ack_angle = ack_data.get(
        "angle"
    )


    if pending["target"] == "servo":

        expected_angle = (
            90
            if pending["state"]
            else 0
        )


        if ack_angle != expected_angle:

            return "ignored"




    #
    # Physical execution confirmed.
    #
    # This is where reality changes.
    #
    # Digital Twin:
    #
    # desired = ON
    # actual  = ON
    # status  = CONFIRMED
    #

    twin.update_confirmed_actuator_state(

        node_path,

        pending["target"],

        pending["state"],

        mode=ack_mode,

        angle=ack_angle,

    )



    pending_commands.pop(command_id, None)



    return "executed"





# ==============================================================================
# TIMEOUT MANAGEMENT
# ==============================================================================


def expire_timed_out_commands(
    pending_commands: Dict[str, Dict[str, Any]],
    twin: DynamicDigitalTwin,
    current_time: float,
    timeout_seconds: float,
) -> List[str]:
    """
    Remove commands that never received hardware acknowledgement.

    Phase 4B:

    Timeout means:

        Desired:
            User requested ON

        Actual:
            Hardware did not confirm

        Status:
            TIMEOUT


    The Digital Twin must preserve this information so Venus
    understands that execution failed.
    """

    expired_ids = [

        command_id

        for command_id, command in pending_commands.items()

        if current_time - command["published_at"] >= timeout_seconds

    ]


    for command_id in expired_ids:

        command = pending_commands.get(command_id)


        if command:

            twin.update_actuator_timeout(

                command["node_path"],

                command["target"]

            )


        pending_commands.pop(
            command_id,
            None
        )


    return expired_ids