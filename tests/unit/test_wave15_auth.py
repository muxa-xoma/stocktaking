"""Wave 15 regression tests for auth/config pure units (Tasks 27, 41).

Split out of the former monolithic ``test_wave15.py``: weak-JWT-secret
startup logging and PyJWT round trip / jose-compatibility, without DB, REST
or NiceGUI.

* T27 — weak JWT secret startup warning.
* T41 — PyJWT: round-trip claims, jose-compatible token.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import logging
from typing import TYPE_CHECKING

from bond_accounting.auth import JwtService
from bond_accounting.config.settings import (
    AppConfig,
    AuthConfig,
    Settings,
    warn_if_weak_jwt_secret,
)

if TYPE_CHECKING:
    import pytest

#: Fake, but ≥32-byte JWT secret: shorter HMAC keys trigger PyJWT's
#: ``InsecureKeyLengthWarning`` (RFC 7518 Section 3.2).
JWT_SECRET = "wave15-test-jwt-secret-0123456789abcdef012345"


# =========================================================================== #
# 1. Weak JWT secret guard (Task 27)
# =========================================================================== #


def _settings(jwt_secret: str, debug: bool) -> Settings:
    return Settings(app=AppConfig(debug=debug), auth=AuthConfig(jwt_secret=jwt_secret))


def test_weak_jwt_secret_logs_critical(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.CRITICAL, logger="bond_accounting.config.settings"):
        warn_if_weak_jwt_secret(_settings("test-secret", debug=False))
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical
    assert "weak" in critical[0].getMessage().lower()


def test_strong_jwt_secret_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.CRITICAL, logger="bond_accounting.config.settings"):
        warn_if_weak_jwt_secret(_settings("strong-unique-value", debug=False))
    assert [r for r in caplog.records if r.levelno == logging.CRITICAL] == []


def test_weak_jwt_secret_silent_in_debug_mode(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="bond_accounting.config.settings"):
        warn_if_weak_jwt_secret(_settings("test-secret", debug=True))
    # The check is skipped entirely in debug mode — not even a DEBUG record.
    assert caplog.records == []


# =========================================================================== #
# 2. PyJWT migration (Task 41)
# =========================================================================== #


def test_jwt_round_trip_claims(jwt_service: JwtService) -> None:
    token = jwt_service.create_token(user_id=42, username="carol")
    payload = jwt_service.verify_token(token)
    assert payload.user_id == 42
    assert payload.username == "carol"
    assert payload.expires_at > datetime.datetime.now(datetime.UTC)


def test_jose_compatible_token_verifies() -> None:
    """A token produced by any RFC 7515 HS256 signer (e.g. python-jose, the
    pre-migration library) is a plain compact JWS and must verify.

    ``python-jose`` is not installed here, so the token is hand-built with
    ``hmac``/``hashlib`` — byte-for-byte the same format jose emits.
    """

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    now = int(datetime.datetime.now(datetime.UTC).timestamp())
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = b64url(
        json.dumps({"sub": "7", "username": "carol", "iat": now, "exp": now + 3600}).encode()
    )
    signature = b64url(
        hmac.new(JWT_SECRET.encode(), f"{header}.{claims}".encode(), hashlib.sha256).digest()
    )
    token = f"{header}.{claims}.{signature}"

    payload = JwtService(AuthConfig(jwt_secret=JWT_SECRET)).verify_token(token)
    assert payload.user_id == 7
    assert payload.username == "carol"
