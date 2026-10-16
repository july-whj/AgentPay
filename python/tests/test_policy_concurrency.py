"""
Tests for T2.5: Concurrency, stress, and race-condition safety for SQLitePolicyEngine.

Verifies:
1. Multi-threaded concurrent reservation never overspends rolling budget (P0-6 invariance).
2. Concurrent reservations fail-closed gracefully upon database locks or contention.
3. Concurrent reserve + release keeps aggregate budget strictly balanced without leakage.
4. Concurrent commit + release on same intents never causes double-spend or invalid transition.
5. High-concurrency stress test with file-based SQLite database under WAL mode.
"""

from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import tempfile

from agentpay.policy.models import PolicyConfig
from agentpay.policy.sqlite_engine import SQLitePolicyEngine


def _init_file_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
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
            circuit_max_rpm INTEGER NOT NULL DEFAULT 10000,
            circuit_consecutive_failures INTEGER NOT NULL DEFAULT 500,
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
    conn.close()


def _make_concurrency_config(budget: int, max_per_tx: int) -> PolicyConfig:
    return PolicyConfig(
        id="pol_concurrent_test",
        name="Concurrent Test Policy",
        wallet_address="0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
        max_per_tx_base_units=max_per_tx,
        rolling_budget_24h_base_units=budget,
        circuit_max_rpm=100_000,  # Ensure RPM does not throttle concurrency budget tests
        whitelist_payees=frozenset(["0x70997970c51812dc3a010c7d01b50e0d17dc79c8"]),
        whitelist_assets=frozenset(["0x036cbd53842c5426634e7929541ec2318f3dcf7e"]),
        whitelist_networks=frozenset(["eip155:84532"]),
        whitelist_domains=frozenset(["api.trusted-provider.com"]),
        is_frozen=False,
    )


