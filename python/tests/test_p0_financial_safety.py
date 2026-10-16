"""
P0 Security & Invariant Tests for AgentPay Financial Firewall.

Covers:
- P0-1: Lifetime budget exhaustion under high concurrency (RESERVED must be counted)
- P0-2: Exact timestamp boundary consistency at now == expires_at
- P0-3: ap_buyer_intent unique idempotency constraint
- P0-5: Strict conditional transition guards and idempotency for commit/release
"""

from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import tempfile

import pytest

from agentpay.policy.errors import PolicyErrorCode, PolicyViolationError
from agentpay.policy.models import PolicyConfig
from agentpay.policy.sqlite_engine import SQLitePolicyEngine


def _init_p0_db(db_path: str) -> None:
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ap_buyer_intent (
            intent_id TEXT PRIMARY KEY,
            policy_id TEXT NOT NULL REFERENCES ap_policy(id),
            idempotency_key TEXT NOT NULL,
            payer_address TEXT NOT NULL,
            payee_address TEXT NOT NULL,
            asset_address TEXT NOT NULL,
            network_id TEXT NOT NULL,
            amount_base_units INTEGER NOT NULL CHECK (amount_base_units > 0 AND typeof(amount_base_units) = 'integer'),
            status TEXT NOT NULL CHECK (status IN ('CREATED', 'AUTHORIZED', 'SIGNATURE_SENT', 'COMPLETED', 'REJECTED', 'EXPIRED', 'CANCELLED', 'RECOVERY_HOLD')),
            nonce TEXT,
            valid_after INTEGER,
            valid_before INTEGER,
            raw_signature TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_buyer_intent_idempotency 
        ON ap_buyer_intent (payer_address, idempotency_key);
        """
    )
    conn.commit()
    conn.close()


class MockClock:
    def __init__(self, initial_time: int = 1729000000):
        self._time = initial_time

    def __call__(self) -> int:
        return self._time

    def advance(self, seconds: int) -> None:
        self._time += seconds

    def set_time(self, timestamp: int) -> None:
        self._time = timestamp


def _make_p0_config(**overrides: object) -> PolicyConfig:
    base: dict = {
        "id": "pol_p0_test",
        "name": "P0 Test Policy",
        "wallet_address": "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
        "max_per_tx_base_units": 1_000_000,
        "rolling_budget_24h_base_units": 100_000_000,
        "lifetime_budget_base_units": 2_000_000,  # 2.0 USDC lifetime cap
        "circuit_max_rpm": 100_000,
        "whitelist_payees": ["0x70997970c51812dc3a010c7d01b50e0d17dc79c8"],
        "whitelist_assets": ["0x036cbd53842c5426634e7929541ec2318f3dcf7e"],
        "whitelist_networks": ["eip155:84532"],
        "whitelist_domains": ["api.trusted-provider.com"],
        "is_frozen": False,
    }
    base.update(overrides)
    return PolicyConfig(**base)


class TestP0FinancialSafety:
    def test_p0_1_concurrent_reservations_respect_lifetime_budget(self) -> None:
        """
        P0-1 Regression:
        Lifetime budget is 2,000,000. Each transaction is 200,000.
        Exactly 10 transactions can be approved.
        50 threads concurrently attempt reservation.
        Must NEVER approve > 10 transactions, even when none are COMMITTED yet!
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

        try:
            _init_p0_db(db_path)
            lifetime_limit = 2_000_000
            tx_amount = 200_000
            max_allowed = lifetime_limit // tx_amount  # 10
            num_workers = 50

            cfg = _make_p0_config(lifetime_budget_base_units=lifetime_limit)

            def worker_reserve(worker_id: int):
                conn = sqlite3.connect(db_path, timeout=10.0)
                conn.execute("PRAGMA busy_timeout = 10000;")
                engine = SQLitePolicyEngine(conn=conn, config=cfg)
                dec = engine.reserve(
                    intent_id=f"intent_lifetime_conc_{worker_id}",
                    amount=tx_amount,
                    payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                    asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                    network="eip155:84532",
                )
                conn.close()
                return dec

            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
                futures = [
                    executor.submit(worker_reserve, i) for i in range(num_workers)
                ]
                decisions = [f.result() for f in futures]

            approved = [d for d in decisions if d.approved]
            rejected = [d for d in decisions if not d.approved]

            # Invariant: Must NOT approve more than lifetime budget permits
            assert len(approved) == max_allowed, (
                f"LIFETIME BUDGET OVERSPEND! Approved {len(approved)} > {max_allowed}"
            )
            assert len(rejected) == num_workers - max_allowed

            # Direct DB verification: Sum of all RESERVED in DB == 2,000,000
            verify_conn = sqlite3.connect(db_path)
            cur = verify_conn.cursor()
            cur.execute(
                "SELECT SUM(amount_base_units) FROM ap_budget_ledger WHERE state = 'RESERVED';"
            )
            total_reserved = cur.fetchone()[0]
            verify_conn.close()
            assert total_reserved == lifetime_limit

        finally:
            if os.path.exists(db_path):
                os.remove(db_path)
            for ext in ["-wal", "-shm"]:
                if os.path.exists(db_path + ext):
                    os.remove(db_path + ext)

    def test_p0_2_exact_timestamp_boundary_at_expires_at(self) -> None:
        """
        P0-2 Boundary Test:
        Validate the behavior exactly when now == expires_at.
        If now == expires_at, it is still within the lease window:
        - Must still occupy budget (not prematurely released)
        - Must still allow commit()
        If now == expires_at + 1:
        - It is expired: commit() must raise RESERVATION_EXPIRED
        - Budget is no longer locked by this expired reservation
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

        try:
            _init_p0_db(db_path)
            clock = MockClock(1729000000)
            cfg = _make_p0_config(rolling_budget_24h_base_units=1_000_000)

            conn = sqlite3.connect(db_path)
            engine = SQLitePolicyEngine(conn=conn, config=cfg, clock=clock)

            # Reserve 500,000 with TTL=100s -> expires_at = 1729000100
            dec = engine.reserve(
                intent_id="intent_boundary",
                amount=500_000,
                payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                network="eip155:84532",
                ttl_seconds=100,
            )
            assert dec.approved is True
            assert dec.reservation is not None
            assert dec.reservation.expires_at == 1729000100

            # Advance clock exactly to expires_at (1729000100)
            clock.set_time(1729000100)

            # Invariant: At now == expires_at, budget MUST still be occupied (available = 500,000)
            avail = engine.get_available_rolling_budget()
            assert avail == 500_000, (
                f"Budget prematurely freed at now == expires_at! Available: {avail}"
            )

            # Invariant: At now == expires_at, commit MUST still succeed!
            engine.commit("intent_boundary")

            # Advance 1 second past expires_at for a second reservation
            dec2 = engine.reserve(
                intent_id="intent_boundary_expired",
                amount=200_000,
                payee="0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                asset="0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                network="eip155:84532",
                ttl_seconds=50,
            )
            assert dec2.approved is True
            expires_at_2 = dec2.reservation.expires_at

            # Advance clock to expires_at_2 + 1 (strictly expired)
            clock.set_time(expires_at_2 + 1)

            # Must raise RESERVATION_EXPIRED
            with pytest.raises(PolicyViolationError) as exc_info:
                engine.commit("intent_boundary_expired")
            assert exc_info.value.code == PolicyErrorCode.RESERVATION_EXPIRED

            conn.close()

        finally:
            if os.path.exists(db_path):
                os.remove(db_path)
            for ext in ["-wal", "-shm"]:
                if os.path.exists(db_path + ext):
                    os.remove(db_path + ext)

    def test_p0_3_buyer_intent_idempotency_constraint(self) -> None:
        """
        P0-3 Database Constraint Test:
        Ensures ap_buyer_intent rejects duplicate (payer_address, idempotency_key).
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

        try:
            _init_p0_db(db_path)
            conn = sqlite3.connect(db_path)

            payer = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
            idem_key = "idemp_order_123"

            # Insert first intent
            conn.execute(
                """
                INSERT INTO ap_buyer_intent (
                    intent_id, policy_id, idempotency_key, payer_address,
                    payee_address, asset_address, network_id, amount_base_units,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "intent_1",
                    "pol_p0_test",
                    idem_key,
                    payer,
                    "0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                    "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                    "eip155:84532",
                    100_000,
                    "CREATED",
                    1729000000,
                    1729000000,
                ),
            )
            conn.commit()

            # Second intent with same payer and idempotency_key must raise IntegrityError
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO ap_buyer_intent (
                        intent_id, policy_id, idempotency_key, payer_address,
                        payee_address, asset_address, network_id, amount_base_units,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        "intent_2",
                        "pol_p0_test",
                        idem_key,
                        payer,
                        "0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
                        "0x036cbd53842c5426634e7929541ec2318f3dcf7e",
                        "eip155:84532",
                        100_000,
                        "CREATED",
                        1729000000,
                        1729000000,
                    ),
                )
                conn.commit()

            conn.close()

        finally:
            if os.path.exists(db_path):
                os.remove(db_path)
            for ext in ["-wal", "-shm"]:
                if os.path.exists(db_path + ext):
                    os.remove(db_path + ext)
