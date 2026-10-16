# ADR-001: Architectural Strategy for x402 V2 Protocol & Official SDK ("Reuse vs. Custom Implementation")

* **Status:** Accepted  
* **Date:** 2026-10-16  
* **Deciders:** AgentPay Core Architecture Working Group  
* **Task References:** 2026 Year-End Plan (Tasks T0.2, T3.1)

---

## 1. Context & Problem Statement

x402 is an open standard protocol for HTTP 402 Payment Required micro-payments (maintained by the x402 Foundation). In the current 2026 ecosystem:

1. **Protocol Specification State:** The x402 V2 specification defines CAIP-2 chain identifiers, EIP-712/3009 transfer authorizations, and structured `PAYMENT-REQUIRED` and `PAYMENT-SIGNATURE` headers.
2. **Official Python SDK State:** A baseline `x402` package exists on PyPI (with optional extras such as `x402[httpx]` and `x402[requests]`), offering basic challenge extraction and raw signing bindings.
3. **AgentPay Core Differentiation:**
   * The official SDK functions as a **Raw Transport Wrapper**: whenever it encounters an HTTP 402, it signs and pays unconditionally.
   * AgentPay is designed as an **Autonomous Financial Firewall & Fault-Tolerant Infrastructure Layer**: its primary value proposition lies in **deterministic policy evaluation (per-tx caps, rolling budgets, whitelists, loop circuit breakers, and two-phase atomic reservations)** and a **decoupled tri-state recovery engine**.

---

## 2. Options Considered

| Evaluation Dimension | Option A: Hard Dependency on Official SDK (`x402`) | Option B: Full Scratch Implementation from Zero | Option C (Recommended): Layered Adaptation with Custom Firewall Core |
| :--- | :--- | :--- | :--- |
| **Financial Firewall Integration** | Difficult. Official interceptor lacks hooks for two-phase reservation (`reserve -> commit -> release`). | Complete autonomy; interceptor seamlessly coordinates with policy engine. | **Excellent.** Full lifecycle interceptor with exact protocol format alignment. |
| **Dependency Footprint** | Heavy. Transitive web3 and cryptography version conflicts. | Minimal (`httpx`, `pydantic`, `eth-account`). | **Minimal.** Core package has zero extraneous dependencies; official SDK is purely optional. |
| **Specification Upstream Parity** | Automatically inherits upstream patches. | Manual alignment required if protocol headers change. | **High.** Standard Pydantic schema validated against upstream test vectors. |
| **Implementation Risk & Velocity** | Low initial velocity; high risk when retrofitting firewall state machine. | Moderate (approx. 200 lines of Pydantic codecs). | **Optimal (approx. 3 hours for clean schema; zero black-box debugging).** |

---

## 3. Decision Outcome

We decided to adopt **Option C (Layered Adaptation Architecture)**:

### 3.1 Core Protocol Codec (`agentpay-core`) — Clean Pydantic V2 Models
* We do **not** take a hard dependency on the third-party `x402` PyPI package.
* We define `PaymentChallenge` and `PaymentAuthorization` models natively using **Pydantic V2**, adhering strictly to the x402 V2 standard.
* **100% Byte-Level Parity:** Serialization and deserialization guarantees are validated against standard test vectors (`specs/conformance/test-vectors/`).
* **Benefit:** Eliminates dependency hell, removes runtime black boxes, and guarantees optimal test execution speed and transparent debugging.

### 3.2 Network Interceptor & Signing Lifecycle (`agentpay-client`) — Custom Orchestration
* We implement an autonomous `httpx` transport interceptor.
* The client strictly orchestrates the full financial lifecycle:
  $$\text{Intercept 402} \longrightarrow \text{Policy Evaluate \& Reserve} \longrightarrow \text{Sign Authorization} \longrightarrow \text{Replay with PAYMENT-SIGNATURE} \longrightarrow \text{Commit or Release}$$

### 3.3 Upstream Interoperability (`x402-interop`) — Optional Extension
* Provide helper utilities to convert between AgentPay native challenge/authorization objects and official `x402` library objects, ensuring frictionless interoperability if users choose to install both.

---

## 4. Consequences & Impact

* **Positive Consequences:**
  * Task T3.1 implementation workload remains strictly bounded ($\le 3$ hours).
  * Two-phase reservation (`reserve -> commit -> release`) hooks directly into the HTTP transport lifecycle without hacks.
  * The Python SDK remains ultra-lightweight with fast cold-start times.
* **Risks & Mitigation:**
  * **Risk:** Upstream x402 V2 specification introduces breaking schema modifications.
  * **Mitigation:** Continuous automated conformance testing in GitHub Actions using cross-language JSON test vectors ensures early detection of schema drift.
