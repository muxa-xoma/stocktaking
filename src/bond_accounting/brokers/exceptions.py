"""Errors for the brokers module.

Kept in a dedicated module (unlike ``bond_accounting.bonds``) so that
callers needing only the error types do not have to import the service.
"""

from __future__ import annotations


class BrokerError(Exception):
    """Base error for broker operations."""


class BrokerNotFoundError(BrokerError):
    """Broker not found."""


class BrokerAccountNotFoundError(BrokerError):
    """Broker account not found."""


class BrokerHasAccountsError(BrokerError):
    """Deleting a broker that has accounts."""


class BrokerNameDuplicateError(BrokerError):
    """Raised when a broker name collides with an existing one."""


class BrokerAccountHasTransactionsError(BrokerError):
    """Deleting an account that has transactions."""


class BrokerAccountHasOperationsError(BrokerError):
    """Deleting an account that has account operations."""
