"""Compose the shared VENUS prompt for each runtime interface."""

from typing import Literal

from CORE.Venus.persona import VENUS_PERSONA
from CORE.Venus.system_context import (
    ASSIGNMENT_PRESENTATION_CONTEXT,
    VENUS_SYSTEM_CONTEXT,
)
from CORE.Venus.tool_policy import (
    TEXT_TOOL_POLICY,
    VENUS_TOOL_POLICY,
    VOICE_TOOL_POLICY,
)


InterfaceName = Literal["voice", "text"]


def build_system_instruction(interface: InterfaceName) -> str:
    """Return one ordered, non-duplicated system instruction."""
    if interface not in {"voice", "text"}:
        raise ValueError(f"Unsupported VENUS interface: {interface}")

    sections = [VENUS_PERSONA, VENUS_SYSTEM_CONTEXT]
    if ASSIGNMENT_PRESENTATION_CONTEXT.strip():
        sections.append(
            "ASSIGNMENT PRESENTATION CONTEXT\n"
            + ASSIGNMENT_PRESENTATION_CONTEXT.strip()
        )
    sections.extend(
        [
            VENUS_TOOL_POLICY,
            VOICE_TOOL_POLICY if interface == "voice" else TEXT_TOOL_POLICY,
        ]
    )
    return "\n\n".join(section.strip() for section in sections if section.strip())
