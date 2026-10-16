"""
Tests for T2.2: Single-transaction cap, Whitelist matrix, and Lifetime budget evaluation.
Testing strict short-circuit evaluation order per specs/policy-engine-spec.md §3.
"""

from __future__ import annotations

from agentpay.policy.errors import PolicyErrorCode
from agentpay.policy.models import (
    PaymentRequest,
    PolicyConfig,
)


def _make_policy_config(**overrides: object) -> PolicyConfig:
    base: dict = {
        "id": "pol_test_eval",
        "name": "Evaluation Test Policy",
        "wallet_address": "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
        "max_per_tx_base_units": 1_000_000,  # 1 USDC
        "rolling_budget_24h_base_units": 10_000_000,  # 10 USDC
        "lifetime_budget_base_units": 50_000_000,  # 50 USDC
        "whitelist_payees": ["0x70997970c51812dc3a010c7d01b50e0d17dc79c8"],
        "whitelist_assets": ["0x036cbd53842c5426634e7929541ec2318f3dcf7e"],
        "whitelist_networks": ["eip155:84532"],
        "whitelist_domains": ["api.trusted-provider.com"],
        "is_frozen": False,
    }
    base.update(overrides)
    return PolicyConfig(**base)


def _make_payment_request(**overrides: object) -> PaymentRequest:
    base: dict = {
        "intent_id": "intent_eval_01",
        "amount": 500_000,  # 0.5 USDC
        "payee": "0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
        "asset": "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
        "network": "eip155:84532",
        "domain": "api.trusted-provider.com",
    }
    base.update(overrides)
    return PaymentRequest(**base)


class TestPolicyEvaluatorT22:
    """Verify static rule checks: freeze, whitelists, single-tx cap, lifetime budget."""

    def test_pass_all_static_rules(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config()
        req = _make_payment_request()
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.passed is True
        assert result.error_code is None

    def test_policy_frozen_aborts_immediately(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config(is_frozen=True)
        req = _make_payment_request()
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.passed is False
        assert result.error_code == PolicyErrorCode.POLICY_FROZEN

    def test_network_not_whitelisted(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config()
        req = _make_payment_request(network="eip155:1")
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.passed is False
        assert result.error_code == PolicyErrorCode.NETWORK_NOT_WHITELISTED

    def test_asset_not_whitelisted(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config()
        req = _make_payment_request(asset="0x1111111111111111111111111111111111111111")
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.passed is False
        assert result.error_code == PolicyErrorCode.ASSET_NOT_WHITELISTED

    def test_payee_not_whitelisted(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config()
        req = _make_payment_request(payee="0x90f79bf6eb2c4f870365e785982e1f101e93b906")
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.passed is False
        assert result.error_code == PolicyErrorCode.PAYEE_NOT_WHITELISTED

    def test_domain_not_whitelisted_when_provided(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config()
        req = _make_payment_request(domain="api.untrusted-phishing.com")
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.passed is False
        assert result.error_code == PolicyErrorCode.DOMAIN_NOT_WHITELISTED

    def test_domain_none_skipped_if_whitelist_configured(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config()
        req = _make_payment_request(domain=None)
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.passed is True

    def test_whitelist_empty_means_deny_all_whitelisted_entities(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        # When whitelist_payees is empty, no payee should be allowed
        config = _make_policy_config(whitelist_payees=[])
        req = _make_payment_request()
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.passed is False
        assert result.error_code == PolicyErrorCode.PAYEE_NOT_WHITELISTED

    def test_single_tx_cap_exceeded(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config(max_per_tx_base_units=1_000_000)
        req = _make_payment_request(amount=1_000_001)
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.passed is False
        assert result.error_code == PolicyErrorCode.SINGLE_TX_CAP_EXCEEDED

    def test_single_tx_cap_exact_boundary_passes(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config(max_per_tx_base_units=1_000_000)
        req = _make_payment_request(amount=1_000_000)
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.passed is True

    def test_lifetime_budget_exceeded(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config(lifetime_budget_base_units=50_000_000)
        req = _make_payment_request(amount=500_000)
        result = evaluate_static_rules(config, req, lifetime_spent=49_600_000)
        assert result.passed is False
        assert result.error_code == PolicyErrorCode.LIFETIME_BUDGET_EXCEEDED

    def test_lifetime_budget_exact_boundary_passes(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config(lifetime_budget_base_units=50_000_000)
        req = _make_payment_request(amount=500_000)
        result = evaluate_static_rules(config, req, lifetime_spent=49_500_000)
        assert result.passed is True

    def test_lifetime_budget_none_means_unlimited(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config(lifetime_budget_base_units=None)
        req = _make_payment_request(amount=1_000_000)
        result = evaluate_static_rules(config, req, lifetime_spent=999_999_999_999)
        assert result.passed is True


class TestPrecedenceShortCircuit:
    """Validate strict evaluation order per specs/policy-engine-spec.md §3:
    1. Freeze check
    2. Whitelist: Network -> Asset -> Payee -> Domain
    3. Single-Tx Cap
    4. Lifetime budget
    """

    def test_freeze_precedes_whitelist(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config(is_frozen=True)
        # Also invalid network
        req = _make_payment_request(network="eip155:1")
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.error_code == PolicyErrorCode.POLICY_FROZEN

    def test_network_precedes_asset(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config()
        # Invalid network and invalid asset
        req = _make_payment_request(
            network="eip155:1", asset="0x0000000000000000000000000000000000000000"
        )
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.error_code == PolicyErrorCode.NETWORK_NOT_WHITELISTED

    def test_asset_precedes_payee(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config()
        # Invalid asset and invalid payee
        req = _make_payment_request(
            asset="0x0000000000000000000000000000000000000000",
            payee="0x0000000000000000000000000000000000000000",
        )
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.error_code == PolicyErrorCode.ASSET_NOT_WHITELISTED

    def test_payee_precedes_domain(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config()
        # Invalid payee and invalid domain
        req = _make_payment_request(
            payee="0x0000000000000000000000000000000000000000",
            domain="evil.com",
        )
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.error_code == PolicyErrorCode.PAYEE_NOT_WHITELISTED

    def test_domain_precedes_single_tx_cap(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config()
        # Invalid domain and amount exceeding cap
        req = _make_payment_request(
            domain="evil.com",
            amount=99_000_000,
        )
        result = evaluate_static_rules(config, req, lifetime_spent=0)
        assert result.error_code == PolicyErrorCode.DOMAIN_NOT_WHITELISTED

    def test_single_tx_cap_precedes_lifetime_budget(self) -> None:
        from agentpay.policy.evaluator import evaluate_static_rules

        config = _make_policy_config(
            max_per_tx_base_units=1_000_000,
            lifetime_budget_base_units=5_000_000,
        )
        # Exceeds both single cap and lifetime budget
        req = _make_payment_request(amount=2_000_000)
        result = evaluate_static_rules(config, req, lifetime_spent=4_500_000)
        assert result.error_code == PolicyErrorCode.SINGLE_TX_CAP_EXCEEDED
