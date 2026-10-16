# AgentPay Domain Model & Database Specification

> **Specification Version:** 1.2.0  
> **Status:** Draft  
> **Target Runtimes:** SQLite 3.35+ (WAL mode), Python SDK (`agentpay`), Self-hosted Gateway  
> **Task Tracking:** 2026 Plan Milestone M2 (Tasks T1.1, T1.3, T4.1, P0-3, P0-6, P0-7, P0-8)

---

## 1. Physical Domain Separation: Buyer Database vs. Gateway Database

To preserve strict separation of concerns and eliminate shared-database bottlenecks, AgentPay partitions data persistence into **two independent physical SQLite databases**:

1. **Buyer Database (`buyer_agentpay.db`)**: Runs on the Agent host machine. Persists policy rules, wallet-scoped true rolling budgets, and outbound payment intents.
   * Defined in canonical DDL: [`infra/sqlite/buyer-schema.sql`](file:///Users/wanghongjie/aiPoiject/AgentPay/infra/sqlite/buyer-schema.sql)
2. **Gateway Database (`gateway_agentpay.db`)**: Runs on the Seller / API Provider host machine. Persists verified inbound payments, on-chain settlement queues, and idempotency response caches.
   * Defined in canonical DDL: [`infra/sqlite/gateway-schema.sql`](file:///Users/wanghongjie/aiPoiject/AgentPay/infra/sqlite/gateway-schema.sql)

---

## 2. Core Entities & Invariants

### 2.1 Buyer Entities (`buyer_agentpay.db`)

#### `ap_policy`
Represents the local financial guardrail configuration for an agent wallet.
* **Fields**: `id`, `name`, `wallet_address`, `max_per_tx_base_units`, `rolling_budget_24h_base_units`, `lifetime_budget_base_units`, `whitelist_payees_json`, `whitelist_assets_json`, `whitelist_networks_json`, `whitelist_domains_json`, `circuit_max_rpm`, `circuit_consecutive_failures`, `circuit_cooldown_seconds`, `breaker_state`, `tripped_at`, `is_frozen`, `created_at`, `updated_at`.
* **Invariant**: One policy file governs one specific wallet address. Multiple agent instances sharing a single wallet MUST share the same SQLite database file to prevent multi-instance budget bypass.

#### `ap_budget_ledger` (True Rolling Window Ledger)
Tracks atomic reservation, commitment, and release of budget units.
* **Fields**: `id` (UUIDv7 reservation ID), `policy_id`, `wallet_address`, `intent_id`, `amount_base_units`, `state` (`RESERVED`, `COMMITTED`, `RELEASED`), `created_at`, `expires_at`, `updated_at`.
* **Mathematical Invariant (P0-6)**:
  $$\text{CurrentUsage} = \sum \{ \text{amount} \mid (\text{state}=\text{'COMMITTED'} \land \text{created\_at} \ge \text{now}-86400) \lor (\text{state}=\text{'RESERVED'} \land \text{expires\_at} > \text{now}) \}$$
  $$\text{AvailableQuota} = \text{rolling\_budget\_24h} - \text{CurrentUsage}$$

#### `ap_buyer_intent`
Tracks outgoing purchase actions from initial challenge to final receipt.
* **Status**: `CREATED`, `AUTHORIZED`, `SIGNATURE_SENT`, `COMPLETED`, `REJECTED`, `EXPIRED`, `CANCELLED`, `RECOVERY_HOLD`.

---

### 2.2 Gateway Entities (`gateway_agentpay.db`)

#### `ap_gateway_settlement`
Records verified x402 payment authorizations and blockchain settlement.
* **Replay Defense (P0-7)**:
  $$\mathbf{UNIQUE(network\_id,\ asset\_address,\ payer\_address,\ nonce)}$$
  Prevents duplicate submission of the same EIP-3009 authorization across requests.

#### `ap_gateway_fulfillment`
Tracks whether the upstream API delivered the requested resource.
* **Status**: `NOT_STARTED`, `PROCESSING`, `DELIVERED`, `FAILED`, `RECOVERY_REQUIRED`.

#### `ap_idempotency_cache`
Protects upstream services and guarantees strict once-and-only-once execution.
* **Scoping (P1-5)**: `PRIMARY KEY (payer_address, idempotency_key)`
* **Leasing (P1-14)**: `locked_until` (Unix seconds) prevents concurrent duplicate upstream execution.
* **Payload Cache (P1-17)**: `response_body_blob` caches binary responses with maximum byte size thresholds.

---

## 3. SQLite Storage Rules & Precision Constraints (P0-8)

1. **Integer Base Units Only:**
   * All amounts are strictly stored as `INTEGER` base units (e.g., `100000` = 0.1 USDC with 6 decimals).
   * SQLite enforces: `CHECK (amount_base_units > 0 AND typeof(amount_base_units) = 'integer')`.
   * **Asset Decimal Constraint (MVP):** The 2026 baseline supports token assets with $\le 6$ decimals (such as USDC/EURC). 18-decimal tokens (e.g. DAI) exceed SQLite's 64-bit signed integer limits ($2^{63}-1 \approx 9.22 \times 10^{18}$) and will be introduced in 2027 using stringified BigInteger storage.
2. **Unix Timestamps (Seconds):**
   * All timestamps (`created_at`, `updated_at`, `valid_after`, `valid_before`, `expires_at`) are stored as 64-bit integer Unix seconds (`INTEGER`).
3. **Address Normalization (P1-16):**
   * All EVM addresses (`payer_address`, `payee_address`, `asset_address`) are converted to canonical **lowercase hex** strings before database insertion and query comparison.
