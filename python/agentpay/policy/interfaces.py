"""
Abstract interface for the Policy Engine.

Implementations will be provided by T2.2–T2.3 (SQLite-backed engine).
The interface follows the two-phase reservation protocol defined in
policy-engine-spec.md §4.1.

Clock injection: implementations accept a ``clock`` callable returning the
current Unix timestamp in seconds, enabling deterministic testing without
``time.sleep()`` or real-time dependencies.
"""

from __future__ import annotations

import abc

from agentpay.policy.models import PolicyDecision


class IPolicyEngine(abc.ABC):
    """
    Abstract base class for the AgentPay policy engine.

    Defines the two-phase reservation protocol:
      1. ``reserve()``  — evaluate rules, atomically lock quota
      2. ``commit()``   — finalise after successful delivery
      3. ``release()``  — return quota on failure (guarded, idempotent)
    """

    @abc.abstractmethod
    def reserve(
        self,
        intent_id: str,
        amount: int,
        payee: str,
        asset: str,
        network: str,
        domain: str | None = None,
        ttl_seconds: int = 360,
    ) -> PolicyDecision:
        """
        Atomically evaluate all precedence rules and lock quota.

        Evaluation order (short-circuit on first failure):
          1. Freeze check
          2. Circuit breaker
          3. Amount format validation
          4. Whitelist matrix (network → asset → payee → domain)
          5. Single-transaction cap
          6. Lifetime budget ceiling
          7. 24h rolling budget reservation

        Args:
            intent_id: Unique identifier for this payment intent.
            amount: Payment amount in integer base units (e.g. USDC 6 decimals).
            payee: Recipient EVM address (lowercase hex).
            asset: Token contract EVM address (lowercase hex).
            network: CAIP-2 chain identifier (e.g. ``eip155:84532``).
            domain: Optional API domain for domain whitelisting.
            ttl_seconds: Reservation time-to-live in seconds (default 360).

        Returns:
            A ``PolicyDecision`` with ``approved=True`` and a ``ReservationToken``
            on success, or ``approved=False`` with an ``error_code`` on rejection.
        """
        ...

    @abc.abstractmethod
    def commit(self, intent_id: str) -> None:
        """
        Transition a reservation from RESERVED to COMMITTED.

        Called after the upstream API returns HTTP 200 OK and the payment
        is considered successfully delivered.

        Idempotent: subsequent calls for the same ``intent_id`` are no-ops.

        Args:
            intent_id: The intent ID of the reservation to finalise.

        Raises:
            PolicyViolationError(RESERVATION_EXPIRED): If the reservation
                has expired before commit.
        """
        ...

    @abc.abstractmethod
    def release(self, intent_id: str, reason: str | None = None) -> None:
        """
        Transition a reservation from RESERVED to RELEASED, returning quota.

        Guarded: if the intent was already COMMITTED, raises
        ``RESERVATION_ALREADY_FINALIZED``. Releasing an already-RELEASED
        intent is a no-op (idempotent).

        SECURITY-REVIEW: Signatures that have been sent MUST NOT trigger
        automatic release. Only expired + on-chain-unspent authorisations
        may be released. This guard is enforced at the implementation level,
        not at this interface level.

        Args:
            intent_id: The intent ID of the reservation to release.
            reason: Optional human-readable reason for the release.

        Raises:
            PolicyViolationError(RESERVATION_ALREADY_FINALIZED): If the
                reservation has already been committed.
        """
        ...
