"""
Database connection utilities and connection factory for AgentPay.

Enforces:
- Foreign key constraint enforcement on EVERY connection (PRAGMA foreign_keys = ON)
- WAL journal mode for concurrent read/write isolation (PRAGMA journal_mode = WAL)
- Busy timeout configuration to gracefully wait during concurrent write contention
- Canonical buyer schema initialization helper
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def create_buyer_connection(
    db_path: str | Path,
    busy_timeout_ms: int = 5000,
    check_same_thread: bool = False,
) -> sqlite3.Connection:
    """
    Factory function to create a standardized SQLite connection to the Buyer database.

    Guarantees:
    - Connection-level ``PRAGMA foreign_keys = ON;``
    - Connection-level ``PRAGMA busy_timeout = {busy_timeout_ms};``
    - Database-level ``PRAGMA journal_mode = WAL;``

    Args:
        db_path: File path or ':memory:' for in-memory databases.
        busy_timeout_ms: Timeout in milliseconds for locked database retries.
        check_same_thread: Whether SQLite restricts connection to the creator thread.

    Returns:
        Configured sqlite3.Connection.
    """
    str_path = str(db_path)
    conn = sqlite3.connect(
        str_path,
        timeout=float(busy_timeout_ms) / 1000.0,
        check_same_thread=check_same_thread,
    )
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms};")
    if str_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_buyer_database(conn: sqlite3.Connection) -> None:
    """
    Initialize the buyer database tables and indexes from schema DDL.
    """
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
        CREATE INDEX IF NOT EXISTS idx_budget_rolling_sum 
        ON ap_budget_ledger (policy_id, state, created_at, expires_at);
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_buyer_intent_idempotency 
        ON ap_buyer_intent (payer_address, idempotency_key);
        """
    )
    conn.commit()
