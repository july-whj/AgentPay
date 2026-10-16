"""
Policy engine data models — Pydantic v2 value objects.

All models are frozen (immutable after construction).
Amounts are strictly int base units; floats are rejected.
EVM addresses are auto-normalised to lowercase.
CAIP-2 network identifiers are validated on construction.

References:
    - specs/domain-model.md §2.1  (ap_policy, ap_budget_ledger fields)
    - specs/policy-engine-spec.md §4.1 (interface contract)
    - infra/sqlite/buyer-schema.sql   (DDL source of truth)
"""

from __future__ import annotations

import enum
import re

from pydantic import BaseModel, field_validator

from agentpay.policy.errors import PolicyErrorCode

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# CAIP-2: namespace:reference  (e.g. eip155:84532)
_CAIP2_RE = re.compile(r"^[-a-z0-9]{3,8}:[-_a-zA-Z0-9]{1,32}$")

# Canonical EVM address: 0x followed by exactly 40 hex chars
_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


# ---------------------------------------------------------------------------
# Helper validators (reused across models)
# ---------------------------------------------------------------------------


def _validate_positive_int(value: object, field_name: str) -> int:
    """Ensure *value* is a strictly positive int (not float)."""
    if isinstance(value, float):
        raise TypeError(f"{field_name} must be an integer, got float")
    if isinstance(value, str):
        # Accept string representations from JSON test vectors
        try:
            value = int(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"{field_name} must be a positive integer, got {value!r}"
            ) from exc
    if not isinstance(value, int):
        raise TypeError(f"{field_name} must be int, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{field_name} must be > 0, got {value}")
    return value


def _validate_evm_address(value: str, field_name: str) -> str:
    """Validate EVM address format and normalise to lowercase."""
    if not _EVM_ADDRESS_RE.match(value):
        raise ValueError(
            f"{field_name}: invalid EVM address format: {value!r}. "
            "Address must match 0x followed by 40 hex characters."
        )
    return value.lower()


