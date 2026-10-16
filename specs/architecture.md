# AgentPay Architecture Specification

## 1. Overview & Architectural Goals

**AgentPay** is an open-source, non-custodial financial infrastructure and security layer for autonomous AI agents. It standardizes HTTP-based micro-payments over the **x402 V2** protocol and integrates an out-of-band **Financial Firewall (Policy Engine)** that prevents financial loss resulting from AI hallucinations, infinite loops, and unauthorized transactions.

### Key Tenets
1. **Zero-Trust for AI Autonomous Actions:** AI models propose transactions, but the cryptographic signing and payment execution occur strictly behind an independent, non-LLM policy boundary.
2. **Decoupled Asynchronous Lifecycle:** Separates high-level payment intent, blockchain consensus/settlement, and downstream service fulfillment to protect against network timeouts.
3. **Cross-Language Equivalence:** Full protocol, state machine, and cryptographic parity between TypeScript (pnpm monorepo) and Java / Spring Boot (Gradle multi-project).
4. **Gasless Micro-Settlements:** Employs EIP-712 / EIP-3009 signed authorizations. Buyer Agents require zero native gas tokens (ETH/MATIC/etc.), which are sponsored and settled by Facilitator nodes.

---

## 2. System Topology & Core Components

```mermaid
graph TB
    subgraph Buyer Ecosystem [Buyer Domain]
        Agent[Autonomous AI Agent / LLM]
        Client[AgentPay Client Interceptor]
        PolicyEngine[Financial Firewall / Policy Engine]
        KeyStore[Wallet / KMS Signer]
        
        Agent -->|HTTP Request| Client
        Client -->|Evaluate Tx Limit & Budget| PolicyEngine
        PolicyEngine -->|Approved & Quota Reserved| Client
        Client -->|Request Signature| KeyStore
        KeyStore -->|EIP-712 Signature| Client
    end

    subgraph Seller Ecosystem [Seller / Provider Domain]
        Gateway[API Gateway / Seller Middleware]
        SellerDB[(Seller PostgreSQL)]
        ResourceService[Core Business API / Resource]
        
        Gateway -->|Verify Idempotency & Claims| SellerDB
        Gateway -->|Dispatch Upon Verification| ResourceService
    end

    subgraph Infrastructure [Settlement & Facilitation]
        Facilitator[Facilitator Service]
        FacilitatorDB[(Facilitator PostgreSQL)]
        ReconciliationWorker[Reconciliation & Recovery Worker]
        Chain[(EVM Blockchain: Base / Ethereum)]
        
        Facilitator -->|Manage Nonces & Tx Queue| FacilitatorDB
        Facilitator -->|Relay Batch Settlements| Chain
        ReconciliationWorker -->|Probe Pending Txs & Re-orgs| Chain
    end

    Client -->|1. Initial Request| Gateway
    Gateway -->|2. HTTP 402 + Challenge| Client
    Client -->|3. Request + PAYMENT-SIGNATURE| Gateway
    Gateway -->|4. Verify Signature & Nonce| Facilitator
    Facilitator -->|5. Signature OK| Gateway
    Gateway -->|6. Paid Resource 200 OK| Client
    Gateway -->|7. Submit Settlement| Facilitator
```

### Component Roles & Responsibilities

| Component | Responsibility |
| :--- | :--- |
| **Buyer Agent** | High-level agent runtime or script initiating HTTP calls to tools, APIs, and data sources. |
| **AgentPay Client Interceptor** | Transparent HTTP client wrapper that detects `402 Payment Required`, triggers policy evaluation, collects EIP-712 signatures, and replays requests. |
| **Policy Engine (Firewall)** | Standalone security engine enforcing rate limits, per-transaction caps, daily rolling budgets, and destination whitelists. |
| **Seller Middleware** | Express / Fastify (TS) or Spring Boot Starter (Java) intercepting incoming requests, generating x402 challenges, and coordinating verification with Facilitators. |
| **Facilitator Service** | High-performance gateway that validates off-chain EIP-712/3009 signatures, sponsors gas, and batches on-chain settlement transactions. |
| **Reconciliation Worker** | Asynchronous service that polls unconfirmed settlements, repairs state after network splits, and triggers automated recovery workflows. |

---

## 3. Protocol & Standards Baseline

1. **x402 V2 Protocol:**
   * **Challenge Header:** `PAYMENT-REQUIRED` with base64/JSON encoded challenge payload containing accepted payment schemes, supported networks, token contract addresses, price, and expiration timestamp.
   * **Authorization Header:** `PAYMENT-SIGNATURE` containing the payer address, nonce, expiration, signature type (`EIP-712` or `EIP-3009`), and raw cryptographic signature.
2. **CAIP-2 Chain Identifiers:**
   * Standardized chain namespaces (e.g., `eip155:8453` for Base Mainnet, `eip155:84532` for Base Sepolia).
3. **EIP-712 & EIP-3009 Authorization:**
   * Type-safe structured signing (`TransferWithAuthorization` / `ReceiveWithAuthorization`).
   * Eliminates the traditional ERC-20 `approve` + `transferFrom` two-step gas cost.
4. **Idempotency Standards:**
   * Every payment request carries an `Idempotency-Key` (UUIDv4) header and deterministic SHA-256 payload digest.

---

## 4. End-to-End Payment Flow

```mermaid
sequenceDiagram
    autonumber
    participant A as Buyer Agent
    participant C as AgentPay Client
    participant P as Policy Engine
    participant S as Seller API Middleware
    participant F as Facilitator
    participant B as EVM Blockchain

    A->>C: Call getResource(params)
    C->>S: GET /v1/data (Standard HTTP request)
    S-->>C: 402 Payment Required [x402 Challenge]
    
    C->>P: Check Policy(Price, Asset, Merchant, Chain)
    alt Policy Evaluation Fails
        P-->>C: REJECTED (BudgetExceeded / UnlistedMerchant)
        C-->>A: Throw PolicyViolationException
    else Policy Evaluation Succeeds
        P-->>C: APPROVED (Atomic Quota Reserved)
        C->>C: Sign EIP-712 TransferWithAuthorization
        C->>S: Retry GET /v1/data with PAYMENT-SIGNATURE
        S->>F: POST /v1/verify {challenge, signature}
        F-->>S: 200 OK {valid: true, payer: "0x..."}
        S->>S: Deliver Resource Payload
        S-->>C: 200 OK [Response Data + Payment Receipt]
        C->>P: Confirm Execution (Commit Quota)
        C-->>A: Return Response Data
        S-)F: POST /v1/settle {signature, intentId} (Async)
        F->>B: Broadcast Gas-Sponsored Settlement Tx
        B-->>F: Tx Included in Block
    end
```

---

## 5. Dual-Stack Parity Architecture

To provide an identical developer experience regardless of language:
* **TypeScript Stack (`packages/`):** Targeted at AI agents (Node.js, Bun, Browser, MCP servers) and modern JS microservices.
* **Java Stack (`java/`):** Targeted at enterprise SaaS providers, Spring Cloud gateways, and high-throughput corporate backends.
* **Shared Conformance Vectors (`specs/conformance/test-vectors/`):**
  * EIP-712 domain separator serialization.
  * x402 V2 challenge parser and signature encoder.
  * State machine transition edge cases.
  * Policy budget deduction math with 64-bit integer overflow protection.
