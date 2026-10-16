"""
Tests for AgentPay Policy Engine data models, error codes, config loading,
and abstract interface definitions.

Task: T2.1 — Policy data model and configuration schema.
"""

from __future__ import annotations

import re

import pytest

# ---------------------------------------------------------------------------
# 1. Error Codes
# ---------------------------------------------------------------------------


class TestPolicyErrorCodes:
    """Verify all 12 standardised error codes from policy-engine-spec §2."""

    def test_all_error_codes_defined(self) -> None:
        from agentpay.policy.errors import PolicyErrorCode

        expected = {
            "POLICY_FROZEN",
            "CIRCUIT_BREAKER_OPEN",
            "INVALID_AMOUNT_FORMAT",
            "NETWORK_NOT_WHITELISTED",
            "ASSET_NOT_WHITELISTED",
            "PAYEE_NOT_WHITELISTED",
            "DOMAIN_NOT_WHITELISTED",
            "SINGLE_TX_CAP_EXCEEDED",
            "LIFETIME_BUDGET_EXCEEDED",
            "DAILY_BUDGET_EXCEEDED",
            "RESERVATION_EXPIRED",
            "RESERVATION_ALREADY_FINALIZED",
        }
        actual = {e.value for e in PolicyErrorCode}
        assert actual == expected

    def test_error_code_values_are_uppercase_strings(self) -> None:
        from agentpay.policy.errors import PolicyErrorCode

        for code in PolicyErrorCode:
            assert code.value == code.value.upper()
            assert re.match(r"^[A-Z_]+$", code.value)

    def test_policy_violation_error_carries_code_and_message(self) -> None:
        from agentpay.policy.errors import PolicyErrorCode, PolicyViolationError

        err = PolicyViolationError(
            code=PolicyErrorCode.SINGLE_TX_CAP_EXCEEDED,
            detail="amount 1500000 exceeds cap 1000000",
        )
        assert err.code is PolicyErrorCode.SINGLE_TX_CAP_EXCEEDED
        assert "1500000" in str(err)
        assert "SINGLE_TX_CAP_EXCEEDED" in repr(err)


# ---------------------------------------------------------------------------
# 2. BreakerState & LedgerEntryState enums
# ---------------------------------------------------------------------------


class TestEnums:
    def test_breaker_state_values(self) -> None:
        from agentpay.policy.models import BreakerState

        assert set(BreakerState) == {
            BreakerState.CLOSED,
            BreakerState.OPEN,
            BreakerState.HALF_OPEN,
        }

    def test_ledger_entry_state_values(self) -> None:
        from agentpay.policy.models import LedgerEntryState

        assert set(LedgerEntryState) == {
            LedgerEntryState.RESERVED,
            LedgerEntryState.COMMITTED,
            LedgerEntryState.RELEASED,
        }


# ---------------------------------------------------------------------------
# 3. PolicyConfig model
# ---------------------------------------------------------------------------


