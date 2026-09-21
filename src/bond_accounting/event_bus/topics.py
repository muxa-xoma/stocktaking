"""Canonical topic registry for the internal event bus.

Every topic used anywhere in the application MUST be declared on
:class:`Topic` (and therefore in :data:`ALL_TOPICS`): the event bus rejects
publishes, subscriptions, and requests for unknown topics with
``ValueError``. To introduce a new topic, add a constant to :class:`Topic`
and include it in :data:`ALL_TOPICS` — there is no runtime registration
path on purpose, so that topics stay greppable and reviewable.
"""

from __future__ import annotations


class Topic:
    """Namespace of all topics known to the event bus."""

    BOND_CREATED = "bond.created"
    BOND_UPDATED = "bond.updated"
    BOND_DELETED = "bond.deleted"
    BROKER_CREATED = "broker.created"
    BROKER_UPDATED = "broker.updated"
    BROKER_DELETED = "broker.deleted"
    BROKER_ACCOUNT_CREATED = "broker_account.created"
    BROKER_ACCOUNT_UPDATED = "broker_account.updated"
    BROKER_ACCOUNT_DELETED = "broker_account.deleted"
    TRANSACTION_CREATED = "transaction.created"
    POSITION_UPDATED = "position.updated"
    PORTFOLIO_RECALCULATED = "portfolio.recalculated"


#: All valid topics; the event bus validates against this set.
#: Keep in sync with the constants on :class:`Topic`.
ALL_TOPICS: frozenset[str] = frozenset(
    {
        Topic.BOND_CREATED,
        Topic.BOND_UPDATED,
        Topic.BOND_DELETED,
        Topic.BROKER_CREATED,
        Topic.BROKER_UPDATED,
        Topic.BROKER_DELETED,
        Topic.BROKER_ACCOUNT_CREATED,
        Topic.BROKER_ACCOUNT_UPDATED,
        Topic.BROKER_ACCOUNT_DELETED,
        Topic.TRANSACTION_CREATED,
        Topic.POSITION_UPDATED,
        Topic.PORTFOLIO_RECALCULATED,
    }
)
