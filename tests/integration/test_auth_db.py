"""DB-backed tests for the auth service.

The tests run against a temporary SQLite database created with
``alembic upgrade head`` (never ``Base.metadata.create_all``).
Pure JWT/hasher tests live in ``tests/unit/test_auth.py``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bond_accounting.auth import (
    AuthError,
    AuthService,
    InvalidCredentialsError,
    JwtService,
    PasswordHasher,
    UsernameTakenError,
)
from bond_accounting.config.settings import AuthConfig, DatabaseConfig
from bond_accounting.db import create_engine_from_settings, create_session_factory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[2]

TEST_PASSWORD = "correct-horse-battery"
TEST_USERNAME = "alice"

#: Fake, but ≥32-byte JWT secret: shorter HMAC keys trigger PyJWT's
#: ``InsecureKeyLengthWarning`` (RFC 7518 Section 3.2).
TEST_JWT_SECRET = "auth-test-jwt-secret-0123456789abcdef0123456789"


@pytest.fixture
def jwt_service() -> JwtService:
    """JWT service with a fixed test config."""
    return JwtService(
        AuthConfig(jwt_secret=TEST_JWT_SECRET, jwt_algorithm="HS256", jwt_expires_minutes=60)
    )


def _run_alembic(db_path: Path, *args: str) -> None:
    """Run ``uv run alembic <args>`` against a temp sqlite file, asserting success."""
    env = {
        **os.environ,
        "BOND_DATABASE__SQLITE_PATH": str(db_path),
        "BOND_AUTH__JWT_SECRET": TEST_JWT_SECRET,
    }
    result = subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory bound to a migrated temp sqlite database."""
    db_path = tmp_path / "auth.db"
    _run_alembic(db_path, "upgrade", "head")
    engine = create_engine_from_settings(DatabaseConfig(driver="sqlite", sqlite_path=str(db_path)))
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def auth_service(session_factory: async_sessionmaker[AsyncSession]) -> AuthService:
    """Auth service wired to the temp DB, a hasher, and the test JWT config."""
    return AuthService(
        session_factory=session_factory,
        password_hasher=PasswordHasher(),
        jwt_service=JwtService(AuthConfig(jwt_secret=TEST_JWT_SECRET)),
    )


# ---------------------------------------------------------------- service


async def test_register_creates_user(auth_service: AuthService) -> None:
    """register() persists and returns a User with the given username."""
    user = await auth_service.register(TEST_USERNAME, TEST_PASSWORD)
    assert user.id is not None
    assert user.username == TEST_USERNAME
    assert user.password_hash
    assert user.password_hash != TEST_PASSWORD


async def test_register_duplicate_username(auth_service: AuthService) -> None:
    """Registering the same username twice raises UsernameTakenError."""
    await auth_service.register(TEST_USERNAME, TEST_PASSWORD)
    with pytest.raises(UsernameTakenError):
        await auth_service.register(TEST_USERNAME, "another-valid-pass")


async def test_register_short_password(auth_service: AuthService) -> None:
    """A password shorter than 8 chars raises AuthError."""
    with pytest.raises(AuthError):
        await auth_service.register("shorty", "1234567")
    assert await auth_service.get_user_by_username("shorty") is None


async def test_login_correct_credentials(
    auth_service: AuthService, jwt_service: JwtService
) -> None:
    """login() with correct credentials returns a verifiable JWT."""
    user = await auth_service.register(TEST_USERNAME, TEST_PASSWORD)
    token = await auth_service.login(TEST_USERNAME, TEST_PASSWORD)
    assert isinstance(token, str)
    assert token
    # Verify with an independent JwtService sharing the same secret.
    assert jwt_service.verify_token(token).user_id == user.id


async def test_login_wrong_password(auth_service: AuthService) -> None:
    """login() with a wrong password raises InvalidCredentialsError."""
    await auth_service.register(TEST_USERNAME, TEST_PASSWORD)
    with pytest.raises(InvalidCredentialsError):
        await auth_service.login(TEST_USERNAME, "wrong-password")


async def test_login_nonexistent_user(auth_service: AuthService) -> None:
    """login() for an unknown user raises InvalidCredentialsError."""
    with pytest.raises(InvalidCredentialsError):
        await auth_service.login("ghost-user", TEST_PASSWORD)


async def test_get_user_by_id_and_username(auth_service: AuthService) -> None:
    """get_user_by_id / get_user_by_username find the registered user."""
    user = await auth_service.register(TEST_USERNAME, TEST_PASSWORD)

    by_id = await auth_service.get_user_by_id(user.id)
    assert by_id is not None
    assert by_id.username == TEST_USERNAME

    by_name = await auth_service.get_user_by_username(TEST_USERNAME)
    assert by_name is not None
    assert by_name.id == user.id

    assert await auth_service.get_user_by_id(999_999) is None
    assert await auth_service.get_user_by_username("nobody") is None
