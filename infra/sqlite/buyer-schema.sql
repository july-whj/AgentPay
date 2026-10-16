-- ============================================================================
-- AgentPay Buyer SQLite Database Schema (infra/sqlite/buyer-schema.sql)
-- Target: Agent Host Process (python/agentpay)
-- ============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- 1. Policy Configuration Table
CREATE TABLE IF NOT EXISTS ap_policy (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    max_per_tx_base_units INTEGER NOT NULL CHECK (max_per_tx_base_units > 0),
    rolling_budget_24h_base_units INTEGER NOT NULL CHECK (rolling_budget_24h_base_units > 0),
    lifetime_budget_base_units INTEGER CHECK (lifetime_budget_base_units > 0),
    
    -- Whitelist rules (Stored as canonical lowercase JSON string array)
    whitelist_payees_json TEXT NOT NULL DEFAULT '[]',
    whitelist_assets_json TEXT NOT NULL DEFAULT '[]',
    whitelist_networks_json TEXT NOT NULL DEFAULT '[]',
    whitelist_domains_json TEXT NOT NULL DEFAULT '[]',
    
    -- Circuit breaker configuration & live state
    circuit_max_rpm INTEGER NOT NULL DEFAULT 15,
    circuit_consecutive_failures INTEGER NOT NULL DEFAULT 5,
    circuit_cooldown_seconds INTEGER NOT NULL DEFAULT 300,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    breaker_state TEXT NOT NULL DEFAULT 'CLOSED' CHECK (breaker_state IN ('CLOSED', 'OPEN', 'HALF_OPEN')),
    tripped_at INTEGER,                     -- Unix seconds
    is_frozen INTEGER NOT NULL DEFAULT 0 CHECK (is_frozen IN (0, 1)), -- 0: false, 1: true
    
    created_at INTEGER NOT NULL,            -- Unix seconds
    updated_at INTEGER NOT NULL             -- Unix seconds
);

-- 2. Budget Reservation Ledger Table (P0-6 True Rolling Math)
CREATE TABLE IF NOT EXISTS ap_budget_ledger (
    id TEXT PRIMARY KEY,                    -- UUIDv7 reservation ID
    policy_id TEXT NOT NULL REFERENCES ap_policy(id),
    wallet_address TEXT NOT NULL,           -- Bound to payer wallet address
    intent_id TEXT NOT NULL UNIQUE,         -- High-level purchase intent ID
    amount_base_units INTEGER NOT NULL CHECK (amount_base_units > 0 AND typeof(amount_base_units) = 'integer'),
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'COMMITTED', 'RELEASED')),
    created_at INTEGER NOT NULL,            -- Unix seconds
    expires_at INTEGER NOT NULL,            -- Unix seconds (validBefore + safe buffer)
    updated_at INTEGER NOT NULL             -- Unix seconds
);

-- 3. Buyer Payment Intent Table
CREATE TABLE IF NOT EXISTS ap_buyer_intent (
    intent_id TEXT PRIMARY KEY,             -- UUIDv7
    policy_id TEXT NOT NULL REFERENCES ap_policy(id),
    idempotency_key TEXT NOT NULL,          -- Client provided idempotency token
    payer_address TEXT NOT NULL,            -- Canonical lowercase 0x address
    payee_address TEXT NOT NULL,            -- Canonical lowercase 0x address
    asset_address TEXT NOT NULL,            -- Canonical lowercase 0x token contract
    network_id TEXT NOT NULL,               -- CAIP-2 network string (e.g. eip155:84532)
    amount_base_units INTEGER NOT NULL CHECK (amount_base_units > 0 AND typeof(amount_base_units) = 'integer'),
    
    -- Buyer State Machine Dimension (P0-1 Fix)
    status TEXT NOT NULL CHECK (status IN ('CREATED', 'AUTHORIZED', 'SIGNATURE_SENT', 'COMPLETED', 'REJECTED', 'EXPIRED', 'CANCELLED', 'RECOVERY_HOLD')),
    
    -- Cryptographic Proofs
    nonce TEXT,                             -- 32-byte hex nonce
    valid_after INTEGER,                    -- Unix seconds
    valid_before INTEGER,                   -- Unix seconds
    raw_signature TEXT,                     -- Serialized (v, r, s) hex signature
    
    created_at INTEGER NOT NULL,            -- Unix seconds
    updated_at INTEGER NOT NULL             -- Unix seconds
);

-- Indexes for high-performance rolling sum and idempotency uniqueness (P0-3, P2-1)
CREATE INDEX IF NOT EXISTS idx_budget_rolling_sum 
ON ap_budget_ledger (policy_id, state, created_at, expires_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_buyer_intent_idempotency 
ON ap_buyer_intent (payer_address, idempotency_key);
