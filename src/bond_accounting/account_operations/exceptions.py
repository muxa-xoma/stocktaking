"""Errors for the account operations module.

Kept in a dedicated module (same as ``bond_accounting.brokers``) so that
callers needing only the error types do not have to import the service.
"""

from __future__ import annotations


class AccountOperationError(Exception):
    """Base error for account operations."""


class AccountOperationForbiddenError(AccountOperationError):
    """Account operation is owned by another user."""