def _make_policy_dict(**overrides: object) -> dict:
    """Return a minimal valid policy config dict; override individual fields."""
    base: dict = {
        "id": "pol_test_01",
        "name": "Test Policy",
        "wallet_address": "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        "max_per_tx_base_units": 1_000_000,
        "rolling_budget_24h_base_units": 10_000_000,
        "whitelist_payees": [
            "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
        ],
        "whitelist_assets": [
            "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        ],
        "whitelist_networks": ["eip155:84532"],
        "whitelist_domains": ["api.trusted-provider.com"],
        "created_at": 1729000000,
        "updated_at": 1729000000,
    }
    base.update(overrides)
    return base


class TestPolicyConfig:
    """Validate PolicyConfig Pydantic model constraints."""

    def test_valid_minimal_config(self) -> None:
        from agentpay.policy.models import BreakerState, PolicyConfig

        cfg = PolicyConfig(**_make_policy_dict())
        assert cfg.id == "pol_test_01"
        assert cfg.name == "Test Policy"
        # Defaults
        assert cfg.lifetime_budget_base_units is None
        assert cfg.circuit_max_rpm == 15
        assert cfg.circuit_consecutive_failures == 5
        assert cfg.circuit_cooldown_seconds == 300
        assert cfg.breaker_state is BreakerState.CLOSED
        assert cfg.tripped_at is None
        assert cfg.is_frozen is False

    # --- Address normalisation (auto-lowercase) ---

    def test_wallet_address_normalised_to_lowercase(self) -> None:
        from agentpay.policy.models import PolicyConfig

        cfg = PolicyConfig(
            **_make_policy_dict(
                wallet_address="0xF39FD6E51AAD88F6F4CE6AB8827279CFFFB92266",
            )
        )
        assert cfg.wallet_address == "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"

    def test_whitelist_payees_normalised_to_lowercase(self) -> None:
        from agentpay.policy.models import PolicyConfig

        cfg = PolicyConfig(
            **_make_policy_dict(
                whitelist_payees=["0x70997970C51812dc3A010C7d01b50e0d17dc79C8"],
            )
        )
        assert "0x70997970c51812dc3a010c7d01b50e0d17dc79c8" in cfg.whitelist_payees

    def test_whitelist_assets_normalised_to_lowercase(self) -> None:
        from agentpay.policy.models import PolicyConfig

        cfg = PolicyConfig(
            **_make_policy_dict(
                whitelist_assets=["0x036CBD53842C5426634E7929541EC2318F3DCF7E"],
            )
        )
        assert "0x036cbd53842c5426634e7929541ec2318f3dcf7e" in cfg.whitelist_assets

    # --- Whitelists are frozenset ---

    def test_whitelists_are_frozenset(self) -> None:
        from agentpay.policy.models import PolicyConfig

        cfg = PolicyConfig(**_make_policy_dict())
        assert isinstance(cfg.whitelist_payees, frozenset)
        assert isinstance(cfg.whitelist_assets, frozenset)
        assert isinstance(cfg.whitelist_networks, frozenset)
        assert isinstance(cfg.whitelist_domains, frozenset)

    # --- Amount must be int, not float ---

    def test_max_per_tx_rejects_float(self) -> None:
        from agentpay.policy.models import PolicyConfig

        with pytest.raises((ValueError, TypeError)):
            PolicyConfig(**_make_policy_dict(max_per_tx_base_units=1000.5))

    def test_rolling_budget_rejects_float(self) -> None:
        from agentpay.policy.models import PolicyConfig

        with pytest.raises((ValueError, TypeError)):
            PolicyConfig(**_make_policy_dict(rolling_budget_24h_base_units=5000.0))

    # --- Amount must be > 0 ---

    def test_max_per_tx_rejects_zero(self) -> None:
        from agentpay.policy.models import PolicyConfig

        with pytest.raises(ValueError):
            PolicyConfig(**_make_policy_dict(max_per_tx_base_units=0))

    def test_max_per_tx_rejects_negative(self) -> None:
        from agentpay.policy.models import PolicyConfig

        with pytest.raises(ValueError):
            PolicyConfig(**_make_policy_dict(max_per_tx_base_units=-1))

    def test_rolling_budget_rejects_zero(self) -> None:
        from agentpay.policy.models import PolicyConfig

        with pytest.raises(ValueError):
            PolicyConfig(**_make_policy_dict(rolling_budget_24h_base_units=0))

    def test_lifetime_budget_rejects_zero_when_set(self) -> None:
        from agentpay.policy.models import PolicyConfig

        with pytest.raises(ValueError):
            PolicyConfig(**_make_policy_dict(lifetime_budget_base_units=0))

    def test_lifetime_budget_accepts_none(self) -> None:
        from agentpay.policy.models import PolicyConfig

        cfg = PolicyConfig(**_make_policy_dict(lifetime_budget_base_units=None))
        assert cfg.lifetime_budget_base_units is None

    def test_lifetime_budget_accepts_positive(self) -> None:
        from agentpay.policy.models import PolicyConfig

        cfg = PolicyConfig(**_make_policy_dict(lifetime_budget_base_units=50_000_000))
        assert cfg.lifetime_budget_base_units == 50_000_000

    # --- CAIP-2 network validation ---

    def test_valid_caip2_network(self) -> None:
        from agentpay.policy.models import PolicyConfig

        cfg = PolicyConfig(
            **_make_policy_dict(
                whitelist_networks=["eip155:84532"],
            )
        )
        assert "eip155:84532" in cfg.whitelist_networks

    def test_invalid_caip2_network_rejected(self) -> None:
        from agentpay.policy.models import PolicyConfig

        with pytest.raises(ValueError, match="CAIP-2"):
            PolicyConfig(**_make_policy_dict(whitelist_networks=["not-a-caip"]))

    def test_multiple_caip2_networks(self) -> None:
        from agentpay.policy.models import PolicyConfig

        cfg = PolicyConfig(
            **_make_policy_dict(
                whitelist_networks=["eip155:84532", "eip155:8453"],
            )
        )
        assert cfg.whitelist_networks == frozenset({"eip155:84532", "eip155:8453"})

    # --- Ethereum address format validation ---

    def test_invalid_eth_address_in_wallet(self) -> None:
        from agentpay.policy.models import PolicyConfig

        with pytest.raises(ValueError, match="[Aa]ddress"):
            PolicyConfig(**_make_policy_dict(wallet_address="not-an-address"))

    def test_invalid_eth_address_in_payees(self) -> None:
        from agentpay.policy.models import PolicyConfig

        with pytest.raises(ValueError, match="[Aa]ddress"):
            PolicyConfig(**_make_policy_dict(whitelist_payees=["0xinvalid"]))

    def test_invalid_eth_address_in_assets(self) -> None:
        from agentpay.policy.models import PolicyConfig

        with pytest.raises(ValueError, match="[Aa]ddress"):
            PolicyConfig(**_make_policy_dict(whitelist_assets=["bad"]))

    # --- Immutability ---

    def test_config_is_frozen(self) -> None:
        from pydantic import ValidationError

        from agentpay.policy.models import PolicyConfig

        cfg = PolicyConfig(**_make_policy_dict())
        with pytest.raises(ValidationError):
            cfg.max_per_tx_base_units = 999  # type: ignore[misc]

    # --- Empty whitelists ---

    def test_empty_whitelist_payees_accepted(self) -> None:
        from agentpay.policy.models import PolicyConfig

        cfg = PolicyConfig(**_make_policy_dict(whitelist_payees=[]))
        assert cfg.whitelist_payees == frozenset()

    def test_empty_whitelist_domains_accepted(self) -> None:
        from agentpay.policy.models import PolicyConfig

        cfg = PolicyConfig(**_make_policy_dict(whitelist_domains=[]))
        assert cfg.whitelist_domains == frozenset()


# ---------------------------------------------------------------------------
# 4. PaymentRequest value object
# ---------------------------------------------------------------------------


class TestPaymentRequest:
    """Validate PaymentRequest model constraints."""

    def _make_request(self, **overrides: object) -> dict:
        base: dict = {
            "intent_id": "intent_01",
            "amount": 100_000,
            "payee": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
            "asset": "0x036CbD53842c5426634e7929541ec2318f3dCF7e",
            "network": "eip155:84532",
        }
        base.update(overrides)
        return base

    def test_valid_request(self) -> None:
        from agentpay.policy.models import PaymentRequest

        req = PaymentRequest(**self._make_request())
        assert req.intent_id == "intent_01"
        assert req.amount == 100_000
        assert req.domain is None
        assert req.ttl_seconds == 360  # default

    def test_amount_rejects_zero(self) -> None:
        from agentpay.policy.models import PaymentRequest

        with pytest.raises(ValueError):
            PaymentRequest(**self._make_request(amount=0))

    def test_amount_rejects_negative(self) -> None:
        from agentpay.policy.models import PaymentRequest

        with pytest.raises(ValueError):
            PaymentRequest(**self._make_request(amount=-50))

    def test_amount_rejects_float(self) -> None:
        from agentpay.policy.models import PaymentRequest

        with pytest.raises((ValueError, TypeError)):
            PaymentRequest(**self._make_request(amount=100.5))

    def test_payee_normalised_to_lowercase(self) -> None:
        from agentpay.policy.models import PaymentRequest

        req = PaymentRequest(
            **self._make_request(
                payee="0x70997970C51812DC3A010C7D01B50E0D17DC79C8",
            )
        )
        assert req.payee == "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"

    def test_asset_normalised_to_lowercase(self) -> None:
        from agentpay.policy.models import PaymentRequest

        req = PaymentRequest(
            **self._make_request(
                asset="0x036CBD53842C5426634E7929541EC2318F3DCF7E",
            )
        )
        assert req.asset == "0x036cbd53842c5426634e7929541ec2318f3dcf7e"

    def test_network_validated_as_caip2(self) -> None:
        from agentpay.policy.models import PaymentRequest

        with pytest.raises(ValueError, match="CAIP-2"):
            PaymentRequest(**self._make_request(network="ethereum-mainnet"))

    def test_domain_optional(self) -> None:
        from agentpay.policy.models import PaymentRequest

        req = PaymentRequest(**self._make_request(domain="api.trusted-provider.com"))
        assert req.domain == "api.trusted-provider.com"

    def test_custom_ttl(self) -> None:
        from agentpay.policy.models import PaymentRequest

        req = PaymentRequest(**self._make_request(ttl_seconds=120))
        assert req.ttl_seconds == 120

    def test_request_is_frozen(self) -> None:
        from pydantic import ValidationError

        from agentpay.policy.models import PaymentRequest

        req = PaymentRequest(**self._make_request())
        with pytest.raises(ValidationError):
            req.amount = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 5. ReservationToken & PolicyDecision
# ---------------------------------------------------------------------------


class TestReservationToken:
    def test_creation(self) -> None:
        from agentpay.policy.models import ReservationToken

        tok = ReservationToken(
            reservation_id="res_001",
            intent_id="intent_01",
            policy_id="pol_test_01",
            amount=100_000,
            expires_at=1729000360,
            created_at=1729000000,
        )
        assert tok.reservation_id == "res_001"
        assert tok.amount == 100_000

    def test_token_is_frozen(self) -> None:
        from pydantic import ValidationError

        from agentpay.policy.models import ReservationToken

        tok = ReservationToken(
            reservation_id="res_001",
            intent_id="intent_01",
            policy_id="pol_test_01",
            amount=100_000,
            expires_at=1729000360,
            created_at=1729000000,
        )
        with pytest.raises(ValidationError):
            tok.amount = 0  # type: ignore[misc]


class TestPolicyDecision:
    def test_approved_decision(self) -> None:
        from agentpay.policy.models import PolicyDecision, ReservationToken

        tok = ReservationToken(
            reservation_id="res_001",
            intent_id="intent_01",
            policy_id="pol_test_01",
            amount=100_000,
            expires_at=1729000360,
            created_at=1729000000,
        )
        decision = PolicyDecision(
            approved=True,
            reservation=tok,
            available_after=9_900_000,
        )
        assert decision.approved is True
        assert decision.error_code is None
        assert decision.reservation is tok

    def test_rejected_decision(self) -> None:
        from agentpay.policy.errors import PolicyErrorCode
        from agentpay.policy.models import PolicyDecision

        decision = PolicyDecision(
            approved=False,
            error_code=PolicyErrorCode.SINGLE_TX_CAP_EXCEEDED,
        )
        assert decision.approved is False
        assert decision.error_code is PolicyErrorCode.SINGLE_TX_CAP_EXCEEDED
        assert decision.reservation is None


# ---------------------------------------------------------------------------
# 6. Config loading
# ---------------------------------------------------------------------------


class TestConfigLoading:
    """Verify dict → PolicyConfig loading via load_policy_config."""

    def test_load_from_dict(self) -> None:
        from agentpay.policy.config import load_policy_config

        cfg = load_policy_config(_make_policy_dict())
        assert cfg.id == "pol_test_01"
        assert cfg.wallet_address == "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"

    def test_load_from_test_vector_format(self) -> None:
        """Load from the JSON test vector 'policy' object format."""
        from agentpay.policy.config import load_policy_config

        vector_data = {
            "policyId": "pol_vector_test",
            "name": "Conformance Test Policy",
            "walletAddress": "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
            "max_per_tx_base_units": "1000000",
            "rolling_budget_24h_base_units": "10000000",
            "lifetime_budget_base_units": "50000000",
            "whitelist": {
                "payees": [
                    "0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                    "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc",
                ],
                "assets": ["0x036cbd53842c5426634e7929541ec2318f3dcf7e"],
                "networks": ["eip155:84532"],
                "domains": ["api.trusted-provider.com"],
            },
        }
        cfg = load_policy_config(vector_data)
        assert cfg.id == "pol_vector_test"
        assert cfg.max_per_tx_base_units == 1_000_000
        assert cfg.rolling_budget_24h_base_units == 10_000_000
        assert cfg.lifetime_budget_base_units == 50_000_000
        assert len(cfg.whitelist_payees) == 2

    def test_load_rejects_invalid_data(self) -> None:
        from agentpay.policy.config import load_policy_config

        with pytest.raises((ValueError, Exception)):
            load_policy_config({"id": "bad"})  # Missing required fields


# ---------------------------------------------------------------------------
# 7. IPolicyEngine interface
# ---------------------------------------------------------------------------


class TestIPolicyEngineInterface:
    """Verify the abstract interface defines the expected methods."""

    def test_interface_has_reserve_commit_release(self) -> None:
        from agentpay.policy.interfaces import IPolicyEngine

        assert hasattr(IPolicyEngine, "reserve")
        assert hasattr(IPolicyEngine, "commit")
        assert hasattr(IPolicyEngine, "release")

    def test_interface_cannot_be_instantiated(self) -> None:
        from agentpay.policy.interfaces import IPolicyEngine

        with pytest.raises(TypeError):
            IPolicyEngine()  # type: ignore[abstract]

    def test_subclass_must_implement_all_methods(self) -> None:
        from agentpay.policy.interfaces import IPolicyEngine

        class IncompleteEngine(IPolicyEngine):
            pass

        with pytest.raises(TypeError):
            IncompleteEngine()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# 8. Public API re-exports from agentpay.policy
# ---------------------------------------------------------------------------


class TestPublicAPI:
    def test_policy_package_exports(self) -> None:
        from agentpay.policy import (
            BreakerState,
            IPolicyEngine,
            LedgerEntryState,
            PaymentRequest,
            PolicyConfig,
            PolicyDecision,
            PolicyErrorCode,
            PolicyViolationError,
            ReservationToken,
            load_policy_config,
        )

        # All imports resolve without error
        assert PolicyConfig is not None
        assert PaymentRequest is not None
        assert ReservationToken is not None
        assert PolicyDecision is not None
        assert PolicyErrorCode is not None
        assert PolicyViolationError is not None
        assert BreakerState is not None
        assert LedgerEntryState is not None
        assert IPolicyEngine is not None
        assert load_policy_config is not None
