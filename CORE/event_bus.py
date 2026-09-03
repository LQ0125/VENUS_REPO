# ==============================================================================
# VENUS CORE: EVENT BUS
# ==============================================================================
#
# Purpose:
#
# Internal CPS event backbone.
#
# Responsibilities:
#
# 1. Broadcast events to Sidecar/UI clients
# 2. Notify internal Core modules
#
# Example:
#
# Safety Watchdog
#        |
#        v
#    Event Bus
#        |
#        +----------+
#        |          |
#        v          v
#
# Safety      User Interface
# Response
#
# ==============================================================================


import json



class VenusEventBus:


    def __init__(self):

        # External listeners
        # Example:
        # Sidecar websocket clients

        self.listeners = set()



        # Internal subscribers
        # Example:
        # Safety response handlers

        self.subscribers = []




    # ==========================================================================
    # External websocket clients
    # ==========================================================================


    def register(
        self,
        websocket
    ):

        self.listeners.add(websocket)




    def unregister(
        self,
        websocket
    ):

        self.listeners.discard(websocket)




    # ==========================================================================
    # Internal Core modules
    # ==========================================================================


    def subscribe(
        self,
        handler
    ):
        """
        Register an internal event handler.

        Example:

            event_bus.subscribe(
                safety_response_handler
            )

        """

        self.subscribers.append(handler)




    # ==========================================================================
    # Publish event
    # ==========================================================================


    async def publish(
        self,
        event: dict
    ):
        """
        Publish a CPS event.

        Flow:

            Producer
                |
                v
            Event Bus
                |
                +------ Internal handlers
                |
                +------ External clients

        """



        # --------------------------------------------------------------
        # 1. Notify internal Core components
        # --------------------------------------------------------------

        for handler in self.subscribers:

            try:

                await handler(event)


            except Exception as e:

                print(
                    f"[EVENT BUS] Handler error: {e}"
                )



        # --------------------------------------------------------------
        # 2. Notify external Sidecars
        # --------------------------------------------------------------

        if not self.listeners:

            return



        disconnected = []



        for websocket in self.listeners:

            try:

                await websocket.send(
                    json.dumps(event)
                )


            except Exception:

                disconnected.append(
                    websocket
                )



        for websocket in disconnected:

            self.unregister(
                websocket
            )