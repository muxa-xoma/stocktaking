"""Tests for the pure auth components: password hashing and JWT.

DB-backed auth service tests live in ``tests/integration/test_auth_db.py``;
they run against a temporary SQLite database created with
``alembic upgrade head`` (never ``Base.metadata.create_all``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bond_accounting.auth import JwtError, JwtService, PasswordHasher
from bond_accounting.config.settings import AuthConfig

TEST_PASSWORD = "correct-horse-battery"
TEST_USERNAME = "alice"

#: Fake, but ≥32-byte JWT secret: shorter HMAC keys trigger PyJWT's
#: ``InsecureKeyLengthWarning`` (RFC 7518 Section 3.2).
TEST_JWT_SECRET = "auth-test-jwt-secret-0123456789abcdef0123456789"


@pytest.fixture
def password_hasher() -> PasswordHasher:
    """Fresh hasher instance per test."""
    return PasswordHasher()


@pytest.fixture
def jwt_service() -> JwtService:
    """JWT service with a fixed test config."""
    return JwtService(
        AuthConfig(jwt_secret=TEST_JWT_SECRET, jwt_algorithm="HS256", jwt_expires_minutes=60)
    )


# ---------------------------------------------------------------- password


def test_hash_returns_non_empty_string(password_hasher: PasswordHasher) -> None:
    """hash() returns a non-empty string different from the plaintext."""
    hashed = password_hasher.hash(TEST_PASSWORD)
    assert isinstance(hashed, str)
    assert hashed
    assert hashed != TEST_PASSWORD
    assert TEST_PASSWORD not in hashed


def test_verify_correct_and_wrong_password(password_hasher: PasswordHasher) -> None:
    """verify() is True for the correct password, False for a wrong one."""
    hashed = password_hasher.hash(TEST_PASSWORD)
    assert password_hasher.verify(TEST_PASSWORD, hashed)
    assert not password_hasher.verify("wrong-password", hashed)


# ---------------------------------------------------------------- jwt


def test_jwt_round_trip(jwt_service: JwtService) -> None:
    """create_token() then verify_token() round-trips the payload.

    ``expires_at`` is checked with a ±1s tolerance against ``now + TTL``.
    """
    token = jwt_service.create_token(user_id=42, username=TEST_USERNAME)
    assert isinstance(token, str)
    assert token

    payload = jwt_service.verify_token(token)
    assert payload.user_id == 42
    assert payload.username == TEST_USERNAME
    expected_expiry = datetime.now(UTC) + timedelta(minutes=60)
    assert abs((payload.expires_at - expected_expiry).total_seconds()) <= 1


def test_jwt_tampered_token_raises(jwt_service: JwtService) -> None:
    """A token with a tampered payload fails verification."""
    token = jwt_service.create_token(user_id=42, username=TEST_USERNAME)
    header, payload, signature = token.split(".")
    # Flip a character in the payload segment.
    tampered = f"{header}.{payload[:-2]}{'AA' if not payload.endswith('AA') else 'BB'}.{signature}"
    with pytest.raises(JwtError):
        jwt_service.verify_token(tampered)


def test_jwt_expired_token_raises(jwt_service: JwtService) -> None:
    """A token created with a negative TTL is already expired."""
    token = jwt_service.create_token(user_id=42, username=TEST_USERNAME, expires_minutes=-1)
    with pytest.raises(JwtError):
        jwt_service.verify_token(token)
