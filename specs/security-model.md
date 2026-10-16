# AgentPay Security & Threat Model Specification

## 1. Threat Model & Attack Vectors

Deploying autonomous agents with payment capabilities introduces novel attack vectors that do not exist in conventional web applications:

| Threat Category | Description | Primary Defense |
| :--- | :--- | :--- |
| **T1: Prompt Injection / Financial Hallucination** | Malicious content in tool responses or prompts instructs LLM to make unauthorized purchases or grant itself permissions. | Out-of-band Policy Engine boundary; LLM has zero programmatic access to modify policies or sign raw bytes. |
| **T2: Infinite-Loop Drain** | Recursive tool calls or logic bugs cause an agent to invoke paid APIs thousands of times in seconds. | Rate limiting, rolling daily budget limits, and automated circuit breakers. |
| **T3: Signature Replay Attacks** | Attacker intercepts a valid `PAYMENT-SIGNATURE` and resubmits it to claim additional resources or cause double transfer. | EIP-3009 32-byte unique cryptographic nonces, short expiration windows (`validBefore`), and database-level idempotency tables. |
| **T4: Malicious / Rogue Sellers** | An untrusted seller presents an exorbitant quote in the x402 challenge (e.g. demanding 1,000 USDC instead of 0.01 USDC). | Per-transaction spending caps, strict merchant/domain whitelisting, and policy pre-authorization. |
| **T5: Key Exfiltration** | Prompts or errors inadvertently expose agent private keys in logs or conversation context. | Signer isolation (KMS / Hardware / local environment signer); raw keys are never passed into LLM prompt contexts. |
| **T6: Double-Spending Race Conditions** | Concurrent agent threads initiate payments exceeding the budget simultaneously. | PostgreSQL row-level locks and atomic compare-and-swap balance deductions. |

---

## 2. The Financial Firewall (Policy Engine Boundary)

```mermaid
graph TD
    subgraph Untrusted Context [Untrusted LLM Context]
        LLM[LLM Agent Core]
        PromptMemory[Prompt & Tool Execution Context]
    end

    subgraph Trust Boundary [Financial Firewall Boundary]
        PolicyEngine[Policy Engine Daemon]
        Rules[(Policy Config & Quota DB)]
        Signer[KMS / Isolated Local Signer]
    end

    subgraph External Network [External Providers]
        Seller[Seller API]
    end

    LLM -->|1. Propose HTTP Action| PolicyEngine
    PolicyEngine -->|2. Verify Cap, Rolling Budget, Whitelist| Rules
    Rules -->>|3. Approved & Quota Pre-Deducted| PolicyEngine
    PolicyEngine -->|4. Request Signature with Scoped Challenge| Signer
    Signer -->>|5. Cryptographic EIP-712 Signature| PolicyEngine
    PolicyEngine -->|6. Execute HTTP Request + Signature| Seller
    
    style Untrusted Context fill:#fff3cd,stroke:#ffeeba
    style Trust Boundary fill:#d4edda,stroke:#c3e6cb
```

### Core Invariants:
1. **No Self-Granting:** The LLM context cannot alter policy variables (`max_per_tx_base_units`, `rolling_budget_base_units`, `whitelist_domains`).
2. **Pre-Deduction:** Quotas are reserved *prior* to cryptographic signing. If a thread exceeds the rolling allowance, execution halts immediately with a typed exception before the wallet signs anything.
3. **Fail-Closed:** In the event of a database connectivity failure, network partition, or unhandled exception within the policy engine, all transactions default to `REJECTED`.

---

## 3. Cryptographic Guardrails (EIP-712 & EIP-3009)

AgentPay adopts **EIP-712 (Typed Structured Data Hashing)** and **EIP-3009 (Transfer With Authorization)**:

### 3.1 Domain Separator
```text
EIP712Domain(
    string name,
    string version,
    uint256 chainId,
    address verifyingContract
)
```
* **Binding:** The signature is cryptographically bound to the specific token contract and CAIP-2 chain ID, preventing cross-chain replays.

### 3.2 Authorization Struct
```solidity
struct TransferWithAuthorization {
    address from;
    address to;
    uint256 value;
    uint256 validAfter;
    uint256 validBefore;
    bytes32 nonce;
}
```
* **Payee Pinning (`to`):** Bound to the verified seller's payout address.
* **Bounded Lifetime (`validAfter` / `validBefore`):** Default validity window is restricted to $\le 5$ minutes. Expired authorizations cannot be submitted by rogue facilitators.
* **Entropy Nonce (`nonce`):** 32-byte cryptographically secure random value (`crypto.randomBytes(32)`). Once settled or marked used, any duplicate nonce is permanently rejected by smart contract state.

---

## 4. Circuit Breakers & Anomaly Detection

To protect funds against runaway agent loops:
1. **Burst Frequency Limit:** If an agent executes $> 20$ transactions within 60 seconds, the policy engine trips the circuit breaker.
2. **Consecutive Error Escalation:** If 5 consecutive payment attempts fail due to seller verification errors, the merchant domain is temporarily blacklisted.
3. **Auto-Freeze State:** When tripped, `ap_policy.is_frozen` is set to `TRUE`. All subsequent signing requests are blocked until an operator explicitly issues an unfreeze command via the control plane.
