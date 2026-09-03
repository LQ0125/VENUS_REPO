"""
VENUS CPS
MQTT Command Protocol Adapter

Responsibility:

Command Gateway
        |
        v
MQTT Command Protocol
        |
        v
MQTT Transceiver Queue


This layer converts internal VENUS commands
into MQTT publish requests.

It does NOT:
- connect MQTT
- publish directly
- handle ACK
- update Digital Twin
"""

import json


class MQTTCommandProtocol:

    def __init__(
        self,
        outbound_queue,
    ):

        self.outbound_queue = outbound_queue


    async def send_command(
        self,
        command,
    ):

        node_path = command["node_path"]


        topic = (
            f"{node_path}/command"
        )


        payload = json.dumps(
            command
        )


        await self.outbound_queue.put(
            (
                topic,
                payload,
            )
        )


        print(
            f"📤 [COMMAND QUEUED] {topic}"
        )