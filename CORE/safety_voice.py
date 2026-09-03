"""Deterministic voice wording for Core safety events."""


def safety_event_announcement(event: dict) -> str | None:
    """Return wording that never confuses a drill with a real hazard."""
    if event.get("event") != "safety_alert":
        return None

    event_type = str(event.get("event_type", "UNKNOWN"))
    active = bool(event.get("active"))
    simulated = event.get("simulated") is True
    if event_type.startswith(("FLAME_", "FIRE_")):
        hazard = "fire"
    elif event_type.startswith("GAS_"):
        hazard = "gas"
    else:
        hazard = "safety"

    if simulated and active:
        return (
            f"Sir, a controlled {hazard} emergency drill is now active. "
            "This is a simulation, not a real hazard. The physical emergency "
            "response is being tested."
        )
    if simulated:
        return (
            f"Sir, the controlled {hazard} emergency drill has ended. "
            "This was a simulation, and drill recovery is complete."
        )
    if active:
        return (
            f"Sir, VENUS has detected a real {hazard} hazard. The emergency "
            "response is active. Please prioritize your safety."
        )
    return (
        f"Sir, VENUS reports that the real {hazard} hazard has cleared. "
        "Please wait for the area to be confirmed safe before returning."
    )
