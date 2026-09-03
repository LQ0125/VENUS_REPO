"""Hybrid read-only and authenticated Operator Mode dashboard server."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from CORE.monitoring_state import ACTUATOR_NODE
from CORE.operator_auth import OperatorAuthManager


ACCESS_MODE_KEY = web.AppKey("venus_access_mode", str)


class VenusDashboardServer:
    """Serve one read-only LAN site and one loopback Operator Mode site.

    Port 8080 is intentionally incapable of accepting control operations. The
    operator listener binds to loopback and is intended to sit behind Tailscale
    Serve, which supplies the trusted HTTPS endpoint.
    """

    def __init__(
        self,
        digital_twin,
        monitoring_state,
        command_gateway=None,
        voice_control_gateway=None,
        operator_auth: OperatorAuthManager | None = None,
        host: str = "0.0.0.0",
        port: int = 8080,
        operator_host: str = "127.0.0.1",
        operator_port: int = 8081,
    ):
        self.digital_twin = digital_twin
        self.monitoring_state = monitoring_state
        self.command_gateway = command_gateway
        self.voice_control_gateway = voice_control_gateway
        self.operator_auth = operator_auth
        self.host = host
        self.port = port
        self.operator_host = operator_host
        self.operator_port = operator_port
        self.frontend_dir = (
            Path(__file__).resolve().parent.parent / "DASHBOARD" / "frontend"
        )
        self._read_only_runner: web.AppRunner | None = None
        self._operator_runner: web.AppRunner | None = None

    @web.middleware
    async def security_headers(self, request: web.Request, handler):
        response = await handler(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), "
            "publickey-credentials-create=(self), "
            "publickey-credentials-get=(self)"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "font-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        if request.secure or request.headers.get("X-Forwarded-Proto") == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    @web.middleware
    async def read_only_guard(self, request: web.Request, handler):
        if request.method not in {"GET", "HEAD"}:
            return web.json_response(
                {
                    "success": False,
                    "error": "read_only_dashboard",
                    "message": "Use the secure VENUS address for Operator Mode.",
                },
                status=405,
                headers={"Allow": "GET, HEAD"},
            )
        return await handler(request)

    @web.middleware
    async def operator_request_guard(self, request: web.Request, handler):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if self.operator_auth is None:
                return self._error(
                    "operator_unavailable",
                    "Operator Mode is unavailable.",
                    503,
                )
            if request.headers.get("Origin") != self.operator_auth.origin:
                return self._error(
                    "origin_rejected",
                    "The request did not come from the configured secure VENUS address.",
                    403,
                )
            if request.content_type != "application/json":
                return self._error(
                    "json_required",
                    "Operator requests must use JSON.",
                    415,
                )
        return await handler(request)

    @staticmethod
    def _error(code: str, message: str, status: int) -> web.Response:
        response = web.json_response(
            {"success": False, "error": code, "message": message},
            status=status,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @staticmethod
    async def _json(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception as error:
            raise ValueError("Malformed JSON request.") from error
        if not isinstance(body, dict):
            raise ValueError("JSON request must be an object.")
        return body

    def _session(self, request: web.Request, *, refresh: bool = True):
        if self.operator_auth is None:
            return None
        return self.operator_auth.get_session(
            request.cookies.get(self.operator_auth.COOKIE_NAME),
            refresh=refresh,
        )

    def _require_session(self, request: web.Request):
        session = self._session(request)
        if session is None:
            raise web.HTTPUnauthorized(
                text='{"success": false, "error": "operator_locked", '
                '"message": "Unlock Operator Mode to continue."}',
                content_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
        return session

    def _set_session_cookie(self, response: web.Response, token: str) -> None:
        assert self.operator_auth is not None
        response.set_cookie(
            self.operator_auth.COOKIE_NAME,
            token,
            max_age=self.operator_auth.maximum_session_seconds,
            httponly=True,
            secure=self.operator_auth.secure_cookie,
            samesite="Strict",
            path="/",
        )

    def _clear_session_cookie(self, response: web.Response) -> None:
        assert self.operator_auth is not None
        response.del_cookie(
            self.operator_auth.COOKIE_NAME,
            path="/",
            secure=self.operator_auth.secure_cookie,
            samesite="Strict",
        )

    def _register_common_routes(self, app: web.Application) -> None:
        app.router.add_get("/", self.index)
        app.router.add_get("/assets/app.css", self.stylesheet)
        app.router.add_get("/assets/app.js", self.javascript)
        app.router.add_get("/api/snapshot", self.snapshot)
        app.router.add_get("/api/health", self.health)
        app.router.add_get("/api/events", self.events)
        app.router.add_get("/ws", self.websocket)

    def create_app(self) -> web.Application:
        """Create the permanently read-only LAN application."""
        app = web.Application(
            middlewares=[self.read_only_guard, self.security_headers],
            client_max_size=64 * 1024,
        )
        app[ACCESS_MODE_KEY] = "read_only"
        self._register_common_routes(app)
        app.router.add_get("/api/operator/session", self.operator_session)
        return app

    def create_operator_app(self) -> web.Application:
        """Create the loopback application exposed through trusted HTTPS."""
        if self.command_gateway is None or self.operator_auth is None:
            raise RuntimeError(
                "Operator Mode requires CommandGateway and OperatorAuthManager."
            )
        app = web.Application(
            middlewares=[self.operator_request_guard, self.security_headers],
            client_max_size=64 * 1024,
        )
        app[ACCESS_MODE_KEY] = "operator"
        self._register_common_routes(app)
        app.router.add_get("/api/operator/session", self.operator_session)
        app.router.add_post("/api/operator/login/password", self.password_login)
        app.router.add_post("/api/operator/logout", self.operator_logout)
        app.router.add_post(
            "/api/operator/passkeys/register/options",
            self.passkey_registration_options,
        )
        app.router.add_post(
            "/api/operator/passkeys/register/verify",
            self.passkey_registration_verify,
        )
        app.router.add_post(
            "/api/operator/passkeys/login/options",
            self.passkey_login_options,
        )
        app.router.add_post(
            "/api/operator/passkeys/login/verify",
            self.passkey_login_verify,
        )
        app.router.add_post("/api/operator/command", self.operator_command)
        app.router.add_post(
            "/api/operator/microphone",
            self.operator_microphone,
        )
        return app

    async def index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.frontend_dir / "index.html")

    async def stylesheet(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.frontend_dir / "app.css")

    async def javascript(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.frontend_dir / "app.js")

    async def snapshot(self, _request: web.Request) -> web.Response:
        response = web.json_response(
            self.monitoring_state.snapshot(self.digital_twin)
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def health(self, _request: web.Request) -> web.Response:
        snapshot = self.monitoring_state.snapshot(self.digital_twin)
        response = web.json_response(
            {
                "schema_version": snapshot["schema_version"],
                "generated_at": snapshot["generated_at"],
                "system": snapshot["system"],
                "nodes": {
                    name: {
                        "status": value["status"],
                        "last_seen": value["last_seen"],
                        "age_seconds": value["age_seconds"],
                    }
                    for name, value in snapshot["nodes"].items()
                },
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def events(self, request: web.Request) -> web.Response:
        snapshot = self.monitoring_state.snapshot(self.digital_twin)
        try:
            limit = min(max(int(request.query.get("limit", "25")), 1), 100)
        except ValueError:
            limit = 25
        response = web.json_response(
            {
                "schema_version": snapshot["schema_version"],
                "generated_at": snapshot["generated_at"],
                "events": snapshot["events"][:limit],
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def operator_session(self, request: web.Request) -> web.Response:
        access_mode = request.app[ACCESS_MODE_KEY]
        if access_mode == "read_only" or self.operator_auth is None:
            return web.json_response(
                {
                    "success": True,
                    "access_mode": "read_only",
                    "authenticated": False,
                    "secure_url": (
                        self.operator_auth.origin
                        if self.operator_auth and self.operator_auth.secure_cookie
                        else None
                    ),
                },
                headers={"Cache-Control": "no-store"},
            )

        session = self._session(request, refresh=False)
        payload = {
            "success": True,
            "access_mode": "operator",
            "authenticated": session is not None,
            "password_available": self.operator_auth.password_available,
            "passkey_available": self.operator_auth.passkey_available,
            "passkey_count": self.operator_auth.credential_count,
            "secure_context_expected": self.operator_auth.secure_cookie,
            "configured_origin": self.operator_auth.origin,
        }
        if session is not None:
            payload.update(
                {
                    "username": session["username"],
                    "method": session["method"],
                    "expires_in": self.operator_auth.session_expires_in(session),
                }
            )
        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    async def password_login(self, request: web.Request) -> web.Response:
        assert self.operator_auth is not None
        client_key = request.remote or "unknown"
        if not self.operator_auth.password_attempt_allowed(client_key):
            return self._error(
                "too_many_attempts",
                "Too many failed attempts. Wait one minute and try again.",
                429,
            )
        try:
            body = await self._json(request)
        except ValueError as error:
            return self._error("invalid_request", str(error), 400)
        if not self.operator_auth.check_password(
            client_key,
            body.get("username"),
            body.get("password"),
        ):
            return self._error(
                "invalid_credentials",
                "The operator username or password is incorrect.",
                401,
            )

        token, _session = self.operator_auth.create_session("password")
        response = web.json_response(
            {"success": True, "message": "Operator Mode unlocked."},
            headers={"Cache-Control": "no-store"},
        )
        self._set_session_cookie(response, token)
        return response

    async def operator_logout(self, request: web.Request) -> web.Response:
        assert self.operator_auth is not None
        token = request.cookies.get(self.operator_auth.COOKIE_NAME)
        self.operator_auth.destroy_session(token)
        response = web.json_response(
            {"success": True, "message": "Operator Mode locked."},
            headers={"Cache-Control": "no-store"},
        )
        self._clear_session_cookie(response)
        return response

    async def passkey_registration_options(self, request: web.Request) -> web.Response:
        assert self.operator_auth is not None
        self._require_session(request)
        token = request.cookies.get(self.operator_auth.COOKIE_NAME)
        return web.json_response(
            self.operator_auth.registration_options(token),
            headers={"Cache-Control": "no-store"},
        )

    async def passkey_registration_verify(self, request: web.Request) -> web.Response:
        assert self.operator_auth is not None
        self._require_session(request)
        token = request.cookies.get(self.operator_auth.COOKIE_NAME)
        try:
            body = await self._json(request)
            self.operator_auth.verify_registration(
                token,
                body.get("ceremony_id"),
                body.get("credential"),
            )
        except Exception:
            return self._error(
                "passkey_registration_failed",
                "The passkey could not be registered. Check the configured HTTPS hostname.",
                400,
            )
        return web.json_response(
            {"success": True, "message": "Passkey registered for Operator Mode."},
            headers={"Cache-Control": "no-store"},
        )

    async def passkey_login_options(self, _request: web.Request) -> web.Response:
        assert self.operator_auth is not None
        try:
            payload = self.operator_auth.authentication_options()
        except ValueError as error:
            return self._error("passkey_unavailable", str(error), 409)
        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    async def passkey_login_verify(self, request: web.Request) -> web.Response:
        assert self.operator_auth is not None
        try:
            body = await self._json(request)
            self.operator_auth.verify_authentication(
                body.get("ceremony_id"),
                body.get("credential"),
            )
        except Exception:
            return self._error(
                "passkey_login_failed",
                "Passkey verification failed or expired.",
                401,
            )
        token, _session = self.operator_auth.create_session("passkey")
        response = web.json_response(
            {"success": True, "message": "Operator Mode unlocked with passkey."},
            headers={"Cache-Control": "no-store"},
        )
        self._set_session_cookie(response, token)
        return response

    async def operator_command(self, request: web.Request) -> web.Response:
        self._require_session(request)
        try:
            body = await self._json(request)
        except ValueError as error:
            return self._error("invalid_request", str(error), 400)

        target = body.get("target")
        state = body.get("state")
        mode = body.get("mode")
        if target not in {"led", "servo", "buzzer"} or not isinstance(state, bool):
            return self._error(
                "invalid_command",
                "Select a known actuator and an explicit on/off state.",
                400,
            )
        if mode is not None and mode not in {
            "warm_white",
            "natural_white",
            "daylight",
            "off",
        }:
            return self._error("invalid_light_mode", "Unknown light mode.", 400)

        mqtt_online = bool(
            self.monitoring_state.services.get("mqtt", {}).get("online")
        )
        if not mqtt_online:
            return self._error(
                "mqtt_offline",
                "MQTT is disconnected; the command was not queued.",
                503,
            )

        result = await self.command_gateway.submit_actuator_request(
            node_path=ACTUATOR_NODE,
            target=target,
            state=state,
            mode=mode,
            source="dashboard_operator",
        )
        if result.get("status") != "accepted":
            return self._error(
                "command_rejected",
                "VENUS rejected the actuator command.",
                400,
            )
        return web.json_response(
            {
                "success": True,
                "message": "Command accepted; waiting for hardware acknowledgement.",
                "result": result,
            },
            status=202,
            headers={"Cache-Control": "no-store"},
        )

    async def operator_microphone(self, request: web.Request) -> web.Response:
        self._require_session(request)
        if self.voice_control_gateway is None:
            return self._error(
                "voice_control_unavailable",
                "VENUS voice control is unavailable.",
                503,
            )

        try:
            body = await self._json(request)
        except ValueError as error:
            return self._error("invalid_request", str(error), 400)

        requested_state = body.get("state")
        if not isinstance(requested_state, bool):
            return self._error(
                "invalid_microphone_state",
                "Select an explicit listening or muted state.",
                400,
            )

        result = await self.voice_control_gateway.submit_microphone_request(
            state=requested_state,
            source="dashboard_operator",
        )
        if result.get("status") != "accepted":
            return self._error(
                "sidecar_offline",
                "The voice sidecar is offline; the microphone was not changed.",
                503,
            )

        return web.json_response(
            {
                "success": True,
                "message": (
                    "Listening request sent to the voice sidecar."
                    if requested_state
                    else "Mute request sent to the voice sidecar."
                ),
                "result": result,
            },
            status=202,
            headers={"Cache-Control": "no-store"},
        )

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse(
            heartbeat=20,
            autoping=True,
            max_msg_size=4096,
        )
        await socket.prepare(request)
        queue = self.monitoring_state.subscribe()

        await socket.send_json(
            {
                "schema_version": self.monitoring_state.SCHEMA_VERSION,
                "event_type": "snapshot",
                "data": self.monitoring_state.snapshot(self.digital_twin),
            }
        )

        async def sender() -> None:
            while not socket.closed:
                message = await queue.get()
                await socket.send_json(message)

        sender_task = asyncio.create_task(sender())
        try:
            async for message in socket:
                if message.type in {WSMsgType.TEXT, WSMsgType.BINARY}:
                    await socket.send_json(
                        {
                            "event_type": "server_to_client_only",
                            "data": {
                                "message": "Use authenticated HTTPS endpoints for commands."
                            },
                        }
                    )
                elif message.type == WSMsgType.ERROR:
                    break
        finally:
            sender_task.cancel()
            await asyncio.gather(sender_task, return_exceptions=True)
            self.monitoring_state.unsubscribe(queue)
        return socket

    async def start(self) -> None:
        if not self.frontend_dir.exists():
            raise RuntimeError(
                f"Dashboard frontend directory not found: {self.frontend_dir}"
            )
        if self.command_gateway is None or self.operator_auth is None:
            raise RuntimeError("Dashboard Operator Mode dependencies are missing.")

        self._read_only_runner = web.AppRunner(self.create_app())
        self._operator_runner = web.AppRunner(self.create_operator_app())
        await self._read_only_runner.setup()
        await self._operator_runner.setup()
        read_only_site = web.TCPSite(
            self._read_only_runner,
            self.host,
            self.port,
        )
        operator_site = web.TCPSite(
            self._operator_runner,
            self.operator_host,
            self.operator_port,
        )
        await read_only_site.start()
        await operator_site.start()

        print(
            f"🖥️ [DASHBOARD] Read-only LAN view: "
            f"http://{self.host}:{self.port}"
        )
        print(
            f"🔐 [DASHBOARD] Operator backend: "
            f"http://{self.operator_host}:{self.operator_port}"
        )
        print(
            f"🔐 [DASHBOARD] Configured operator origin: "
            f"{self.operator_auth.origin}"
        )
        if not self.operator_auth.password_available:
            print(
                "⚠️ [DASHBOARD] Password login is disabled. Set "
                "VENUS_OPERATOR_PASSWORD_HASH before registering a passkey."
            )

        try:
            await asyncio.Future()
        finally:
            await asyncio.gather(
                self._read_only_runner.cleanup(),
                self._operator_runner.cleanup(),
                return_exceptions=True,
            )