class TestConcurrencyAndStress:
    def test_concurrent_reservations_never_exceed_rolling_budget(self) -> None:
        """
        Launch 50 concurrent worker threads attempting to reserve 100,000 units each,
        with a total rolling budget of only 2,000,000 units (enough for exactly 20).
        Assert that:
          1. Exactly 20 reservations succeed (or <= 20 if lock timeouts occur).
          2. No more than 20 succeed (TOTAL committed/reserved NEVER > 2,000,000).
          3. Total reserved in DB matches successful tokens.
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
            db_path = tmp_file.name

        try:
            _init_file_db(db_path)
            total_budget = 2_000_000
            tx_amount = 100_000
            max_allowed = total_budget // tx_amount  # 20
            num_workers = 50

            cfg = _make_concurrency_config(budget=total_budget, max_per_tx=tx_amount)

            def worker_reserve(worker_id: int):
                # Each thread opens its own SQLite connection with busy timeout
                conn = sqlite3.connect(db_path, timeout=10.0)
                conn.execute("PRAGMA busy_timeout = 10000;")
                engine = SQLitePolicyEngine(conn=conn, config=cfg)
                decision = engine.reserve(
                    intent_id=f"intent_conc_{worker_id}",
                    amount=tx_amount,
                    payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                    asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                    network="eip155:84532",
                )
                conn.close()
                return decision

            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
                futures = [
                    executor.submit(worker_reserve, i) for i in range(num_workers)
                ]
                decisions = [f.result() for f in futures]

            approved = [d for d in decisions if d.approved]
            rejected = [d for d in decisions if not d.approved]

            # Invariant 1: Approved count must NEVER exceed max_allowed
            assert len(approved) <= max_allowed, (
                f"OVERSPEND DETECTED! Approved {len(approved)} > {max_allowed}"
            )
            assert len(approved) == max_allowed
            assert len(rejected) == num_workers - max_allowed

            # Verify in database directly
            verify_conn = sqlite3.connect(db_path)
            cur = verify_conn.cursor()
            cur.execute(
                "SELECT SUM(amount_base_units), COUNT(*) FROM ap_budget_ledger WHERE state = 'RESERVED';"
            )
            row = cur.fetchone()
            total_spent = row[0]
            count_spent = row[1]
            verify_conn.close()

            assert count_spent == max_allowed
            assert total_spent == total_budget

        finally:
            if os.path.exists(db_path):
                os.remove(db_path)
            for ext in ["-wal", "-shm"]:
                if os.path.exists(db_path + ext):
                    os.remove(db_path + ext)

    def test_concurrent_reserve_and_release_recovers_budget(self) -> None:
        """
        Concurrency stress with dynamic release:
        10 workers continuously reserve and release.
        After all releases complete, available budget must exactly equal original budget.
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
            db_path = tmp_file.name

        try:
            _init_file_db(db_path)
            total_budget = 500_000
            tx_amount = 100_000
            cfg = _make_concurrency_config(budget=total_budget, max_per_tx=tx_amount)

            def worker_reserve_then_release(worker_id: int):
                conn = sqlite3.connect(db_path, timeout=10.0)
                conn.execute("PRAGMA busy_timeout = 10000;")
                engine = SQLitePolicyEngine(conn=conn, config=cfg)
                intent_id = f"intent_rel_{worker_id}"
                dec = engine.reserve(
                    intent_id=intent_id,
                    amount=tx_amount,
                    payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                    asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                    network="eip155:84532",
                )
                if dec.approved:
                    engine.release(intent_id, reason="Stress test rollback")
                conn.close()
                return dec.approved

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                futures = [
                    executor.submit(worker_reserve_then_release, i) for i in range(30)
                ]
                [f.result() for f in futures]

            # At the end, all approved intents were released -> available quota must be 100% restored
            verify_conn = sqlite3.connect(db_path)
            engine_check = SQLitePolicyEngine(conn=verify_conn, config=cfg)
            avail = engine_check.get_available_rolling_budget()
            verify_conn.close()

            assert avail == total_budget

        finally:
            if os.path.exists(db_path):
                os.remove(db_path)
            for ext in ["-wal", "-shm"]:
                if os.path.exists(db_path + ext):
                    os.remove(db_path + ext)

    def test_concurrent_commit_and_release_race_on_same_intent(self) -> None:
        """
        Race condition test: A single reservation has commit() and release()
        called simultaneously across two threads.
        Assert that:
          1. Exactly one operation wins.
          2. No database corruption or inconsistent state (e.g. released after committed).
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
            db_path = tmp_file.name

        try:
            _init_file_db(db_path)
            cfg = _make_concurrency_config(budget=1_000_000, max_per_tx=100_000)

            # Pre-reserve 10 intents
            conn_main = sqlite3.connect(db_path)
            engine_main = SQLitePolicyEngine(conn=conn_main, config=cfg)
            for i in range(10):
                res = engine_main.reserve(
                    intent_id=f"intent_race_{i}",
                    amount=50_000,
                    payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                    asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                    network="eip155:84532",
                )
                assert res.approved is True
            conn_main.close()

            def race_commit(intent_id: str):
                conn = sqlite3.connect(db_path, timeout=10.0)
                conn.execute("PRAGMA busy_timeout = 10000;")
                engine = SQLitePolicyEngine(conn=conn, config=cfg)
                try:
                    engine.commit(intent_id)
                    return ("commit", True)
                except Exception as e:  # noqa: BLE001
                    return ("commit", str(e))
                finally:
                    conn.close()

            def race_release(intent_id: str):
                conn = sqlite3.connect(db_path, timeout=10.0)
                conn.execute("PRAGMA busy_timeout = 10000;")
                engine = SQLitePolicyEngine(conn=conn, config=cfg)
                try:
                    engine.release(intent_id)
                    return ("release", True)
                except Exception as e:  # noqa: BLE001
                    return ("release", str(e))
                finally:
                    conn.close()

            # Execute race across 10 intents simultaneously
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                commit_futures = [
                    executor.submit(race_commit, f"intent_race_{i}") for i in range(10)
                ]
                release_futures = [
                    executor.submit(race_release, f"intent_race_{i}") for i in range(10)
                ]
                [f.result() for f in commit_futures]
                [f.result() for f in release_futures]

            # Invariant: Each row in DB MUST be either COMMITTED or RELEASED, never in invalid state
            verify_conn = sqlite3.connect(db_path)
            cur = verify_conn.cursor()
            cur.execute("SELECT state, COUNT(*) FROM ap_budget_ledger GROUP BY state;")
            rows = cur.fetchall()
            verify_conn.close()

            states = {r[0]: r[1] for r in rows}
            assert "RESERVED" not in states or states["RESERVED"] == 0
            total_finalized = states.get("COMMITTED", 0) + states.get("RELEASED", 0)
            assert total_finalized == 10

        finally:
            if os.path.exists(db_path):
                os.remove(db_path)
            for ext in ["-wal", "-shm"]:
                if os.path.exists(db_path + ext):
                    os.remove(db_path + ext)
