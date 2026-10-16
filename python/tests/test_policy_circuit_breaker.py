"""
Tests for T2.4: Circuit breaker (rate frequency, consecutive failures, half-open probe, cooldown, manual unfreeze).

Covers:
- RPM rate limit trigger: requests in last 60s > circuit_max_rpm -> trips to OPEN, CIRCUIT_BREAKER_OPEN
- Consecutive failures trigger: N consecutive failure notifications -> trips to OPEN, CIRCUIT_BREAKER_OPEN
- In OPEN state: immediately aborts with CIRCUIT_BREAKER_OPEN
- Cooldown expiration: now >= tripped_at + circuit_cooldown_seconds -> transitions to HALF_OPEN
- In HALF_OPEN state: allows exactly 1 probe reservation (in-flight)
- Concurrent probe block: while a probe is pending in HALF_OPEN, second reservation is rejected
- Probe outcome:
  - Probe success (commit) -> resets circuit breaker to CLOSED, consecutive failures = 0
  - Probe failure (release / failure report) -> trips back to OPEN, resets tripped_at = now
- Administrative manual unfreeze / reset -> resets to CLOSED
- Precedence: Freeze check precedes Breaker check; Breaker precedes Whitelists and Single-Tx Cap
"""

from __future__ import annotations

import sqlite3

import pytest

from agentpay.policy.errors import PolicyErrorCode
from agentpay.policy.models import BreakerState, PolicyConfig


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
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
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


class MockClock:
    def __init__(self, initial_time: int = 1729000000):
        self._time = initial_time

    def __call__(self) -> int:
        return self._time

    def advance(self, seconds: int) -> None:
        self._time += seconds

    def set_time(self, timestamp: int) -> None:
        self._time = timestamp


def _make_breaker_config(**overrides: object) -> PolicyConfig:
    base: dict = {
        "id": "pol_breaker_test",
        "name": "Circuit Breaker Test Policy",
        "wallet_address": "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
        "max_per_tx_base_units": 1_000_000,
        "rolling_budget_24h_base_units": 100_000_000,
        "circuit_max_rpm": 5,  # Low RPM threshold for testing
        "circuit_consecutive_failures": 3,  # Low failure threshold
        "circuit_cooldown_seconds": 60,  # 60s cooldown
        "whitelist_payees": ["0x70997970c51812dc3a010c7d01b50e0d17dc79c8"],
        "whitelist_assets": ["0x036cbd53842c5426634e7929541ec2318f3dcf7e"],
        "whitelist_networks": ["eip155:84532"],
        "whitelist_domains": ["api.trusted-provider.com"],
        "is_frozen": False,
    }
    base.update(overrides)
    return PolicyConfig(**base)


