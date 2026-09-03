"""Authentication and session management for VENUS Dashboard Operator Mode.

The dashboard never receives biometric data. WebAuthn verifies a signed
challenge produced by a passkey after the device performs Face ID, Touch ID,
or another local user-verification method.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str) -> str:
    """Create a portable scrypt password hash for VENUS configuration."""
    if len(password) < 8:
        raise ValueError("Operator password must contain at least 8 characters.")

    salt = secrets.token_bytes(16)
    work_factor, block_size, parallelism = 16384, 8, 1
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=work_factor,
        r=block_size,
        p=parallelism,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            str(work_factor),
            str(block_size),
            str(parallelism),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def verify_password_hash(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded_hash.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_b64decode(expected)),
        )
        return hmac.compare_digest(actual, _b64decode(expected))
    except (TypeError, ValueError):
        return False


class OperatorAuthManager:
    """Own password verification, WebAuthn credentials, and short sessions."""

    COOKIE_NAME = "venus_operator_session"
    CEREMONY_SECONDS = 120

    def __init__(
        self,
        *,
        username: str = "operator",
        password: str | None = None,
        password_hash: str | None = None,
        rp_id: str = "localhost",
        origin: str = "http://localhost:8081",
        data_path: Path | None = None,
        idle_timeout_seconds: int = 900,
        maximum_session_seconds: int = 28800,
    ):
        self.username = username.strip() or "operator"
        self.password_hash = password_hash or (
            hash_password(password) if password else None
        )
        self.rp_id = rp_id.strip().lower()
        self.origin = origin.rstrip("/")
        self.idle_timeout_seconds = max(60, idle_timeout_seconds)
        self.maximum_session_seconds = max(
            self.idle_timeout_seconds,
            maximum_session_seconds,
        )
        self.data_path = data_path or (
            Path.home() / ".venus" / "operator_passkeys.json"
        )
        self.sessions: dict[str, dict[str, Any]] = {}
        self.ceremonies: dict[str, dict[str, Any]] = {}
        self.failed_logins: dict[str, list[float]] = {}
        self._store = self._load_store()

    @classmethod
    def from_environment(cls) -> "OperatorAuthManager":
        origin = os.environ.get(
            "VENUS_OPERATOR_ORIGIN",
            "http://localhost:8081",
        ).rstrip("/")
        default_rp_id = origin.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        return cls(
            username=os.environ.get("VENUS_OPERATOR_USERNAME", "operator"),
            password=os.environ.get("VENUS_OPERATOR_PASSWORD"),
            password_hash=os.environ.get("VENUS_OPERATOR_PASSWORD_HASH"),
            rp_id=os.environ.get("VENUS_OPERATOR_RP_ID", default_rp_id),
            origin=origin,
            data_path=Path(
                os.environ.get(
                    "VENUS_OPERATOR_DATA_FILE",
                    str(Path.home() / ".venus" / "operator_passkeys.json"),
                )
            ).expanduser(),
            idle_timeout_seconds=int(
                os.environ.get("VENUS_OPERATOR_IDLE_SECONDS", "900")
            ),
            maximum_session_seconds=int(
                os.environ.get("VENUS_OPERATOR_MAX_SESSION_SECONDS", "28800")
            ),
        )

    @property
    def password_available(self) -> bool:
        return self.password_hash is not None

    @property
    def passkey_available(self) -> bool:
        return bool(self._store["credentials"])

    @property
    def credential_count(self) -> int:
        return len(self._store["credentials"])

    @property
    def secure_cookie(self) -> bool:
        return self.origin.startswith("https://")

    def _load_store(self) -> dict[str, Any]:
        if self.data_path.exists():
            try:
                stored = json.loads(self.data_path.read_text(encoding="utf-8"))
                if isinstance(stored.get("credentials"), list) and stored.get("user_id"):
                    return stored
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "version": 1,
            "user_id": _b64encode(secrets.token_bytes(32)),
            "credentials": [],
        }

    def _save_store(self) -> None:
        self.data_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.data_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._store, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.data_path)

    @staticmethod
    def _session_key(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _cleanup(self) -> None:
        now = time.time()
        self.ceremonies = {
            key: value
            for key, value in self.ceremonies.items()
            if value["expires_at"] > now
        }
        self.sessions = {
            key: value
            for key, value in self.sessions.items()
            if now - value["last_seen"] <= self.idle_timeout_seconds
            and now - value["created_at"] <= self.maximum_session_seconds
        }
        self.failed_logins = {
            key: [stamp for stamp in values if now - stamp < 60]
            for key, values in self.failed_logins.items()
        }

    def password_attempt_allowed(self, client_key: str) -> bool:
        self._cleanup()
        return len(self.failed_logins.get(client_key, [])) < 5

    def check_password(self, client_key: str, username: Any, password: Any) -> bool:
        self._cleanup()
        valid = (
            self.password_hash is not None
            and isinstance(username, str)
            and isinstance(password, str)
            and hmac.compare_digest(username, self.username)
            and verify_password_hash(password, self.password_hash)
        )
        if valid:
            self.failed_logins.pop(client_key, None)
        else:
            self.failed_logins.setdefault(client_key, []).append(time.time())
        return valid

    def create_session(self, method: str) -> tuple[str, dict[str, Any]]:
        self._cleanup()
        token = secrets.token_urlsafe(32)
        now = time.time()
        session = {
            "username": self.username,
            "method": method,
            "created_at": now,
            "last_seen": now,
        }
        self.sessions[self._session_key(token)] = session
        return token, session

    def get_session(self, token: str | None, *, refresh: bool = True) -> dict[str, Any] | None:
        self._cleanup()
        if not token:
            return None
        session = self.sessions.get(self._session_key(token))
        if session and refresh:
            session["last_seen"] = time.time()
        return session

    def destroy_session(self, token: str | None) -> None:
        if token:
            self.sessions.pop(self._session_key(token), None)

    def session_expires_in(self, session: dict[str, Any]) -> int:
        now = time.time()
        idle_remaining = self.idle_timeout_seconds - (now - session["last_seen"])
        maximum_remaining = self.maximum_session_seconds - (
            now - session["created_at"]
        )
        return max(0, int(min(idle_remaining, maximum_remaining)))

    def _new_ceremony(
        self,
        purpose: str,
        challenge: bytes,
        session_token: str | None = None,
    ) -> str:
        ceremony_id = secrets.token_urlsafe(24)
        self.ceremonies[ceremony_id] = {
            "purpose": purpose,
            "challenge": challenge,
            "session_key": self._session_key(session_token) if session_token else None,
            "expires_at": time.time() + self.CEREMONY_SECONDS,
        }
        return ceremony_id

    def _consume_ceremony(
        self,
        ceremony_id: Any,
        purpose: str,
        session_token: str | None = None,
    ) -> bytes:
        self._cleanup()
        if not isinstance(ceremony_id, str):
            raise ValueError("Missing authentication ceremony identifier.")
        ceremony = self.ceremonies.pop(ceremony_id, None)
        expected_session = self._session_key(session_token) if session_token else None
        if (
            ceremony is None
            or ceremony["purpose"] != purpose
            or ceremony["session_key"] != expected_session
        ):
            raise ValueError("Authentication ceremony is invalid or expired.")
        return ceremony["challenge"]

    def registration_options(self, session_token: str) -> dict[str, Any]:
        credentials = [
            PublicKeyCredentialDescriptor(id=_b64decode(item["credential_id"]))
            for item in self._store["credentials"]
        ]
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name="VENUS Operator Mode",
            user_id=_b64decode(self._store["user_id"]),
            user_name=self.username,
            user_display_name="VENUS Operator",
            exclude_credentials=credentials,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        ceremony_id = self._new_ceremony(
            "registration",
            options.challenge,
            session_token,
        )
        return {
            "ceremony_id": ceremony_id,
            "publicKey": json.loads(options_to_json(options)),
        }

    def verify_registration(
        self,
        session_token: str,
        ceremony_id: Any,
        credential: Any,
    ) -> dict[str, Any]:
        challenge = self._consume_ceremony(
            ceremony_id,
            "registration",
            session_token,
        )
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=self.rp_id,
            expected_origin=self.origin,
            require_user_verification=True,
        )
        encoded_id = _b64encode(verification.credential_id)
        if any(
            item["credential_id"] == encoded_id
            for item in self._store["credentials"]
        ):
            raise ValueError("This passkey is already registered.")
        record = {
            "credential_id": encoded_id,
            "public_key": _b64encode(verification.credential_public_key),
            "sign_count": verification.sign_count,
            "created_at": time.time(),
            "device_type": verification.credential_device_type.value,
            "backed_up": verification.credential_backed_up,
        }
        self._store["credentials"].append(record)
        self._save_store()
        return record

    def authentication_options(self) -> dict[str, Any]:
        if not self.passkey_available:
            raise ValueError("No operator passkey has been registered yet.")
        options = generate_authentication_options(
            rp_id=self.rp_id,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=_b64decode(item["credential_id"]))
                for item in self._store["credentials"]
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        ceremony_id = self._new_ceremony("authentication", options.challenge)
        return {
            "ceremony_id": ceremony_id,
            "publicKey": json.loads(options_to_json(options)),
        }

    def verify_authentication(
        self,
        ceremony_id: Any,
        credential: Any,
    ) -> dict[str, Any]:
        challenge = self._consume_ceremony(ceremony_id, "authentication")
        credential_id = credential.get("id") if isinstance(credential, dict) else None
        record = next(
            (
                item
                for item in self._store["credentials"]
                if item["credential_id"] == credential_id
            ),
            None,
        )
        if record is None:
            raise ValueError("Unknown operator passkey.")
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=self.rp_id,
            expected_origin=self.origin,
            credential_public_key=_b64decode(record["public_key"]),
            credential_current_sign_count=int(record.get("sign_count", 0)),
            require_user_verification=True,
        )
        record["sign_count"] = verification.new_sign_count
        record["last_used_at"] = time.time()
        record["backed_up"] = verification.credential_backed_up
        self._save_store()
        return record


def _password_hash_cli() -> None:
    parser = argparse.ArgumentParser(description="VENUS Operator Mode utilities")
    parser.add_argument(
        "--generate-password-hash",
        action="store_true",
        help="Prompt for a password and print a scrypt configuration value.",
    )
    args = parser.parse_args()
    if not args.generate_password_hash:
        parser.print_help()
        return
    first = getpass.getpass("Operator password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise SystemExit("Passwords do not match.")
    print(hash_password(first))


if __name__ == "__main__":
    _password_hash_cli()
