"""
Standardized policy engine error codes and exception types.

All error codes are defined in policy-engine-spec.md §2 and are language-agnostic
uppercase string constants.
"""

from __future__ import annotations

import enum


class PolicyErrorCode(enum.Enum):
    """
    Standard error codes returned by the Policy Engine.

    Each code maps to a specific evaluation failure in the precedence pipeline
    defined in policy-engine-spec.md §3.
    """

    # 1. Administrative lock
    POLICY_FROZEN = "POLICY_FROZEN"

    # 2. Circuit breaker
    CIRCUIT_BREAKER_OPEN = "CIRCUIT_BREAKER_OPEN"

    # 3. Input format
    INVALID_AMOUNT_FORMAT = "INVALID_AMOUNT_FORMAT"

    # 4. Whitelist matrix (evaluated in order: network → asset → payee → domain)
    NETWORK_NOT_WHITELISTED = "NETWORK_NOT_WHITELISTED"
    ASSET_NOT_WHITELISTED = "ASSET_NOT_WHITELISTED"
    PAYEE_NOT_WHITELISTED = "PAYEE_NOT_WHITELISTED"
    DOMAIN_NOT_WHITELISTED = "DOMAIN_NOT_WHITELISTED"

    # 5. Single-transaction cap
    SINGLE_TX_CAP_EXCEEDED = "SINGLE_TX_CAP_EXCEEDED"

    # 6. Lifetime budget
    LIFETIME_BUDGET_EXCEEDED = "LIFETIME_BUDGET_EXCEEDED"

    # 7. Rolling 24h budget
    DAILY_BUDGET_EXCEEDED = "DAILY_BUDGET_EXCEEDED"

    # Reservation lifecycle errors
    RESERVATION_EXPIRED = "RESERVATION_EXPIRED"
    RESERVATION_ALREADY_FINALIZED = "RESERVATION_ALREADY_FINALIZED"


class PolicyViolationError(Exception):
    """
    Raised when a payment request is rejected by the policy engine.

    Attributes:
        code: The standardized error code identifying the rejection reason.
        detail: Human-readable detail message for logging/debugging.
    """

    def __init__(self, code: PolicyErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}: {detail}" if detail else code.value)

    def __repr__(self) -> str:
        return f"PolicyViolationError(code={self.code.value!r}, detail={self.detail!r})"