class TestCircuitBreaker:
    @pytest.fixture
    def clock(self) -> MockClock:
        return MockClock(1729000000)

    @pytest.fixture
    def db_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        _init_test_db(conn)
        return conn

    def test_rpm_threshold_trips_breaker(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        """Exceeding circuit_max_rpm (5) in last 60s trips state to OPEN."""
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        cfg = _make_breaker_config(circuit_max_rpm=5)
        engine = SQLitePolicyEngine(conn=db_conn, config=cfg, clock=clock)

        # Execute 5 requests within 60s
        for i in range(1, 6):
            decision = engine.reserve(
                intent_id=f"intent_rpm_{i}",
                amount=10_000,
                payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                network="eip155:84532",
            )
            assert decision.approved is True

        # 6th request within the same 60s window trips the breaker
        decision_6 = engine.reserve(
            intent_id="intent_rpm_6",
            amount=10_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert decision_6.approved is False
        assert decision_6.error_code == PolicyErrorCode.CIRCUIT_BREAKER_OPEN
        assert engine.get_breaker_state() == BreakerState.OPEN

    def test_consecutive_failures_trip_breaker(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        """Reporting N consecutive failures trips the circuit breaker to OPEN."""
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        cfg = _make_breaker_config(circuit_consecutive_failures=3)
        engine = SQLitePolicyEngine(conn=db_conn, config=cfg, clock=clock)

        assert engine.get_breaker_state() == BreakerState.CLOSED

        # Record 2 failures
        engine.record_failure("upstream error 1")
        engine.record_failure("upstream error 2")
        assert engine.get_breaker_state() == BreakerState.CLOSED

        # 3rd failure trips the breaker
        engine.record_failure("upstream error 3")
        assert engine.get_breaker_state() == BreakerState.OPEN

        # Next reservation rejected
        decision = engine.reserve(
            intent_id="intent_after_fail",
            amount=10_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert decision.approved is False
        assert decision.error_code == PolicyErrorCode.CIRCUIT_BREAKER_OPEN

    def test_success_resets_consecutive_failures(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        """A successful commit resets the consecutive failure counter to 0."""
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        cfg = _make_breaker_config(circuit_consecutive_failures=3)
        engine = SQLitePolicyEngine(conn=db_conn, config=cfg, clock=clock)

        engine.record_failure("failure 1")
        engine.record_failure("failure 2")

        # Now a successful transaction commits
        engine.reserve(
            intent_id="intent_reset_fails",
            amount=10_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        engine.commit("intent_reset_fails")

        # Now record another failure: consecutive failures should be 1, not 3
        engine.record_failure("failure after success")
        assert engine.get_breaker_state() == BreakerState.CLOSED

    def test_cooldown_transitions_open_to_half_open(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        """After cooldown_seconds elapsed, OPEN transitions to HALF_OPEN."""
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        cfg = _make_breaker_config(circuit_cooldown_seconds=60)
        engine = SQLitePolicyEngine(conn=db_conn, config=cfg, clock=clock)

        # Trip to OPEN
        engine.record_failure("f1")
        engine.record_failure("f2")
        engine.record_failure("f3")
        assert engine.get_breaker_state() == BreakerState.OPEN

        # Advance 59s (< 60s cooldown) -> still OPEN
        clock.advance(59)
        assert engine.get_breaker_state() == BreakerState.OPEN

        # Advance 2s (total 61s >= 60s cooldown) -> transitions to HALF_OPEN
        clock.advance(2)
        assert engine.get_breaker_state() == BreakerState.HALF_OPEN

    def test_half_open_allows_single_probe_and_blocks_concurrent(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        """In HALF_OPEN, allows exactly one probe reservation; rejects second reservation."""
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        cfg = _make_breaker_config(circuit_cooldown_seconds=60)
        engine = SQLitePolicyEngine(conn=db_conn, config=cfg, clock=clock)

        # Trip to OPEN and wait for cooldown
        engine.record_failure("f1")
        engine.record_failure("f2")
        engine.record_failure("f3")
        clock.advance(65)
        assert engine.get_breaker_state() == BreakerState.HALF_OPEN

        # First request in HALF_OPEN is the test probe -> approved!
        decision_probe = engine.reserve(
            intent_id="intent_probe_1",
            amount=10_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert decision_probe.approved is True

        # Second request while probe is in flight -> rejected!
        decision_concurrent = engine.reserve(
            intent_id="intent_probe_2",
            amount=10_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert decision_concurrent.approved is False
        assert decision_concurrent.error_code == PolicyErrorCode.CIRCUIT_BREAKER_OPEN

    def test_probe_success_closes_circuit_breaker(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        """When test probe succeeds (commit), breaker transitions to CLOSED."""
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        cfg = _make_breaker_config(circuit_cooldown_seconds=60)
        engine = SQLitePolicyEngine(conn=db_conn, config=cfg, clock=clock)

        engine.record_failure("f1")
        engine.record_failure("f2")
        engine.record_failure("f3")
        clock.advance(65)

        engine.reserve(
            intent_id="intent_probe_succ",
            amount=10_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        engine.commit("intent_probe_succ")

        # Breaker should now be CLOSED
        assert engine.get_breaker_state() == BreakerState.CLOSED

    def test_probe_failure_re_trips_to_open(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        """When test probe fails (record_failure), breaker resets to OPEN with new tripped_at."""
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        cfg = _make_breaker_config(circuit_cooldown_seconds=60)
        engine = SQLitePolicyEngine(conn=db_conn, config=cfg, clock=clock)

        engine.record_failure("f1")
        engine.record_failure("f2")
        engine.record_failure("f3")
        clock.advance(65)
        assert engine.get_breaker_state() == BreakerState.HALF_OPEN

        engine.reserve(
            intent_id="intent_probe_fail",
            amount=10_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        # Probe fails
        engine.record_failure("probe request failed")
        assert engine.get_breaker_state() == BreakerState.OPEN

    def test_manual_unfreeze_and_reset(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        """Explicit unfreeze / reset resets breaker to CLOSED."""
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        cfg = _make_breaker_config()
        engine = SQLitePolicyEngine(conn=db_conn, config=cfg, clock=clock)

        engine.record_failure("f1")
        engine.record_failure("f2")
        engine.record_failure("f3")
        assert engine.get_breaker_state() == BreakerState.OPEN

        # Operator unfreezes / resets
        engine.reset_breaker()
        assert engine.get_breaker_state() == BreakerState.CLOSED

    def test_freeze_precedes_circuit_breaker(
        self, db_conn: sqlite3.Connection, clock: MockClock
    ) -> None:
        """is_frozen == 1 takes precedence over CIRCUIT_BREAKER_OPEN."""
        from agentpay.policy.sqlite_engine import SQLitePolicyEngine

        cfg = _make_breaker_config(is_frozen=True)
        engine = SQLitePolicyEngine(conn=db_conn, config=cfg, clock=clock)

        # Even if breaker is tripped
        engine.record_failure("f1")
        engine.record_failure("f2")
        engine.record_failure("f3")

        decision = engine.reserve(
            intent_id="intent_freeze_prio",
            amount=10_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert decision.approved is False
        assert decision.error_code == PolicyErrorCode.POLICY_FROZEN
