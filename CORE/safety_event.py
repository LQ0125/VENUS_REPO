# ==============================================================================
# VENUS CORE: SAFETY EVENT MODEL
# ==============================================================================
#
# Standard representation of CPS safety events.
#
# A safety event represents:
#
#   "Something important happened in the physical world."
#
# Example:
#
#   Flame sensor detects fire
#
#   becomes:
#
#   SafetyEvent(
#       event_type="FLAME_DETECTED",
#       severity="CRITICAL",
#       source="venus/living_room/sensor_node"
#   )
#
# ==============================================================================


from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, Optional



@dataclass
class SafetyEvent:
    """
    Standard CPS safety event object.
    """


    # Event classification

    event_type: str



    # Importance level

    severity: str



    # Physical system that generated event

    source: str



    # Optional sensor information

    details: Dict



    # Event creation time

    timestamp: str = None

    # Trust and provenance metadata. Physical watchdog events keep the
    # defaults; only the registered operator-remote service creates drills.

    simulated: bool = False

    origin_type: str = "physical_sensor"

    drill_id: Optional[str] = None

    actuators_enabled: bool = True



    def __post_init__(self):

        if self.timestamp is None:

            self.timestamp = datetime.now(
                timezone.utc
            ).isoformat()



    def to_dict(self) -> dict:
        """
        Convert event into a JSON-compatible dictionary.

        The 'event' field lets the Sidecar distinguish pushed events
        from replies to get_telemetry/set_actuator requests.
        """

        payload = asdict(self)

        payload["event"] = "safety_alert"

        payload["active"] = not self.event_type.endswith(
            "_CLEARED"
        )

        return payload



    def __repr__(self):

        return (
            f"SafetyEvent("
            f"type={self.event_type}, "
            f"severity={self.severity}, "
            f"source={self.source}"
            f")"
        )
