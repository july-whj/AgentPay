# AgentPay Project Context Capsule (项目上下文胶囊)

> **Document Version:** v1.0.0  
> **Target Audience:** Antigravity AI Agent & Human Contributors in new project workspaces  
> **Usage:** Place this file in the root of the new project directory (`AgentPay/`), and prompt the AI Agent to read it to instantly restore 100% of the project's background, architecture, and roadmap.

---

## 1. Project Overview & Meta Information

* **Project Name:** AgentPay
* **GitHub Repository:** [https://github.com/july-whj/AgentPay](https://github.com/july-whj/AgentPay)
* **Tagline:** The Open-Source Payment Infrastructure & Financial Firewall for Autonomous AI Agents
* **Core Protocol:** x402 V2 (Standard HTTP 402 Payment Required protocol)
* **Polyglot Tech Stack:**
  * **TypeScript:** Reference implementation, Buyer Agent Client SDK, Express/Fastify Middleware, MCP Transport Interceptor.
  * **Python:** AI Agent Ecosystem SDK (`agentpay-python`), transparent HTTP interceptors (`httpx`, `requests`, `aiohttp`), LangChain / CrewAI / LlamaIndex tool wrappers, FastAPI middleware.
  * **Java / Spring Boot:** Enterprise API Provider SDK (`agentpay-spring-boot-starter`), declarative `@RequiresPayment` annotations, robust Facilitator services.
* **Database:** PostgreSQL (Transactional guarantees, atomic quota deduction, row-level locks).
* **Open Source License:** Apache-2.0
* **Deployment Model:** Self-hosted first (Non-custodial, developers own their private keys and database records).

---

## 2. Core Value Proposition & Differentiation

AgentPay is **NOT** a simple wrapper around the x402 SDK. It solves real-world engineering, financial, and distributed system problems when AI Agents interact with paid APIs:

1. **Independent Policy Engine (Financial Firewall):**
   * LLMs propose transactions, but they can **never** grant themselves permissions.
   * Enforces single-transaction caps, rolling daily budgets, domain/resource whitelists, and circuit breakers against infinite-loop spending.
2. **Decoupled Tri-State Machine:**
   * Strictly decouples `PaymentIntent`, `Settlement` (on-chain/protocol), and `Fulfillment` (API resource delivery).
   * RPC timeouts do **not** imply payment failure. Prevents double-spending and ensures graceful recovery if delivery fails after settlement.
3. **Cross-Language Parity (TS + Python + Java):**
   * Bridges modern AI agent frameworks (Python & TypeScript) with enterprise API providers (Java / Spring Boot).
   * Shared test vectors (`specs/conformance/test-vectors/`) ensure identical protocol behavior across languages.
4. **Gasless Micro-Settlement (EIP-712 / EIP-3009):**
   * The Agent generates off-chain EIP-712 signatures for compatible stablecoins (e.g., USDC `TransferWithAuthorization`).
   * Facilitators sponsor/settle gas on-chain. Agents never need to manage native gas tokens.

---

## 3. Architecture & Monorepo Structure

```text
agentpay/
├── specs/                           # Cross-language specifications & test vectors
│   ├── architecture.md
│   ├── domain-model.md
│   ├── payment-state-machine.md
│   ├── security-model.md
│   └── conformance/test-vectors/
│
├── packages/                        # TypeScript Monorepo (pnpm workspace)
│   ├── core/                        # Domain models, state machines, policy interfaces
│   ├── x402-v2/                     # x402 V2 protocol codec, challenge parser
│   ├── evm/                         # EIP-712, EIP-3009 signature builders
│   ├── wallet/                      # LocalSigner, KMS abstraction
│   ├── facilitator-client/          # HTTP client for Facilitator /verify & /settle
│   ├── policy/                      # Policy engine (budgets, whitelists, rate limits)
│   ├── express/                     # Express seller middleware
│   ├── fastify/                     # Fastify seller plugin
│   ├── mcp/                         # Model Context Protocol paid tool wrapper
│   ├── agent-client/                # Transparent HTTP interceptor for Buyer Agents
│   └── testing/                     # Shared test fixtures & mocks
│
├── python/                          # Python SDK (Poetry / Hatch / UV workspace)
│   ├── agentpay-core/               # Domain models, x402 V2 codecs & typing
│   ├── agentpay-client/             # Transparent httpx/requests/aiohttp 402 interceptor
│   ├── agentpay-policy/             # Python Financial Firewall & budget engine
│   ├── agentpay-fastapi/            # FastAPI provider middleware
│   └── agentpay-langchain/          # LangChain, CrewAI & LlamaIndex tool wrappers
│
├── java/                            # Java / Spring Boot (Gradle multi-project)
│   ├── agentpay-core/
│   ├── agentpay-x402/
│   ├── agentpay-evm/
│   ├── agentpay-facilitator/
│   ├── agentpay-policy/
│   ├── agentpay-spring-boot-starter/# Auto-configuration & @RequiresPayment annotation
│   ├── agentpay-mcp/
│   └── agentpay-testkit/
│
├── services/                        # Standalone Deployable Services
│   ├── facilitator/                 # Reference Facilitator service
│   ├── control-plane/               # Admin & audit dashboard API
│   └── reconciliation/              # Background recovery & reconciliation worker
│
├── examples/
│   ├── typescript-seller/           # Express/Fastify protected API example
│   ├── typescript-buyer/            # AgentPayClient calling protected API
│   ├── java-spring-seller/          # Spring Boot protected endpoint example
│   ├── mcp-paid-tool/               # Paid MCP tool demonstration
│   └── local-devnet/                # Local anvil/hardhat + mock Facilitator environment
│
├── infra/
│   ├── docker/                      # Docker compose for PostgreSQL & local devnet
│   └── postgres/                    # Init migrations and DDL
│
├── AGENTPAY_CONTEXT.md
├── README.md
├── LICENSE                          # Apache-2.0
├── SECURITY.md
└── CONTRIBUTING.md
```

---

## 4. Domain Model & PostgreSQL Design

### Key Tables
* `ap_merchant`: Merchant identity, payout wallet addresses, configuration.
* `ap_resource`: Paid API endpoints, tools, or digital assets.
* `ap_price_rule`: Price per call (amount, asset symbol, network ID, scheme).
* `ap_agent`: Agent identity, public key, status, assigned policy ID.
* `ap_policy`: Financial guardrails (single cap, daily budget, spent amount, whitelists, reset interval).
* `ap_payment_intent`: High-level business intent (amount, asset, payer, payee, status).
* `ap_payment_attempt`: Specific payment attempt with raw x402 payload reference.
* `ap_settlement`: Blockchain settlement state, tx hash, block number, confirmation count.
* `ap_fulfillment`: Business resource delivery state, recovery retry count, response cache.
* `ap_idempotency`: Unique idempotency key with request hash and cached response.
* `ap_audit_event`: Immutable security log (policy evaluations, state transitions, manual overrides).

### Critical Data Rules
* Amounts **MUST** be stored as minimum base units (e.g., 6 decimals integer for USDC: `1000000` = 1 USDC) or high-precision decimal strings. **Float is strictly prohibited**.
* Network IDs must follow CAIP-2 standards (e.g., `eip155:8453` for Base).
* Concurrency protection: Atomic quota deduction using `UPDATE ap_policy SET used_amount = used_amount + :amount WHERE id = :id AND (used_amount + :amount) <= limit_amount` with row-level locks.

---

## 5. State Machines & Fault Tolerance

### Tri-State Separation

```text
[Payment Intent]
CREATED -> AUTHORIZED -> VERIFIED -> [COMPLETED | REJECTED | EXPIRED | CANCELLED]

[Settlement]
NOT_STARTED -> SUBMITTED -> PENDING -> [CONFIRMED | FAILED | REORGED | UNKNOWN]

[Fulfillment]
NOT_STARTED -> PROCESSING -> [DELIVERED | FAILED | RECOVERY_REQUIRED]
```

### Recovery Principles
1. **RPC Timeout != Failure:** If blockchain RPC times out, the transaction must be marked `UNKNOWN`/`PENDING` and probed by the background worker. Never blindly re-submit.
2. **Settled but Failed to Deliver:** If settlement is `CONFIRMED` but business delivery times out, the fulfillment enters `RECOVERY_REQUIRED` and retries with cached response or issues business credit.
3. **Transparent Client Interception:** Buyer Agent client transparently catches `402 Payment Required`, checks policy, signs EIP-712, and retries with `PAYMENT-SIGNATURE`. If policy fails, it surfaces a typed `BudgetExceededException`.

---

## 6. Project Roadmap

### Phase 0: 14-Day Demand & Competitor Validation
* **Days 1–3:** Competitor analysis vs official x402 SDK, finalize EVM network (Base Sepolia) and test asset (USDC).
* **Days 4–7:** Minimal TypeScript End-to-End POC (Buyer + Seller + Mock Facilitator).
* **Days 8–11:** Validate Policy Engine (budget caps, idempotency, simulated crash recovery).
* **Days 12–14:** External developer preview and feedback collection.

### Phase 1: 8-Week Core Delivery
* **Week 1:** Specs, Monorepo scaffolding, test vectors, PostgreSQL DDL.
* **Week 2:** TypeScript Seller SDK (`@agentpay/express`, `@agentpay/fastify`).
* **Week 3:** TypeScript Buyer Agent SDK with transparent 402 interceptor.
* **Week 4:** Reference Facilitator & PostgreSQL state persistence.
* **Week 5:** Java SDK & `agentpay-spring-boot-starter`.
* **Week 6:** MCP Paid Tool implementation & demo.
* **Week 7:** Fault injection tests, conformance test suite, security audit docs.
* **Week 8:** Public release, Docker compose one-click deployment, documentation site.

---

## 7. Immediate Next Steps for AI Agent in New Workspace

When you open this project in a new workspace:

1. **Verify workspace:** Confirm you are in the cloned `AgentPay` repository.
2. **Step 1 - Scaffolding:**
   * Create `.gitignore` (Node, pnpm, Java, Gradle, IDEA, VSCode, .env).
   * Create root `package.json` and `pnpm-workspace.yaml`.
   * Create `LICENSE` (Apache-2.0) and `README.md`.
3. **Step 2 - Specs & Contracts:**
   * Create `specs/domain-model.md` and `specs/architecture.md`.
   * Provide the initial PostgreSQL schema migration in `infra/postgres/init.sql`.
4. **Step 3 - Minimal POC (Days 4–7):**
   * Build the standalone POC script demonstrating Buyer Agent handling HTTP 402, signing EIP-712, and Seller verifying payment.