def _validate_caip2(value: str) -> str:
    """Validate CAIP-2 chain identifier format."""
    if not _CAIP2_RE.match(value):
        raise ValueError(
            f"Invalid CAIP-2 network identifier: {value!r}. "
            "Expected format: namespace:reference (e.g. eip155:84532)."
        )
    return value


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BreakerState(enum.Enum):
    """Circuit breaker states (policy-engine-spec §5)."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class LedgerEntryState(enum.Enum):
    """Budget ledger reservation lifecycle states (domain-model §2.1)."""

    RESERVED = "RESERVED"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"


# ---------------------------------------------------------------------------
# PolicyConfig — maps to ap_policy table
# ---------------------------------------------------------------------------


class PolicyConfig(BaseModel):
    """
    Financial firewall configuration for a single agent wallet.

    Immutable after construction. Addresses are auto-lowered, amounts must be
    strictly positive integers, and CAIP-2 network IDs are validated.

    Corresponds to the ``ap_policy`` table in buyer-schema.sql.
    """

    model_config = {"frozen": True}

    id: str
    name: str
    wallet_address: str

    # Budget limits (integer base units, e.g. USDC 6 decimals)
    max_per_tx_base_units: int
    rolling_budget_24h_base_units: int
    lifetime_budget_base_units: int | None = None

    # Whitelists — stored as frozenset for immutability & O(1) membership checks
    whitelist_payees: frozenset[str] = frozenset()
    whitelist_assets: frozenset[str] = frozenset()
    whitelist_networks: frozenset[str] = frozenset()
    whitelist_domains: frozenset[str] = frozenset()

    # Circuit breaker configuration
    circuit_max_rpm: int = 15
    circuit_consecutive_failures: int = 5
    circuit_cooldown_seconds: int = 300

    # Circuit breaker live state
    breaker_state: BreakerState = BreakerState.CLOSED
    tripped_at: int | None = None

    # Administrative lock
    is_frozen: bool = False

    # Timestamps (Unix seconds)
    created_at: int = 0
    updated_at: int = 0

    # --- Field validators ---

    @field_validator("wallet_address")
    @classmethod
    def _normalise_wallet_address(cls, v: str) -> str:
        return _validate_evm_address(v, "wallet_address")

    @field_validator("max_per_tx_base_units", mode="before")
    @classmethod
    def _validate_max_per_tx(cls, v: object) -> int:
        return _validate_positive_int(v, "max_per_tx_base_units")

    @field_validator("rolling_budget_24h_base_units", mode="before")
    @classmethod
    def _validate_rolling_budget(cls, v: object) -> int:
        return _validate_positive_int(v, "rolling_budget_24h_base_units")

    @field_validator("lifetime_budget_base_units", mode="before")
    @classmethod
    def _validate_lifetime_budget(cls, v: object) -> int | None:
        if v is None:
            return None
        return _validate_positive_int(v, "lifetime_budget_base_units")

    @field_validator("whitelist_payees", mode="before")
    @classmethod
    def _normalise_payees(cls, v: object) -> frozenset[str]:
        if isinstance(v, (list, tuple, set, frozenset)):
            return frozenset(
                _validate_evm_address(addr, "whitelist_payees") for addr in v
            )
        return frozenset()

    @field_validator("whitelist_assets", mode="before")
    @classmethod
    def _normalise_assets(cls, v: object) -> frozenset[str]:
        if isinstance(v, (list, tuple, set, frozenset)):
            return frozenset(
                _validate_evm_address(addr, "whitelist_assets") for addr in v
            )
        return frozenset()

    @field_validator("whitelist_networks", mode="before")
    @classmethod
    def _validate_networks(cls, v: object) -> frozenset[str]:
        if isinstance(v, (list, tuple, set, frozenset)):
            return frozenset(_validate_caip2(n) for n in v)
        return frozenset()

    @field_validator("whitelist_domains", mode="before")
    @classmethod
    def _normalise_domains(cls, v: object) -> frozenset[str]:
        if isinstance(v, (list, tuple, set, frozenset)):
            return frozenset(d.lower() for d in v)
        return frozenset()


# ---------------------------------------------------------------------------
# PaymentRequest — value object for a single payment evaluation request
# ---------------------------------------------------------------------------


class PaymentRequest(BaseModel):
    """
    Immutable value object representing a payment challenge to be evaluated
    by the policy engine.

    Corresponds to the parameters of ``IPolicyEngine.reserve()``.
    """

    model_config = {"frozen": True}

    intent_id: str
    amount: int
    payee: str
    asset: str
    network: str
    domain: str | None = None
    ttl_seconds: int = 360

    @field_validator("amount", mode="before")
    @classmethod
    def _validate_amount(cls, v: object) -> int:
        return _validate_positive_int(v, "amount")

    @field_validator("payee")
    @classmethod
    def _normalise_payee(cls, v: str) -> str:
        return _validate_evm_address(v, "payee")

    @field_validator("asset")
    @classmethod
    def _normalise_asset(cls, v: str) -> str:
        return _validate_evm_address(v, "asset")

    @field_validator("network")
    @classmethod
    def _validate_network(cls, v: str) -> str:
        return _validate_caip2(v)


# ---------------------------------------------------------------------------
# ReservationToken — returned when a reservation is approved
# ---------------------------------------------------------------------------


class ReservationToken(BaseModel):
    """
    Opaque token emitted by a successful ``reserve()`` call.

    Carries the reservation metadata needed for downstream commit/release.
    """

    model_config = {"frozen": True}

    reservation_id: str
    intent_id: str
    policy_id: str
    amount: int
    expires_at: int
    created_at: int


# ---------------------------------------------------------------------------
# PolicyDecision — result of a policy evaluation
# ---------------------------------------------------------------------------


class PolicyDecision(BaseModel):
    """
    Result of a policy evaluation (reserve attempt).

    If ``approved`` is True, ``reservation`` contains the token and
    ``available_after`` reflects the remaining budget.
    If ``approved`` is False, ``error_code`` explains the rejection reason.
    """

    model_config = {"frozen": True}

    approved: bool
    error_code: PolicyErrorCode | None = None
    reservation: ReservationToken | None = None
    available_after: int | None = None
