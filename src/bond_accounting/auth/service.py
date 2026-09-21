"""High-level authentication service: register, login, user lookup."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bond_accounting.db.models import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from bond_accounting.auth.jwt_service import JwtService
    from bond_accounting.auth.password import PasswordHasher

logger = logging.getLogger(__name__)

#: Minimum accepted password length; shorter passwords are rejected at registration.
MIN_PASSWORD_LENGTH = 8

#: Maximum accepted password size in UTF-8 bytes; bcrypt silently truncates
#: beyond 72 bytes, so longer passwords are rejected outright.
MAX_PASSWORD_BYTES = 72

#: Placeholder written to logs instead of the real password.
_MASKED_PASSWORD = "***"  # nosec B105


class AuthError(Exception):
    """Base class for authentication errors."""


class UsernameTakenError(AuthError):
    """Registration failed because the username already exists."""


class InvalidCredentialsError(AuthError):
    """Login failed: unknown username or wrong password."""


class AuthService:
    """High-level auth operations using a DB session factory.

    Registration hashes passwords with :class:`PasswordHasher` before
    storing them; login verifies credentials and returns a signed JWT
    from :class:`JwtService`. Passwords are never written to the logs
    (masked as ``***``).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        password_hasher: PasswordHasher,
        jwt_service: JwtService,
    ) -> None:
        """Store collaborators; no I/O happens in the constructor.

        Args:
            session_factory: Opens :class:`AsyncSession` per operation.
            password_hasher: Hashes/verifies passwords (bcrypt).
            jwt_service: Creates JWTs on successful login.
        """
        self._session_factory = session_factory
        self._password_hasher = password_hasher
        self._jwt_service = jwt_service

    async def register(self, username: str, password: str) -> User:
        """Create a new user. Raise :class:`AuthError` if the username exists.

        Passwords shorter than :data:`MIN_PASSWORD_LENGTH` or longer than
        :data:`MAX_PASSWORD_BYTES` (in UTF-8 bytes) are rejected.

        Args:
            username: Unique username for the new user.
            password: Plaintext password (hashed before storage).

        Returns:
            The persisted :class:`User` (id populated after commit).

        Raises:
            AuthError: If the password is too short or exceeds 72 bytes.
            UsernameTakenError: If the username already exists.
        """
        if len(password) < MIN_PASSWORD_LENGTH:
            raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long")

        password_bytes = password.encode("utf-8")
        if len(password_bytes) > MAX_PASSWORD_BYTES:
            raise AuthError(f"Password must not exceed 72 bytes (got {len(password_bytes)} bytes)")

        password_hash = self._password_hasher.hash(password)
        user = User(username=username, password_hash=password_hash)
        async with self._session_factory() as session:
            session.add(user)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                logger.warning(
                    "Registration failed: username=%r already taken (password=%s)",
                    username,
                    _MASKED_PASSWORD,
                )
                raise UsernameTakenError(f"Username {username!r} is already taken") from exc
        logger.info(
            "Registered user id=%s username=%r (password=%s)",
            user.id,
            username,
            _MASKED_PASSWORD,
        )
        return user

    async def login(self, username: str, password: str) -> str:
        """Verify credentials and return a JWT string.

        Args:
            username: Username to authenticate.
            password: Plaintext password to verify against the stored hash.

        Returns:
            Signed JWT for the authenticated user.

        Raises:
            InvalidCredentialsError: If the username is unknown or the
                password does not match (same error for both cases, to
                avoid leaking which one failed). Passwords longer than
                bcrypt's 72-byte limit also map to this error — the
                over-long input is never even passed to bcrypt by the
                hasher, and reporting it as a distinct "password too
                long" failure would leak information and enable user
                enumeration, so it is deliberately indistinguishable
                from a wrong password.
        """
        user = await self.get_user_by_username(username)
        if user is None or not self._password_hasher.verify(password, user.password_hash):
            logger.warning(
                "Failed login for username=%r (password=%s)",
                username,
                _MASKED_PASSWORD,
            )
            raise InvalidCredentialsError("Invalid username or password")
        token = self._jwt_service.create_token(user.id, user.username)
        logger.info(
            "User id=%s username=%r logged in (password=%s)",
            user.id,
            user.username,
            _MASKED_PASSWORD,
        )
        return token

    async def get_user_by_id(self, user_id: int) -> User | None:
        """Fetch a user by primary key, or ``None`` if not found."""
        async with self._session_factory() as session:
            return await session.get(User, user_id)

    async def get_user_by_username(self, username: str) -> User | None:
        """Fetch a user by username, or ``None`` if not found."""
        async with self._session_factory() as session:
            result = await session.scalars(select(User).where(User.username == username))
            return result.one_or_none()
