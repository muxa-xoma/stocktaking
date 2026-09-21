"""Bcrypt-based password hashing via the :mod:`bcrypt` package directly."""

from __future__ import annotations

import logging

import bcrypt

logger = logging.getLogger(__name__)

# bcrypt only considers the first 72 bytes of a password; hashing more is a
# silent truncation that misleads users about actual password strength.
_BCRYPT_MAX_PASSWORD_BYTES = 72


class PasswordHasher:
    """Bcrypt-based password hashing using the bcrypt package directly.

    All failures (malformed hash strings, bcrypt errors) are converted to a
    boolean/result contract instead of leaking bcrypt exceptions to callers.
    """

    def hash(self, plaintext: str) -> str:
        """Hash a plaintext password, return the bcrypt hash string.

        Args:
            plaintext: The password to hash; never stored in plain form.

        Returns:
            The bcrypt hash string, e.g. ``$2b$12$...``.

        Raises:
            ValueError: If the password exceeds the 72-byte bcrypt limit or
                bcrypt fails to hash the input.
        """
        password_bytes = plaintext.encode("utf-8")
        if len(password_bytes) > _BCRYPT_MAX_PASSWORD_BYTES:
            raise ValueError(
                f"Password exceeds bcrypt's 72-byte limit ({len(password_bytes)} bytes given)"
            )
        try:
            hashed = bcrypt.hashpw(password_bytes, bcrypt.gensalt())
        except (ValueError, TypeError) as exc:
            logger.error("bcrypt hashing failed: %s", exc)
            raise ValueError("Password hashing failed") from exc
        logger.debug("Hashed a password (%d chars input)", len(plaintext))
        return hashed.decode("utf-8")

    def verify(self, plaintext: str, hashed: str) -> bool:
        """Return True if plaintext matches the bcrypt hash, False otherwise.

        Malformed or unknown hash formats also return ``False`` — the
        constant-false behaviour is preferable to leaking bcrypt errors
        from a login path.

        Symmetric with :meth:`hash`, passwords longer than 72 bytes are
        rejected instead of compared: bcrypt silently truncates input to
        72 bytes, so comparing a truncated password would make login
        succeed for any password sharing a 72-byte prefix with the real
        one — an unacceptable security hole. Such passwords return
        ``False`` (invalid credentials) rather than raising, matching
        this method's boolean failure contract.
        """
        password_bytes = plaintext.encode("utf-8")
        if len(password_bytes) > _BCRYPT_MAX_PASSWORD_BYTES:
            logger.debug(
                "Rejected password verification: input exceeds bcrypt's 72-byte limit (%d bytes given)",
                len(password_bytes),
            )
            return False
        try:
            return bcrypt.checkpw(password_bytes, hashed.encode("utf-8"))
        except ValueError, TypeError:
            logger.debug("Password verification failed on a malformed hash")
            return False
