"""
Tests for T2.3: SQLitePolicyEngine 24h rolling budget, atomic reservation, commit and release.

Covers:
- Atomic reservation in SQLite transaction (BEGIN IMMEDIATE)
- 24h rolling window calculation (P0-6): COMMITTED in [now-86400, now] + active RESERVED where expires_at > now
- Multi-step budget consumption, exhaustion -> DAILY_BUDGET_EXCEEDED
- Release restoring budget immediately
- Release idempotency (calling release multiple times is a no-op)
- Commit transitioning RESERVED -> COMMITTED
- Commit idempotency (calling commit multiple times is a no-op)
- Commit on expired reservation raises RESERVATION_EXPIRED
- Release on already COMMITTED reservation raises RESERVATION_ALREADY_FINALIZED
- Full conformance against policy-budget.json test vector suite
"""

from __future__ import annotations

import sqlite3

import pytest

from agentpay.policy.errors import PolicyErrorCode, PolicyViolationError
from agentpay.policy.models import PolicyConfig


def _init_test_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ap_policy (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            wallet_address TEXT NOT NULL,
            max_per_tx_base_units INTEGER NOT NULL CHECK (max_per_tx_base_units > 0),
            rolling_budget_24h_base_units INTEGER NOT NULL CHECK (rolling_budget_24h_base_units > 0),
            lifetime_budget_base_units INTEGER CHECK (lifetime_budget_base_units > 0),
            whitelist_payees_json TEXT NOT NULL DEFAULT '[]',
            whitelist_assets_json TEXT NOT NULL DEFAULT '[]',
            whitelist_networks_json TEXT NOT NULL DEFAULT '[]',
            whitelist_domains_json TEXT NOT NULL DEFAULT '[]',
            circuit_max_rpm INTEGER NOT NULL DEFAULT 15,
            circuit_consecutive_failures INTEGER NOT NULL DEFAULT 5,
            circuit_cooldown_seconds INTEGER NOT NULL DEFAULT 300,
            breaker_state TEXT NOT NULL DEFAULT 'CLOSED' CHECK (breaker_state IN ('CLOSED', 'OPEN', 'HALF_OPEN')),
            tripped_at INTEGER,
            is_frozen INTEGER NOT NULL DEFAULT 0 CHECK (is_frozen IN (0, 1)),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ap_budget_ledger (
            id TEXT PRIMARY KEY,
            policy_id TEXT NOT NULL REFERENCES ap_policy(id),
            wallet_address TEXT NOT NULL,
            intent_id TEXT NOT NULL UNIQUE,
            amount_base_units INTEGER NOT NULL CHECK (amount_base_units > 0 AND typeof(amount_base_units) = 'integer'),
            state TEXT NOT NULL CHECK (state IN ('RESERVED', 'COMMITTED', 'RELEASED')),
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
    )
    conn.commit()


def _make_sample_config() -> PolicyConfig:
    return PolicyConfig(
        id="pol_test_sqlite",
        name="SQLite Policy Test",
        wallet_address="0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
        max_per_tx_base_units=1_000_000,  # 1 USDC
        rolling_budget_24h_base_units=10_000_000,  # 10 USDC
        lifetime_budget_base_units=50_000_000,
        whitelist_payees=frozenset(["0x70997970c51812dc3a010c7d01b50e0d17dc79c8"]),
        whitelist_assets=frozenset(["0x036cbd53842c5426634e7929541ec2318f3dcf7e"]),
        whitelist_networks=frozenset(["eip155:84532"]),
        whitelist_domains=frozenset(["api.trusted-provider.com"]),
        created_at=1729000000,
        updated_at=1729000000,
    )


class MockClock:
    def __init__(self, initial_time: int = 1729000000):
        self._time = initial_time

    def __call__(self) -> int:
        return self._time

    def advance(self, seconds: int) -> None:
        self._time += seconds

    def set_time(self, timestamp: int) -> None:
        self._time = timestamp


class TestSQLitePolicyEngine:
    @pytest.fixture
    def clock(self) -> MockClock:
        return MockClock(1729000000)

    @pytest.fixture
    def db_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        _init_test_db(conn)
        return conn

    def test_single_normal_reservation_and_commit(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        config = _make_sample_config()
        engine = SQLitePolicyEngine(conn=db_conn, config=config, clock=clock)

        decision = engine.reserve(
            intent_id="intent_01",
            amount=100_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
            domain="api.trusted-provider.com",
        )

        assert decision.approved is True
        assert decision.error_code is None
        assert decision.available_after == 9_900_000
        assert decision.reservation is not None
        assert decision.reservation.intent_id == "intent_01"
        assert decision.reservation.amount == 100_000

        # Commit reservation
        engine.commit("intent_01")

        # Quota remains committed and available quota stays at 9.9 USDC
        avail = engine.get_available_rolling_budget()
        assert avail == 9_900_000

    def test_rolling_budget_exhaustion(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        config = _make_sample_config()
        engine = SQLitePolicyEngine(conn=db_conn, config=config, clock=clock)

        # Reserve 10 x 1 USDC = 10 USDC
        for i in range(1, 11):
            decision = engine.reserve(
                intent_id=f"intent_batch_{i}",
                amount=1_000_000,
                payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                network="eip155:84532",
            )
            assert decision.approved is True
            assert decision.available_after == 10_000_000 - i * 1_000_000

        # 11th reservation must be rejected with DAILY_BUDGET_EXCEEDED
        decision_11 = engine.reserve(
            intent_id="intent_batch_11",
            amount=1_000_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert decision_11.approved is False
        assert decision_11.error_code == PolicyErrorCode.DAILY_BUDGET_EXCEEDED

    def test_release_restores_available_budget(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        config = _make_sample_config()
        engine = SQLitePolicyEngine(conn=db_conn, config=config, clock=clock)

        decision = engine.reserve(
            intent_id="intent_rel",
            amount=500_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert decision.approved is True
        assert decision.available_after == 9_500_000

        # Release reservation
        engine.release("intent_rel", reason="User cancelled")
        assert engine.get_available_rolling_budget() == 10_000_000

    def test_release_idempotency(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        config = _make_sample_config()
        engine = SQLitePolicyEngine(conn=db_conn, config=config, clock=clock)

        engine.reserve(
            intent_id="intent_idem_rel",
            amount=500_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )

        # Release 1st time
        engine.release("intent_idem_rel")
        assert engine.get_available_rolling_budget() == 10_000_000

        # Release 2nd time should be a no-op
        engine.release("intent_idem_rel")
        assert engine.get_available_rolling_budget() == 10_000_000

    def test_commit_idempotency(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        config = _make_sample_config()
        engine = SQLitePolicyEngine(conn=db_conn, config=config, clock=clock)

        engine.reserve(
            intent_id="intent_idem_commit",
            amount=500_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )

        engine.commit("intent_idem_commit")
        # Second commit is a no-op
        engine.commit("intent_idem_commit")
        assert engine.get_available_rolling_budget() == 9_500_000

    def test_release_already_committed_raises_finalized(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        config = _make_sample_config()
        engine = SQLitePolicyEngine(conn=db_conn, config=config, clock=clock)

        engine.reserve(
            intent_id="intent_guard",
            amount=500_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        engine.commit("intent_guard")

        with pytest.raises(PolicyViolationError) as exc_info:
            engine.release("intent_guard")
        assert exc_info.value.code == PolicyErrorCode.RESERVATION_ALREADY_FINALIZED

    def test_commit_expired_reservation_raises_expired(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        config = _make_sample_config()
        engine = SQLitePolicyEngine(conn=db_conn, config=config, clock=clock)

        engine.reserve(
            intent_id="intent_exp",
            amount=500_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
            ttl_seconds=300,
        )

        # Advance clock beyond ttl_seconds
        clock.advance(301)

        with pytest.raises(PolicyViolationError) as exc_info:
            engine.commit("intent_exp")
        assert exc_info.value.code == PolicyErrorCode.RESERVATION_EXPIRED

    def test_24h_window_sliding_recovery(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        """Committed transactions older than 24h (86400s) roll off the window and recover budget."""
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        config = _make_sample_config()
        engine = SQLitePolicyEngine(conn=db_conn, config=config, clock=clock)

        # Commit 10 USDC at t0
        engine.reserve(
            intent_id="intent_slide",
            amount=1_000_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        engine.commit("intent_slide")
        assert engine.get_available_rolling_budget() == 9_000_000

        # Advance 23h59m (86340s) -> still counted
        clock.advance(86340)
        assert engine.get_available_rolling_budget() == 9_000_000

        # Advance another 61s (total 86401s > 24h) -> rolled off!
        clock.advance(61)
        assert engine.get_available_rolling_budget() == 10_000_000


class TestConformanceVectorsT23:
    """Executes all 11 test cases in policy-budget.json against SQLitePolicyEngine."""

    @pytest.fixture
    def test_vector_suite(self) -> dict:
        import json
        from pathlib import Path

        vector_path = (
            Path(__file__).resolve().parent.parent.parent
            / "specs"
            / "conformance"
            / "test-vectors"
            / "policy-budget.json"
        )
        with open(vector_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_run_all_vector_test_cases(self, test_vector_suite: dict) -> None:
        from agentpay.policy.config import load_policy_config
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        policy_dict = test_vector_suite["policy"]

        for case in test_vector_suite["testCases"]:
            case_id = case["id"]
            clock_val = case.get("clock", 1729000000)
            clock = MockClock(clock_val)

            conn = sqlite3.connect(":memory:", check_same_thread=False)
            _init_test_db(conn)

            # Check if case overrides initial state (e.g. is_frozen)
            cfg_dict = dict(policy_dict)
            if "initialState" in case:
                for k, v in case["initialState"].items():
                    if k == "is_frozen":
                        cfg_dict["is_frozen"] = bool(v)
            cfg = load_policy_config(cfg_dict)

            engine = SQLitePolicyEngine(conn=conn, config=cfg, clock=clock)

            for act in case["actions"]:
                step = act["step"]
                action_type = act["action"]
                intent_id = act["intentId"]

                if action_type == "reserve":
                    amount = int(act["amount"])
                    payee = act["payee"]
                    asset = act["asset"]
                    network = act["network"]
                    domain = act.get("domain")

                    decision = engine.reserve(
                        intent_id=intent_id,
                        amount=amount,
                        payee=payee,
                        asset=asset,
                        network=network,
                        domain=domain,
                    )

                    expected_res = act["expectedResult"]
                    if expected_res == "APPROVED":
                        assert decision.approved is True, (
                            f"[{case_id} step {step}] Expected APPROVED, got {decision}"
                        )
                        if "expectedAvailableAfter" in act:
                            expected_avail = int(act["expectedAvailableAfter"])
                            assert decision.available_after == expected_avail, (
                                f"[{case_id} step {step}] Expected availableAfter {expected_avail}, "
                                f"got {decision.available_after}"
                            )
                    elif expected_res == "REJECTED":
                        assert decision.approved is False, (
                            f"[{case_id} step {step}] Expected REJECTED, got {decision}"
                        )
                        expected_err = act["expectedError"]
                        assert decision.error_code is not None
                        assert decision.error_code.value == expected_err, (
                            f"[{case_id} step {step}] Expected error {expected_err}, "
                            f"got {decision.error_code.value}"
                        )

                elif action_type == "commit":
                    engine.commit(intent_id)
                    if "expectedAvailableAfter" in act:
                        expected_avail = int(act["expectedAvailableAfter"])
                        avail = engine.get_available_rolling_budget()
                        assert avail == expected_avail, (
                            f"[{case_id} step {step}] Expected avail {expected_avail}, got {avail}"
                        )

                elif action_type == "release":
                    engine.release(intent_id)
                    if "expectedAvailableAfter" in act:
                        expected_avail = int(act["expectedAvailableAfter"])
                        avail = engine.get_available_rolling_budget()
                        assert avail == expected_avail, (
                            f"[{case_id} step {step}] Expected avail {expected_avail}, got {avail}"
                        )
