"""
Tests for P2 Engineering and Quality Improvements:
- P2-1: Query plan verification for indexed rolling sum and lifetime queries.
- P2-2: Database lock contention, busy_timeout handling, and post-timeout recovery.
- P2-3: True multi-process concurrent reservations under WAL mode.
- P2-4: Lifetime budget concurrent regression test near boundary limit.
- P2-5: Comprehensive state transition assertions and idempotent boundary testing.
- P2-6: Rejection of invalid whitelist configurations (explicit TypeError instead of silent coercion).
- P2-7: RPM 60-second sliding window rate count verification.
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing as mp
import sqlite3
import tempfile

import pytest
from pydantic import ValidationError

from agentpay.policy.db import create_buyer_connection, init_buyer_database
from agentpay.policy.errors import PolicyErrorCode, PolicyViolationError
from agentpay.policy.models import (
    LedgerEntryState,
    PolicyConfig,
)
from agentpay.policy.sqlite_engine import SQLitePolicyEngine


def _make_p2_config(**overrides: object) -> PolicyConfig:
    base: dict = {
        "id": "pol_p2_test",
        "name": "P2 Test Policy",
        "wallet_address": "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
        "max_per_tx_base_units": 1_000_000,
        "rolling_budget_24h_base_units": 10_000_000,
        "lifetime_budget_base_units": None,
        "whitelist_payees": ["0x70997970c51812dc3a010c7d01b50e0d17dc79c8"],
        "whitelist_assets": ["0x036cbd53842c5426634e7929541ec2318f3dcf7e"],
        "whitelist_networks": ["eip155:84532"],
        "whitelist_domains": ["api.trusted-provider.com"],
        "is_frozen": False,
    }
    base.update(overrides)
    return PolicyConfig(**base)


# Multiprocessing worker function must be module-level for pickling
def _mp_worker_reserve(
    db_path: str,
    policy_id: str,
    worker_id: int,
    request_count: int,
    amount_per_request: int,
) -> int:
    """
    Worker process: opens a separate connection to db_path, executes reservations.
    Returns number of successfully approved reservations.
    """
    conn = create_buyer_connection(db_path)
    cfg = _make_p2_config(
        id=policy_id,
        rolling_budget_24h_base_units=100_000,
        max_per_tx_base_units=20_000,
    )
    engine = SQLitePolicyEngine(conn=conn, config=cfg)

    success_count = 0
    for i in range(request_count):
        intent_id = f"mp_intent_w{worker_id}_req{i}"
        dec = engine.reserve(
            intent_id=intent_id,
            amount=amount_per_request,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        if dec.approved:
            success_count += 1
            # Commit some immediately to simulate real workflow
            if i % 2 == 0:
                engine.commit(intent_id)

    conn.close()
    return success_count


class TestP2Engineering:
    def test_p2_1_query_plan_uses_index_not_full_scan(self) -> None:
        """P2-1: Verify EXPLAIN QUERY PLAN uses index on (policy_id, state, created_at, expires_at)."""
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db_path = tmp.name

        conn = create_buyer_connection(db_path)
        init_buyer_database(conn)

        # Explain query plan for rolling budget calculation
        cursor = conn.cursor()
        cursor.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT COALESCE(SUM(amount_base_units), 0)
            FROM ap_budget_ledger
            WHERE policy_id = ?
              AND (
                  (state = 'COMMITTED' AND created_at >= ? - 86400)
                  OR
                  (state = 'RESERVED' AND expires_at >= ?)
              );
            """,
            ("pol_p2_test", 1729000000, 1729000000),
        )
        plan_rows = cursor.fetchall()
        plan_text = " ".join(str(row) for row in plan_rows)

        # Assert query plan performs an index search and NOT a full table scan
        assert "idx_budget_rolling_sum" in plan_text
        assert "SEARCH" in plan_text
        assert "SCAN ap_budget_ledger" not in plan_text
        conn.close()

    def test_p2_2_lock_contention_and_recovery(self) -> None:
        """P2-2: When another connection holds an exclusive lock, fail closed safely and recover."""
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db_path = tmp.name

        # Initialize schema
        setup_conn = create_buyer_connection(db_path)
        init_buyer_database(setup_conn)
        setup_conn.close()

        # Conn B: low busy_timeout (10ms) initialized first
        conn_b = sqlite3.connect(db_path, timeout=0.01)
        conn_b.execute("PRAGMA busy_timeout = 10;")
        engine_b = SQLitePolicyEngine(conn=conn_b, config=_make_p2_config())

        # Conn A: holds exclusive lock with BEGIN IMMEDIATE
        conn_a = create_buyer_connection(db_path)
        conn_a.execute("BEGIN IMMEDIATE;")

        # Attempt reserve while locked -> must fail-closed safely without crashing
        decision = engine_b.reserve(
            intent_id="i_locked_attempt",
            amount=10_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert decision.approved is False

        # Release lock on Conn A
        conn_a.execute("ROLLBACK;")
        conn_a.close()

        # Conn B can now proceed normally and make reservations
        decision_recovered = engine_b.reserve(
            intent_id="i_recovered_attempt",
            amount=10_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert decision_recovered.approved is True
        conn_b.close()

    def test_p2_3_multiprocess_concurrency(self) -> None:
        """P2-3: Multi-process concurrency test using Python multiprocessing spawn."""
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db_path = tmp.name

        setup_conn = create_buyer_connection(db_path)
        init_buyer_database(setup_conn)
        policy_id = "pol_mp_test"
        cfg = _make_p2_config(
            id=policy_id,
            rolling_budget_24h_base_units=100_000,
            max_per_tx_base_units=20_000,
        )
        SQLitePolicyEngine(conn=setup_conn, config=cfg)
        setup_conn.close()

        # 4 separate processes, each attempting 5 requests of 10_000
        # Total requested: 4 * 5 * 10,000 = 200,000 (budget ceiling is 100,000)
        # At most 10 reservations can be active / committed
        ctx = mp.get_context("spawn")
        with ctx.Pool(processes=4) as pool:
            futures = [
                pool.apply_async(
                    _mp_worker_reserve,
                    args=(db_path, policy_id, worker_id, 5, 10_000),
                )
                for worker_id in range(4)
            ]
            results = [f.get(timeout=15) for f in futures]

        total_approved = sum(results)
        assert total_approved <= 10  # 10 * 10_000 = 100_000 max budget

        # Check total committed and active reserved in database
        verify_conn = create_buyer_connection(db_path)
        cur = verify_conn.cursor()
        cur.execute(
            """
            SELECT COALESCE(SUM(amount_base_units), 0)
            FROM ap_budget_ledger
            WHERE policy_id = ? AND state IN ('RESERVED', 'COMMITTED');
            """,
            (policy_id,),
        )
        total_spent = cur.fetchone()[0]
        assert total_spent <= 100_000
        verify_conn.close()

    def test_p2_4_lifetime_budget_boundary_concurrency(self) -> None:
        """P2-4: When lifetime budget is nearly exhausted, concurrent requests never exceed ceiling."""
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db_path = tmp.name

        conn = create_buyer_connection(db_path)
        init_buyer_database(conn)

        lifetime_cap = 100_000
        cfg = _make_p2_config(
            lifetime_budget_base_units=lifetime_cap,
            rolling_budget_24h_base_units=1_000_000,
            max_per_tx_base_units=100_000,
        )
        engine_init = SQLitePolicyEngine(conn=conn, config=cfg)

        # Pre-fill 90,000 committed spend
        res_prefill = engine_init.reserve(
            intent_id="i_prefill",
            amount=90_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert res_prefill.approved is True
        engine_init.commit("i_prefill")
        conn.close()

        # Exactly 10,000 remains. Launch 10 concurrent threads requesting 5,000 each.
        def thread_task(idx: int) -> bool:
            thread_conn = create_buyer_connection(db_path)
            thread_engine = SQLitePolicyEngine(conn=thread_conn, config=cfg)
            res = thread_engine.reserve(
                intent_id=f"i_race_lifetime_{idx}",
                amount=5_000,
                payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                network="eip155:84532",
            )
            thread_conn.close()
            return res.approved

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(thread_task, i) for i in range(10)]
            results = [f.result() for f in futures]

        approved_count = sum(1 for ok in results if ok)
        rejected_count = sum(1 for ok in results if not ok)

        # Exactly 2 should succeed (2 * 5_000 = 10_000), 8 should be rejected
        assert approved_count == 2
        assert rejected_count == 8

        # Verify DB total commitments
        verify_conn = create_buyer_connection(db_path)
        cur = verify_conn.cursor()
        cur.execute(
            """
            SELECT COALESCE(SUM(amount_base_units), 0)
            FROM ap_budget_ledger
            WHERE policy_id = ? AND state IN ('RESERVED', 'COMMITTED');
            """,
            (cfg.id,),
        )
        total_committed_and_reserved = cur.fetchone()[0]
        assert total_committed_and_reserved == lifetime_cap
        verify_conn.close()

    def test_p2_5_state_transition_rigorous_assertions(self) -> None:
        """P2-5: Comprehensive verification of state transitions, idempotency, and forbidden flows."""
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db_path = tmp.name

        conn = create_buyer_connection(db_path)
        init_buyer_database(conn)
        cfg = _make_p2_config()
        engine = SQLitePolicyEngine(conn=conn, config=cfg)

        intent_id = "i_transition_test"

        # 1. Non-existent intent commit or release is safe no-op
        engine.commit("i_non_existent")
        engine.release("i_non_existent")

        # 2. Reserve
        dec = engine.reserve(
            intent_id=intent_id,
            amount=50_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert dec.approved is True
        assert dec.reservation is not None
        assert dec.reservation.amount == 50_000

        # Check ledger record is RESERVED
        cur = conn.cursor()
        cur.execute(
            "SELECT state, amount_base_units FROM ap_budget_ledger WHERE intent_id = ?;",
            (intent_id,),
        )
        row = cur.fetchone()
        assert row == (LedgerEntryState.RESERVED.value, 50_000)

        # 3. Commit: RESERVED -> COMMITTED
        engine.commit(intent_id)
        cur.execute(
            "SELECT state FROM ap_budget_ledger WHERE intent_id = ?;", (intent_id,)
        )
        assert cur.fetchone()[0] == LedgerEntryState.COMMITTED.value

        # 4. Duplicate commit is idempotent no-op
        engine.commit(intent_id)
        cur.execute(
            "SELECT state FROM ap_budget_ledger WHERE intent_id = ?;", (intent_id,)
        )
        assert cur.fetchone()[0] == LedgerEntryState.COMMITTED.value

        # 5. Attempting to release an already COMMITTED reservation must raise PolicyViolationError
        with pytest.raises(PolicyViolationError) as exc_info:
            engine.release(intent_id)
        assert exc_info.value.code == PolicyErrorCode.RESERVATION_ALREADY_FINALIZED

        # 6. Test RELEASED flow on a separate intent
        intent_rel = "i_release_flow"
        engine.reserve(
            intent_id=intent_rel,
            amount=20_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        engine.release(intent_rel)
        cur.execute(
            "SELECT state FROM ap_budget_ledger WHERE intent_id = ?;", (intent_rel,)
        )
        assert cur.fetchone()[0] == LedgerEntryState.RELEASED.value

        # Duplicate release is idempotent no-op
        engine.release(intent_rel)
        cur.execute(
            "SELECT state FROM ap_budget_ledger WHERE intent_id = ?;", (intent_rel,)
        )
        assert cur.fetchone()[0] == LedgerEntryState.RELEASED.value

        # Attempting to commit an already RELEASED reservation is rejected
        engine.commit(intent_rel)
        cur.execute(
            "SELECT state FROM ap_budget_ledger WHERE intent_id = ?;", (intent_rel,)
        )
        assert cur.fetchone()[0] == LedgerEntryState.RELEASED.value

        conn.close()

    def test_p2_6_rejects_invalid_whitelist_types(self) -> None:
        """P2-6: Non-iterable or invalid type configurations for whitelists raise TypeError."""
        # String passed directly instead of list
        with pytest.raises((ValidationError, TypeError)):
            _make_p2_config(
                whitelist_payees="0x70997970c51812dc3a010c7d01b50e0d17dc79c8"
            )

        # Integer passed instead of list
        with pytest.raises((ValidationError, TypeError)):
            _make_p2_config(whitelist_assets=12345)

        # List containing non-string elements
        with pytest.raises((ValidationError, TypeError)):
            _make_p2_config(whitelist_networks=[123])

        with pytest.raises((ValidationError, TypeError)):
            _make_p2_config(whitelist_domains=[True])

    def test_p2_7_rpm_sliding_window_metric(self) -> None:
        """P2-7: Verify RPM metric counts exactly within the rolling 60-second window."""
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db_path = tmp.name

        class ControllableClock:
            def __init__(self, start: int = 1729000000):
                self.t = start

            def __call__(self) -> int:
                return self.t

        clock = ControllableClock(1729000000)
        conn = create_buyer_connection(db_path)
        init_buyer_database(conn)

        cfg = _make_p2_config(circuit_max_rpm=3)
        engine = SQLitePolicyEngine(conn=conn, config=cfg, clock=clock)

        # Make 3 reservations at t=0
        for i in range(3):
            dec = engine.reserve(
                intent_id=f"i_rpm_{i}",
                amount=1_000,
                payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                network="eip155:84532",
            )
            assert dec.approved is True

        # 4th reservation at t=10s trips breaker (RPM=3 exceeded in last 60s)
        clock.t += 10
        dec_trip = engine.reserve(
            intent_id="i_rpm_exceed",
            amount=1_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert dec_trip.approved is False
        assert dec_trip.error_code == PolicyErrorCode.CIRCUIT_BREAKER_OPEN

        # Reset breaker and advance clock past 60s (t = 1729000075)
        engine.reset_breaker()
        clock.t = 1729000075

        # Reservations from t=0 are now older than 60s, so new reservation succeeds
        dec_new = engine.reserve(
            intent_id="i_rpm_new_window",
            amount=1_000,
            payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
            asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
            network="eip155:84532",
        )
        assert dec_new.approved is True
        conn.close()
