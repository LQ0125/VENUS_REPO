# ==============================================================================
# VENUS CORE: COGNITIVE AGENT MODULE (CORE/Venus/agent.py)
# ==============================================================================
# Description: The High-Level Reasoning Engine. Connects asynchronously to the 
# Gemini API, injecting the real-time RAM state of the house alongside the 
# strict tool execution boundaries to process user commands and anomaly events.
# ==============================================================================

import os
import json
import asyncio
from typing import Optional

# Using the modern Google GenAI SDK for optimal async support
from google import genai
from google.genai import types

from CORE.command_gateway import CommandGateway
from CORE.digital_twin import DynamicDigitalTwin
from CORE.mqtt_command_protocol import MQTTCommandProtocol
from CORE.Venus.prompt_builder import build_system_instruction
from CORE.Venus.tool_schema import ACTUATOR_NODE, VENUS_TOOLS

class VenusAgent:
    def __init__(self, api_key: Optional[str] = None):
        """
        Initializes the AI agent. Assumes GEMINI_API_KEY is set in the environment,
        otherwise uses the explicitly provided key.
        """
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("[ERROR] Gemini API Key not found. Export GEMINI_API_KEY.")
            
        # Initialize the asynchronous GenAI client
        self.client = genai.Client(api_key=self.api_key)
        
        # We use flash for speed (crucial in IoT architectures)
        self.model_name = "gemini-2.5-flash"
        
        # ----------------------------------------------------------------------
        # SYSTEM INSTRUCTIONS
        # ----------------------------------------------------------------------
        # This string sets the persona and operational rules for the LLM.
        self.system_instruction = build_system_instruction("text")

    async def process_intent(
        self, 
        user_prompt: str, 
        twin: DynamicDigitalTwin, 
        outbound_queue: asyncio.Queue
    ) -> str:
        """
        Takes a human command, injects the digital twin RAM snapshot for context,
        and asks Gemini to reason about the required hardware actions.
        """
        
        # 1. CONTEXT INGESTION (O(1) Memory Read)
        # We grab the instantaneous state of the house without touching the network.
        current_state = twin.snapshot()
        command_gateway = CommandGateway(
            twin,
            MQTTCommandProtocol(outbound_queue),
        )
        
        # 2. PROMPT ENGINEERING
        # We wrap the user's request with the live data so Gemini knows the exact 
        # state of the house before it decides to take action.
        contextual_prompt = (
            f"CURRENT HARDWARE STATE (JSON):\n{json.dumps(current_state, indent=2)}\n\n"
            f"USER COMMAND: {user_prompt}"
        )
        
        try:
            # 3. ASYNCHRONOUS API CALL
            # We configure the request to strictly adhere to our predefined tools.
            print(f"🧠 [VENUS AI] Reasoning over intent: '{user_prompt}'")
            
            # Using client.aio allows the 50ms Watchdog loop to keep spinning
            # in the background while we wait ~1 second for Google's servers.
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=contextual_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=self.system_instruction,
                    # Wrap the raw schemas inside a Tool object's function_declarations list
                    tools=[types.Tool(function_declarations=VENUS_TOOLS)], 
                    temperature=0.0        
                )
            )

            # 4. TOOL CALL PARSING & VALIDATION
            # If Gemini decides hardware action is necessary, it will return a function call
            # instead of plain text. We iterate through the response to check for these.
            queued_targets = []
            rejected_targets = []
            if response.candidates and response.candidates[0].content.parts:
                for part in response.candidates[0].content.parts:
                    
                    if part.function_call:
                        tool_name = part.function_call.name
                        raw_args = dict(part.function_call.args)
                        
                        print(f"⚡ [VENUS AI] Intent recognized: {tool_name} with args: {raw_args}")
                        
                        if tool_name != "set_actuator":
                            continue

                        # The fixed UNS path is trusted application context, not
                        # an argument the language model is allowed to invent.
                        result = await command_gateway.submit_actuator_request(
                            node_path=ACTUATOR_NODE,
                            target=raw_args.get("target"),
                            state=raw_args.get("state"),
                            mode=raw_args.get("mode"),
                            source="terminal_agent",
                        )
                        target = str(raw_args.get("target", "actuator"))
                        if result.get("status") == "accepted":
                            queued_targets.append(target)
                            print(
                                f"✅ [VENUS AI] Command {result.get('command_id')} "
                                "validated and queued for MQTT dispatch."
                            )
                        else:
                            rejected_targets.append(target)
            
            # 7. RETURN NATURAL LANGUAGE TEXT
            # Gemini usually provides a text response alongside the function call 
            # (e.g., "I have turned on the light"). We return this to the UI/Sidecar.
            if queued_targets or rejected_targets:
                clauses = []
                if queued_targets:
                    clauses.append(
                        "Queued for hardware execution: "
                        + ", ".join(queued_targets)
                        + ". Physical execution is awaiting acknowledgement."
                    )
                if rejected_targets:
                    clauses.append(
                        "Rejected by Core validation: "
                        + ", ".join(rejected_targets)
                        + "."
                    )
                return " ".join(clauses)
            if response.text:
                return response.text
            return "No hardware command was requested."

        except Exception as e:
            # NON-CRASHING FAULT TOLERANCE
            # If the internet drops or the API rate-limits us, the exception is caught here.
            # Because it is isolated, the local Watchdog and Microcontrollers keep working.
            print(f"🚨 [AI FAULT] LLM reasoning failed: {e}")
            return "I am currently unable to reach my cognitive core due to a network error."
