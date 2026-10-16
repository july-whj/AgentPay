-- ============================================================================
-- AgentPay Gateway SQLite Database Schema (infra/sqlite/gateway-schema.sql)
-- Target: Self-Hosted Reverse Proxy Gateway (python/agentpay or gateway daemon)
-- ============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- 1. Gateway Inbound Payment & Settlement State
CREATE TABLE IF NOT EXISTS ap_gateway_settlement (
    settlement_id TEXT PRIMARY KEY,         -- UUIDv7
    idempotency_key TEXT NOT NULL,
    payer_address TEXT NOT NULL,            -- Canonical lowercase 0x address
    payee_address TEXT NOT NULL,            -- Canonical lowercase 0x address
    asset_address TEXT NOT NULL,            -- Canonical lowercase 0x token contract
    network_id TEXT NOT NULL,               -- CAIP-2 network string
    amount_base_units INTEGER NOT NULL CHECK (amount_base_units > 0 AND typeof(amount_base_units) = 'integer'),
    
    -- Cryptographic Proofs & Replay Defense (P0-7 Fix)
    nonce TEXT NOT NULL,
    valid_after INTEGER NOT NULL,
    valid_before INTEGER NOT NULL,
    raw_signature TEXT NOT NULL,
    
    -- Settlement Status (P1-1 Fix)
    status TEXT NOT NULL DEFAULT 'NOT_STARTED' CHECK (status IN ('NOT_STARTED', 'SUBMITTED', 'PENDING', 'CONFIRMED', 'FAILED', 'REORGED', 'UNKNOWN')),
    tx_hash TEXT,
    block_number INTEGER,
    error_message TEXT,
    
    created_at INTEGER NOT NULL,            -- Unix seconds
    updated_at INTEGER NOT NULL,            -- Unix seconds
    
    -- P0-7: Nonce uniquely bound to network, token, and payer
    CONSTRAINT uq_network_asset_payer_nonce UNIQUE (network_id, asset_address, payer_address, nonce)
);

-- 2. Gateway Upstream Fulfillment Delivery State
CREATE TABLE IF NOT EXISTS ap_gateway_fulfillment (
    fulfillment_id TEXT PRIMARY KEY,        -- UUIDv7
    settlement_id TEXT NOT NULL REFERENCES ap_gateway_settlement(settlement_id),
    status TEXT NOT NULL DEFAULT 'NOT_STARTED' CHECK (status IN ('NOT_STARTED', 'PROCESSING', 'DELIVERED', 'FAILED', 'RECOVERY_REQUIRED')),
    upstream_status_code INTEGER,
    response_hash TEXT,                     -- SHA-256 of delivered payload
    retry_count INTEGER NOT NULL DEFAULT 0,
    delivered_at INTEGER,                   -- Unix seconds
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- 3. Idempotency Cache Table (P1-3, P1-5, P1-14, P1-17 Fixes)
CREATE TABLE IF NOT EXISTS ap_idempotency_cache (
    payer_address TEXT NOT NULL,            -- Scoped by payer address
    idempotency_key TEXT NOT NULL,
    settlement_id TEXT REFERENCES ap_gateway_settlement(settlement_id),
    status TEXT NOT NULL CHECK (status IN ('IN_FLIGHT', 'COMPLETED', 'FAILED')),
    request_hash TEXT NOT NULL,             -- SHA-256 parameter fingerprint
    response_status_code INTEGER,
    response_headers_json TEXT,
    response_body_blob BLOB,                -- Binary cached response (size capped)
    locked_until INTEGER NOT NULL,          -- Lease lock expiration (Unix seconds)
    expires_at INTEGER NOT NULL,            -- Cache retention eviction (Unix seconds)
    created_at INTEGER NOT NULL,
    
    -- P1-5: Jointly unique by payer and idempotency key
    PRIMARY KEY (payer_address, idempotency_key)
);

-- 4. Tamper-Evident Audit Event Log (P1-15 Fix)
CREATE TABLE IF NOT EXISTS ap_audit_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payer_address TEXT,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,                -- Hash chaining for integrity
    entry_hash TEXT NOT NULL,               -- SHA-256(id + prev_hash + payload_json)
    created_at INTEGER NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_settlement_lookup 
ON ap_gateway_settlement (payer_address, idempotency_key);

CREATE INDEX IF NOT EXISTS idx_idempotency_lease 
ON ap_idempotency_cache (locked_until, status);
