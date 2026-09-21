"""JWT token creation and verification via :mod:`jwt` (PyJWT)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import jwt

if TYPE_CHECKING:
    from bond_accounting.config.settings import AuthConfig

logger = logging.getLogger(__name__)


class JwtError(Exception):
    """Raised when a JWT cannot be created or fails verification."""


@dataclass(frozen=True)
class TokenPayload:
    """Verified contents of a JWT.

    Attributes:
        user_id: Subject claim (``sub``) as an integer.
        username: The ``username`` claim.
        expires_at: Token expiry (``exp`` claim) as a timezone-aware datetime.
    """

    user_id: int
    username: str
    expires_at: datetime


class JwtService:
    """JWT token creation and verification.

    Tokens carry ``sub`` (str of the user id), ``username``, ``iat`` and
    ``exp`` (int unix timestamps), signed with the secret and algorithm
    from :class:`~bond_accounting.config.settings.AuthConfig`.
    """

    def __init__(self, auth_config: AuthConfig) -> None:
        """Store the auth configuration (secret, algorithm, default TTL).

        Args:
            auth_config: Provides ``jwt_secret``, ``jwt_algorithm`` and
                ``jwt_expires_minutes`` (the default TTL).
        """
        self._config = auth_config

    def create_token(self, user_id: int, username: str, expires_minutes: int | None = None) -> str:
        """Create a signed JWT with sub=<user_id>, username, iat, exp.

        Args:
            user_id: Numeric user id; stored as ``sub`` (string).
            username: Stored verbatim in the ``username`` claim.
            expires_minutes: TTL in minutes; defaults to
                ``auth_config.jwt_expires_minutes``.

        Returns:
            The compact signed JWT string.

        Raises:
            JwtError: If signing fails.
        """
        minutes = self._config.jwt_expires_minutes if expires_minutes is None else expires_minutes
        now = datetime.now(UTC)
        claims = {
            "sub": str(user_id),
            "username": username,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=minutes)).timestamp()),
        }
        try:
            token = jwt.encode(
                claims, self._config.jwt_secret, algorithm=self._config.jwt_algorithm
            )
        except jwt.PyJWTError as exc:
            raise JwtError(f"Failed to create JWT: {exc}") from exc
        logger.debug("Created JWT for user_id=%s username=%s ttl=%dmin", user_id, username, minutes)
        return token

    def verify_token(self, token: str) -> TokenPayload:
        """Verify signature + expiry and return the token payload.

        Args:
            token: The compact JWT string to verify.

        Returns:
            :class:`TokenPayload` with ``user_id`` parsed from ``sub``,
            ``username`` and ``expires_at`` (from the ``exp`` claim).

        Raises:
            JwtError: On a bad signature, tampered payload, expiry, or
                missing/malformed claims.
        """
        try:
            claims = jwt.decode(
                token,
                self._config.jwt_secret,
                algorithms=[self._config.jwt_algorithm],
            )
        except jwt.PyJWTError as exc:
            # Covers expiry (ExpiredSignatureError), bad signature
            # (InvalidSignatureError) and decode errors (DecodeError);
            # all of them subclass InvalidTokenError/PyJWTError.
            raise JwtError(f"Invalid JWT: {exc}") from exc

        try:
            user_id = int(claims["sub"])
            username = claims["username"]
            expires_at = datetime.fromtimestamp(claims["exp"], tz=UTC)
        except (KeyError, TypeError, ValueError) as exc:
            raise JwtError(f"JWT is missing required claims or they are malformed: {exc}") from exc

        logger.debug("Verified JWT for user_id=%d username=%s", user_id, username)
        return TokenPayload(user_id=user_id, username=username, expires_at=expires_at)
