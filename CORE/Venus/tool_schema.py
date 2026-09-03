# ==============================================================================
# VENUS CORE: TOOL SCHEMA MODULE
#
# Responsibility:
#   1. Define Gemini function interfaces
#   2. Validate AI-generated commands
#   3. Convert natural language targets into authorised hardware identities
#
# IMPORTANT:
#   This module DOES NOT handle MQTT.
#   It only decides:
#
#       "Is this command allowed?"
#
# Transport is handled later by Command Protocol.
# ==============================================================================


from typing import Dict, Any, Optional


# ==============================================================================
# TRUSTED HARDWARE CAPABILITY REGISTRY
#
# This defines what each CPS node owns.
#
# Future expansion:
#
# venus/living_room/actuator_node
# vehicle/tony/car
# wearable/armor
#
# Each CPS domain can have its own actuator registry.
# ==============================================================================

ACTUATOR_NODE = "venus/living_room/actuator_node_01"


NODE_ACTUATORS = {

    ACTUATOR_NODE: {
        "led",
        "buzzer",
        "servo",
    }

}


# ==============================================================================
# APPROVED HUMAN LANGUAGE ALIASES
#
# Gemini may generate:
#
# "turn on the lamp"
#
# but the hardware identity is:
#
# "led"
#
# This layer performs the translation.
# ==============================================================================

ACTUATOR_ALIASES = {

    "led": {
        "led",
        "light",
        "rgb light",
        "main light",
        "living room light",
        "living room main light",
        "lamp",
    },

    "buzzer": {
        "buzzer",
        "alarm",
        "alarm buzzer",
        "buzzer actuator",
        "emergency buzzer",
        "warning buzzer",
        "siren",
    },

    "servo": {
        "servo",
        "door",
        "living room door",
    },

}

LIGHT_MODES = {
    "warm_white",
    "natural_white",
    "daylight",
    "off",
}


# ==============================================================================
# GEMINI FUNCTION DEFINITIONS
# ==============================================================================

SET_ACTUATOR_SCHEMA = {

    "name": "set_actuator",

    "description": (
        "Requests a physical change to the registered VENUS living-room "
        "actuator node. Hardware execution is not confirmed until an ACK is "
        "received."
    ),

    "parameters": {

        "type": "OBJECT",

        "properties": {

            "target": {

                "type": "STRING",

                "enum": [
                    "led",
                    "servo",
                    "buzzer",
                ],

                "description":
                    "Internal actuator identity: led for the main light, "
                    "servo for the door, or buzzer for the alarm."
            },


            "state": {

                "type": "BOOLEAN",

                "description":
                "Desired actuator state."
            },

            "mode": {

                "type": "STRING",

                "enum": [
                    "warm_white",
                    "natural_white",
                    "daylight",
                    "off",
                ],

                "description":
                    "Optional mode used only for led. Omit it for servo and buzzer.",
            },

        },


        "required": [
            "target",
            "state"
        ],

    },

}


VENUS_TOOLS = [
    SET_ACTUATOR_SCHEMA,
]


# ==============================================================================
# VALIDATION FIREWALL
#
# Responsibility:
#
#     Gemini output
#          |
#          v
#     Validation Firewall
#          |
#          v
#     Safe VENUS command object
#
# It DOES NOT create MQTT packets.
# ==============================================================================


def validate_tool_call(
    tool_name: str,
    arguments: Dict[str, Any],
    digital_twin_snapshot: Dict[str, Any],
) -> Optional[Dict[str, Any]]:


    if tool_name == "set_actuator":


        node_path = arguments.get("node_path")

        raw_target = arguments.get(
            "target",
            ""
        )

        state = arguments.get(
            "state"
        )

        mode = arguments.get(
            "mode"
        )


        # -----------------------------
        # Type validation
        # -----------------------------

        if not isinstance(node_path, str):

            print(
                "❌ Invalid node path"
            )

            return None


        if not isinstance(raw_target, str):

            print(
                "❌ Invalid actuator target"
            )

            return None


        if not isinstance(state, bool):

            print(
                "❌ Invalid actuator state"
            )

            return None



        # -----------------------------
        # UNS node validation
        # -----------------------------

        if node_path not in digital_twin_snapshot.get("nodes", {}):

            print(
                f"❌ Unknown UNS node: {node_path}"
            )

            return None



        available_actuators = NODE_ACTUATORS.get(
            node_path
        )


        if not available_actuators:

            print(
                f"❌ No actuator registry for {node_path}"
            )

            return None



        # -----------------------------
        # Resolve actuator identity
        # -----------------------------

        target = raw_target.lower().strip()


        resolved_target = None



        if target in available_actuators:

            resolved_target = target



        else:

            matches = [

                actuator

                for actuator in available_actuators

                if target in ACTUATOR_ALIASES.get(
                    actuator,
                    set()
                )

            ]


            if len(matches) == 1:

                resolved_target = matches[0]


            elif len(matches) > 1:

                print(
                    "❌ Ambiguous actuator"
                )

                return None



        if not resolved_target:

            print(
                f"❌ Unauthorized actuator: {raw_target}"
            )

            return None



        # -----------------------------
        # Return VENUS command object
        #
        # No MQTT here.
        # -----------------------------

        return {


            "type":
                "actuator_command",


            "node_path":
                node_path,


            "device":
                {

                    "type":
                        "actuator",

                    "name":
                        resolved_target,

                },


            "action":
                {
                    "operation":
                        "set",

                    "value":
                        state,

                    "mode":
                        (
                            "off"
                            if not state
                            else (
                                mode
                                if mode in LIGHT_MODES
                                else "natural_white"
                            )
                        )
                        if resolved_target == "led"
                        else None,
                },


        }

    return None
