"""
Tests for P1 items:
- P1-1: Safe transaction rollbacks without double-rollback OperationalError
- P1-2: Authoritative DB is_frozen cannot be overwritten by stale in-memory config
- P1-3: Consecutive failures tracking and recovery
- P1-4 & P1-7: Model rejects bool, rejects integers > 2^63 - 1, validates ttl_seconds range
- P1-5: Domain normalization handles full URLs, paths, ports, and trailing dots
- P1-6: create_buyer_connection enables PRAGMA foreign_keys = ON and WAL mode
"""

from __future__ import annotations

import tempfile

import pytest

from agentpay.policy.db import create_buyer_connection, init_buyer_database
from agentpay.policy.errors import PolicyErrorCode
from agentpay.policy.models import (
    SQLITE_MAX_INT,
    PaymentRequest,
    PolicyConfig,
)
from agentpay.policy.sqlite_engine import SQLitePolicyEngine


def _make_p1_config(**overrides: object) -> PolicyConfig:
    base: dict = {
        "id": "pol_p1_test",
        "name": "P1 Test Policy",
        "wallet_address": "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
        "max_per_tx_base_units": 1_000_000,
        "rolling_budget_24h_base_units": 10_000_000,
        "whitelist_payees": ["0x70997970c51812dc3a010c7d01b50e0d17dc79c8"],
        "whitelist_assets": ["0x036cbd53842c5426634e7929541ec2318f3dcf7e"],
        "whitelist_networks": ["eip155:84532"],
        "whitelist_domains": ["api.trusted-provider.com"],
        "is_frozen": False,
    }
    base.update(overrides)
    return PolicyConfig(**base)


class TestP1ReliabilityAndConsistency:
    def test_p1_4_rejects_bool_for_amount(self) -> None:
        """P1-4: Reject bool (True/False) for amount and limits."""
        with pytest.raises(TypeError, match="bool"):
            PaymentRequest(
                intent_id="i_bool",
                amount=True,  # type: ignore[arg-type]
                payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                network="eip155:84532",
            )

        with pytest.raises(TypeError, match="bool"):
            _make_p1_config(max_per_tx_base_units=True)

    def test_p1_7_rejects_int_exceeding_sqlite_max(self) -> None:
        """P1-7: Reject amounts exceeding SQLite 64-bit signed maximum."""
        overflow_amount = SQLITE_MAX_INT + 1
        with pytest.raises(ValueError, match="exceeds SQLite maximum"):
            PaymentRequest(
                intent_id="i_overflow",
                amount=overflow_amount,
                payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                network="eip155:84532",
            )

    def test_p1_4_ttl_seconds_validation_range(self) -> None:
        """P1-4: Reject negative TTL or TTL exceeding 24 hours."""
        with pytest.raises(ValueError):
            PaymentRequest(
                intent_id="i_ttl",
                amount=10_000,
                payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                network="eip155:84532",
                ttl_seconds=0,
            )

        with pytest.raises(ValueError, match="cannot exceed 86400"):
            PaymentRequest(
                intent_id="i_ttl2",
                amount=10_000,
                payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                network="eip155:84532",
                ttl_seconds=86401,
            )

    def test_p1_5_domain_normalization(self) -> None:
        """P1-5: Full URL, ports, paths, and trailing dots are normalized to hostname."""
        req1 = PaymentRequest(
            intent_id="i_dom1",
            amount=10_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
            domain="https://api.trusted-provider.com:8443/v1/data?foo=bar",
        )
        assert req1.domain == "api.trusted-provider.com"

        req2 = PaymentRequest(
            intent_id="i_dom2",
            amount=10_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
            domain="API.TRUSTED-PROVIDER.COM.",
        )
        assert req2.domain == "api.trusted-provider.com"

    def test_p1_6_connection_factory_enforces_pragmas(self) -> None:
        """P1-6: create_buyer_connection enables foreign keys and WAL mode."""
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db_path = tmp.name

        conn = create_buyer_connection(db_path)
        cur = conn.cursor()

        # Check foreign keys
        cur.execute("PRAGMA foreign_keys;")
        assert cur.fetchone()[0] == 1

        # Check WAL journal mode
        cur.execute("PRAGMA journal_mode;")
        assert cur.fetchone()[0].upper() == "WAL"

        # Check busy timeout
        cur.execute("PRAGMA busy_timeout;")
        assert cur.fetchone()[0] >= 5000

        conn.close()

    def test_p1_2_authoritative_is_frozen_in_db_preserved_on_init(self) -> None:
        """P1-2: If operator froze policy in DB, restart of engine with is_frozen=False preserves frozen."""
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db_path = tmp.name

        conn1 = create_buyer_connection(db_path)
        init_buyer_database(conn1)

        cfg_unfrozen = _make_p1_config(is_frozen=False)
        _ = SQLitePolicyEngine(conn=conn1, config=cfg_unfrozen)

        # Operator marks policy as frozen directly in DB
        conn1.execute(
            "UPDATE ap_policy SET is_frozen = 1 WHERE id = ?;", (cfg_unfrozen.id,)
        )
        conn1.commit()
        conn1.close()

        # Restart engine with old unfrozen config
        conn2 = create_buyer_connection(db_path)
        engine2 = SQLitePolicyEngine(conn=conn2, config=cfg_unfrozen)

        # reserve() must detect is_frozen from DB authoritative record!
        decision = engine2.reserve(
            intent_id="i_freeze_restart",
            amount=10_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert decision.approved is False
        assert decision.error_code == PolicyErrorCode.POLICY_FROZEN
        conn2.close()

    def test_p1_3_consecutive_failures_persisted_across_instances_and_restart(
        self,
    ) -> None:
        """P1-3: Consecutive failures are persisted to ap_policy and preserved across engine restarts."""
        from agentpay.policy.models import BreakerState

        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db_path = tmp.name

        cfg = _make_p1_config(circuit_consecutive_failures=3)

        # Instance 1: init and record 2 failures
        conn1 = create_buyer_connection(db_path)
        init_buyer_database(conn1)
        engine1 = SQLitePolicyEngine(conn=conn1, config=cfg)
        assert engine1.get_breaker_state() == BreakerState.CLOSED

        engine1.record_failure("err 1")
        engine1.record_failure("err 2")
        assert engine1.get_breaker_state() == BreakerState.CLOSED
        assert engine1._consecutive_failures == 2
        conn1.close()

        # Instance 2 (simulating restart / separate process):
        conn2 = create_buyer_connection(db_path)
        engine2 = SQLitePolicyEngine(conn=conn2, config=cfg)
        assert engine2._consecutive_failures == 2
        assert engine2.get_breaker_state() == BreakerState.CLOSED

        # 3rd failure trips the breaker on instance 2!
        engine2.record_failure("err 3")
        assert engine2.get_breaker_state() == BreakerState.OPEN
        assert engine2._consecutive_failures == 3

        # New reservation is rejected by circuit breaker
        dec = engine2.reserve(
            intent_id="i_trip_across_inst",
            amount=10_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert dec.approved is False
        assert dec.error_code == PolicyErrorCode.CIRCUIT_BREAKER_OPEN
        conn2.close()
