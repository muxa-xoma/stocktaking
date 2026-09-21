"""Unit tests for :mod:`bond_accounting.auth.password`.

Focus: bcrypt error branches (malformed hash strings, >72-byte passwords on
both ``hash()`` and ``verify()``, bcrypt internal failures) plus the happy
paths that anchor the contract.
"""

from __future__ import annotations

import pytest

from bond_accounting.auth.password import PasswordHasher

_BCRYPT_HASH_PREFIX = "$2b$12$"


def _long_password(byte_length: int) -> str:
    """ASCII password of exactly ``byte_length`` bytes (1 byte per char)."""
    return "a" * byte_length


@pytest.fixture
def hasher() -> PasswordHasher:
    return PasswordHasher()


# --------------------------------------------------------------------------- #
# hash(): happy path
# --------------------------------------------------------------------------- #


class TestHashHappyPath:
    def test_hash_returns_bcrypt_hash_that_verifies_back(self, hasher: PasswordHasher) -> None:
        hashed = hasher.hash("correct horse battery staple")

        assert isinstance(hashed, str)
        assert hashed.startswith(_BCRYPT_HASH_PREFIX)
        assert hasher.verify("correct horse battery staple", hashed) is True

    def test_hash_is_salted_unique_per_call(self, hasher: PasswordHasher) -> None:
        assert hasher.hash("same-password") != hasher.hash("same-password")

    def test_hash_exactly_72_bytes_is_allowed(self, hasher: PasswordHasher) -> None:
        password = _long_password(72)

        hashed = hasher.hash(password)

        assert hasher.verify(password, hashed) is True


# --------------------------------------------------------------------------- #
# hash(): >72-byte rejection
# --------------------------------------------------------------------------- #


class TestHashTooLong:
    def test_hash_rejects_password_over_72_bytes(self, hasher: PasswordHasher) -> None:
        password = _long_password(73)

        with pytest.raises(ValueError, match="72-byte limit"):
            hasher.hash(password)

    def test_hash_rejects_multibyte_password_over_72_bytes(self, hasher: PasswordHasher) -> None:
        # 40 cyrillic chars = 80 bytes > 72, even though it is only 40 chars.
        password = "ж" * 40

        assert len(password) < 72
        assert len(password.encode("utf-8")) > 72
        with pytest.raises(ValueError, match="72-byte limit"):
            hasher.hash(password)


# --------------------------------------------------------------------------- #
# hash(): bcrypt internal failure branch
# --------------------------------------------------------------------------- #


class TestHashBcryptFailure:
    @pytest.mark.parametrize("error", [ValueError("boom"), TypeError("boom")])
    def test_hash_wraps_bcrypt_error_in_value_error(
        self,
        hasher: PasswordHasher,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
    ) -> None:
        def failing_hashpw(_password: bytes, _salt: bytes) -> bytes:
            raise error

        monkeypatch.setattr("bond_accounting.auth.password.bcrypt.hashpw", failing_hashpw)

        with pytest.raises(ValueError, match="Password hashing failed"):
            hasher.hash("any-password")


# --------------------------------------------------------------------------- #
# verify(): happy path and wrong password
# --------------------------------------------------------------------------- #


class TestVerifyHappyPath:
    def test_verify_accepts_correct_password(self, hasher: PasswordHasher) -> None:
        hashed = hasher.hash("s3cret!")

        assert hasher.verify("s3cret!", hashed) is True

    def test_verify_rejects_wrong_password(self, hasher: PasswordHasher) -> None:
        hashed = hasher.hash("s3cret!")

        assert hasher.verify("wrong-password", hashed) is False


# --------------------------------------------------------------------------- #
# verify(): >72-byte password (Task 36 symmetric rejection)
# --------------------------------------------------------------------------- #


class TestVerifyTooLong:
    def test_verify_long_password_returns_false_without_bcrypt_call(
        self,
        hasher: PasswordHasher,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import Mock

        checkpw_mock = Mock(return_value=True)
        monkeypatch.setattr("bond_accounting.auth.password.bcrypt.checkpw", checkpw_mock)
        hashed = hasher.hash("short-password")

        assert hasher.verify(_long_password(73), hashed) is False
        checkpw_mock.assert_not_called()

    def test_verify_long_password_never_hashes_truncated_prefix(
        self, hasher: PasswordHasher
    ) -> None:
        # A >72-byte password sharing its 72-byte prefix with the real
        # password must NOT verify (bcrypt would truncate otherwise).
        real_password = _long_password(72)
        longer_password = real_password + "tail"
        hashed = hasher.hash(real_password)

        assert hasher.verify(longer_password, hashed) is False


# --------------------------------------------------------------------------- #
# verify(): malformed hash strings
# --------------------------------------------------------------------------- #


class TestVerifyMalformedHash:
    @pytest.mark.parametrize(
        "malformed_hash",
        [
            "not-a-bcrypt-hash",
            "",
            "   ",
            "$2b$12$",  # prefix only
            "$2b$12$abc",  # truncated salt
            "$2b$12$" + "x" * 20 + "$" + "y" * 10,  # truncated digest
            "2b12xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "$2b$99$invalid-cost",
        ],
    )
    def test_verify_malformed_hash_returns_false(
        self, hasher: PasswordHasher, malformed_hash: str
    ) -> None:
        assert hasher.verify("some-password", malformed_hash) is False

    def test_verify_truncated_real_hash_returns_false(self, hasher: PasswordHasher) -> None:
        hashed = hasher.hash("some-password")
        truncated = hashed[:-5]

        assert truncated != hashed
        assert hasher.verify("some-password", truncated) is False

    def test_verify_corrupted_real_hash_returns_false(self, hasher: PasswordHasher) -> None:
        hashed = hasher.hash("some-password")
        corrupted = hashed[:-1] + ("!" if hashed[-1] != "!" else "?")

        assert hasher.verify("some-password", corrupted) is False

    def test_verify_bcrypt_type_error_is_swallowed(
        self,
        hasher: PasswordHasher,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def failing_checkpw(_password: bytes, _hashed: bytes) -> bool:
            raise TypeError("unexpected type from bcrypt")

        monkeypatch.setattr("bond_accounting.auth.password.bcrypt.checkpw", failing_checkpw)

        assert hasher.verify("some-password", "any-hash") is False


# --------------------------------------------------------------------------- #
# Empty / whitespace password contract
# --------------------------------------------------------------------------- #


class TestEmptyPasswordContract:
    def test_hash_and_verify_empty_password(self, hasher: PasswordHasher) -> None:
        # bcrypt 5.x hashes empty strings fine; the contract is symmetric.
        hashed = hasher.hash("")

        assert hashed.startswith(_BCRYPT_HASH_PREFIX)
        assert hasher.verify("", hashed) is True
        assert hasher.verify("not-empty", hashed) is False

    def test_verify_whitespace_password_against_other_hash(self, hasher: PasswordHasher) -> None:
        hashed = hasher.hash("real password")

        assert hasher.verify("", hashed) is False
        assert hasher.verify("   ", hashed) is False
