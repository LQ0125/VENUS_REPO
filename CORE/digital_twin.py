# ==============================================================================
# VENUS CORE: DIGITAL TWIN ENGINE (Phase 4B)
# ==============================================================================
#
# Purpose:
#     Digital Twin is the virtual representation of the physical CPS system.
#
# IMPORTANT DESIGN RULE:
#     Digital Twin NEVER controls hardware.
#
# It only stores:
#     1. Sensor observations
#     2. Actuator desired states (what Venus wants)
#     3. Actuator actual states (what hardware confirmed)
#     4. Node health information
#
# Phase 4B improvement:
#
# Before:
#     light = True
#
# Problem:
#     True means:
#       - Venus requested ON?
#       - Hardware confirmed ON?
#
# After:
#
#     light:
#         desired: True
#         actual: False
#         status: EXECUTING
#
# ==============================================================================

import time
from typing import Dict, Any, Optional


class DynamicDigitalTwin:

    def __init__(self):
        """
        Initialize empty Digital Twin database.

        Structure:

        {
            "node_path":
            {
                "sensors": {},
                "actuators": {},
                "metadata": {}
            }
        }
        """

        self.nodes: Dict[str, Dict[str, Any]] = {}


    # ==========================================================================
    # NODE MANAGEMENT
    # ==========================================================================

    def register_node(self, node_path: str):
        """
        Register a CPS node inside Venus UNS.

        Example:

        venus/living_room/sensor_node_01
        venus/living_room/actuator_node
        """

        if node_path not in self.nodes:

            self.nodes[node_path] = {

                "sensors": {},

                "actuators": {},

                "metadata": {
                    "registered_at": time.time()
                }
            }

    # ==========================================================================
    # SENSOR STATE
    # ==========================================================================

    def update_sensor_state(
        self,
        node_path: str,
        sensor_name: str,
        value: Any
    ):
        """
        Update physical sensor observation.

        Example:

        MQ2 smoke sensor:
            smoke = 350

        The Digital Twin simply records reality.
        """

        self.register_node(node_path)

        self.nodes[node_path]["sensors"][sensor_name] = {

            "value": value,

            "timestamp": time.time()
        }



    def get_sensor_value(
        self,
        node_path: str,
        sensor_name: str
    ) -> Optional[Any]:

        """
        Retrieve latest sensor observation.
        """

        node = self.nodes.get(node_path)

        if not node:
            return None


        sensor = node["sensors"].get(sensor_name)

        if not sensor:
            return None


        return sensor["value"]



    def get_sensor_age(
        self,
        node_path: str
    ) -> float:
        """
        Return the age in seconds of the newest sensor observation.

        Nodes without sensor telemetry are treated as infinitely stale.
        """

        node = self.nodes.get(node_path)

        if not node:
            return float("inf")


        timestamps = [
            sensor.get("timestamp")
            for sensor in node["sensors"].values()
            if sensor.get("timestamp") is not None
        ]

        if not timestamps:
            return float("inf")


        return max(
            0.0,
            time.time() - max(timestamps)
        )



    # ==========================================================================
    # ACTUATOR STATE - PHASE 4B
    # ==========================================================================

    def update_desired_actuator_state(
        self,
        node_path: str,
        actuator: str,
        state: bool,
        mode: Optional[str] = None,
    ):
        """
        Update what Venus wants the physical world to become.

        IMPORTANT:

        This does NOT mean hardware changed.

        Example:

        User:
            "Venus open window"


        Digital Twin:

            desired = OPEN
            actual  = CLOSED
            status  = REQUESTED

        """

        self.register_node(node_path)


        actuator_state = self.nodes[node_path]["actuators"].get(
            actuator
        )


        if actuator_state is None:

            actuator_state = {

                "desired": state,

                "actual": None,

                "status": "REQUESTED",

                "updated_at": time.time()
            }

        else:

            actuator_state["desired"] = state

            actuator_state["status"] = "REQUESTED"

            actuator_state["updated_at"] = time.time()

        if mode is not None:

            actuator_state["desired_mode"] = mode

        self.nodes[node_path]["actuators"][actuator] = actuator_state



    def update_confirmed_actuator_state(
        self,
        node_path: str,
        actuator: str,
        state: bool,
        mode: Optional[str] = None,
        angle: Optional[int] = None,
    ):
        """
        Update physical reality after hardware ACK.

        This function should ONLY be called after:

            ESP32/PIC confirms execution

        Example:

            desired = True
            actual  = True
            status  = CONFIRMED

        """

        self.register_node(node_path)


        actuator_state = {

            "desired":
                state,

            "actual":
                state,

            "status":
                "CONFIRMED",

            "updated_at":
                time.time(),
        }


        if mode is not None:

            actuator_state["desired_mode"] = mode

            actuator_state["actual_mode"] = mode


        if angle is not None:

            actuator_state["commanded_angle"] = angle


        self.nodes[node_path]["actuators"][
            actuator
        ] = actuator_state



    def update_observed_actuator_state(
        self,
        node_path: str,
        actuator: str,
        state: bool
    ):
        """
        Update physical actuator state reported by telemetry.

        Unlike a command ACK, unsolicited telemetry must not change
        what Venus currently desires. This is required for local reflexes,
        such as the PIC turning the buzzer on during a hazard.
        """

        self.register_node(node_path)


        actuator_state = self.nodes[node_path]["actuators"].get(
            actuator
        )


        if actuator_state is None:

            actuator_state = {

                "desired": None,

                "actual": state,

                "status": "OBSERVED",

                "updated_at": time.time()
            }

        else:

            actuator_state["actual"] = state

            actuator_state["status"] = "OBSERVED"

            actuator_state["updated_at"] = time.time()


        self.nodes[node_path]["actuators"][actuator] = actuator_state



    def update_actuator_failure(
        self,
        node_path: str,
        actuator: str
    ):
        """
        Hardware reported execution failure.

        Example:

            desired = ON
            actual  = OFF
            status  = FAILED

        """

        self.register_node(node_path)


        actuator_state = self.nodes[node_path]["actuators"].get(
            actuator
        )


        if actuator_state is None:

            self.nodes[node_path]["actuators"][actuator] = {

                "desired": None,

                "actual": None,

                "status": "FAILED",

                "updated_at": time.time()

            }

        else:

            actuator_state["status"] = "FAILED"

            actuator_state["updated_at"] = time.time()

    def update_actuator_timeout(
        self,
        node_path: str,
        actuator: str
    ):
        """
        Hardware did not respond before timeout.

        Example:

            desired:
                ON

            actual:
                OFF

            status:
                TIMEOUT
        """

        self.register_node(node_path)


        actuator_state = self.nodes[node_path]["actuators"].get(
            actuator
        )


        if actuator_state:

            actuator_state["status"] = "TIMEOUT"

            actuator_state["updated_at"] = time.time()



    # ==========================================================================
    # BACKWARD COMPATIBILITY
    # ==========================================================================

    def update_actuator_state(
        self,
        node_path: str,
        actuator: str,
        state: bool
    ):
        """
        Legacy API.

        Previous Phase 4A code used:

            update_actuator_state()

        In Phase 4B:

            ACK = physical confirmation

        Therefore this redirects to:

            update_confirmed_actuator_state()
        """

        self.update_confirmed_actuator_state(
            node_path,
            actuator,
            state
        )



    # ==========================================================================
    # QUERY FUNCTIONS
    # ==========================================================================

    def get_actuator_state(
        self,
        node_path: str,
        actuator: str
    ) -> Optional[Dict[str, Any]]:

        """
        Return complete actuator state.

        Example:

        {
            desired: True,
            actual: False,
            status: EXECUTING
        }

        """

        node = self.nodes.get(node_path)

        if not node:
            return None


        return node["actuators"].get(actuator)



    # ==========================================================================
    # SNAPSHOT
    # ==========================================================================

    def snapshot(self) -> Dict[str, Any]:
        """
        Return complete Digital Twin state.

        Used by:
            - Command Gateway
            - Venus AI tools
            - Monitoring system

        """

        return {

            "timestamp": time.time(),

            "nodes": self.nodes
        }
