# AgentPay Cross-Language Conformance Test Vectors

This directory contains standardized, language-agnostic JSON test vectors used to guarantee 100% cryptographic and protocol parity between:
* **TypeScript Reference Implementation** (`packages/`)
* **Python AI Ecosystem Implementation** (`python/`)
* **Java / Spring Boot Provider Implementation** (`java/`)

---

## 1. Directory Structure

```text
specs/conformance/test-vectors/
├── README.md
├── eip712-authorization.json     # EIP-712 & EIP-3009 structured hash and signature vectors
├── x402-v2-challenge.json        # Header serialization, challenge encoding, and decoding vectors
└── policy-budget.json            # Base units math, integer overflow, and window deduction vectors
```

---

## 2. Test Vector Categories

### 2.1 `eip712-authorization.json`
* **Purpose:** Validates that Viem (TypeScript), `eth-account` (Python), and Web3j (Java) generate and verify identical struct hashes (`hashStruct`), domain separators, and recover the exact same signer address from raw (v, r, s) tuples.
* **Key Fields:**
  * `domain`: Token name, version, chain ID, contract address.
  * `message`: `from`, `to`, `value`, `validAfter`, `validBefore`, `nonce`.
  * `expectedStructHash`: Hex-encoded 32-byte hash.
  * `expectedSigner`: Checksummed EVM address.

### 2.2 `x402-v2-challenge.json`
* **Purpose:** Validates consistent parsing and generation of the HTTP `PAYMENT-REQUIRED` and `PAYMENT-SIGNATURE` headers.
* **Key Fields:**
  * `rawHeader`: HTTP header string representation.
  * `parsedPayload`: Normalized JSON object.
  * `edgeCases`: Malformed base64, missing required parameters, unsupported schemes.

### 2.3 `policy-budget.json`
* **Purpose:** Ensures identical 64-bit/BigInteger arithmetic across runtimes without precision degradation or floating-point truncation.
* **Key Fields:**
  * `initialBudget`: High-precision base unit string.
  * `deductions`: Sequence of transaction amounts.
  * `expectedRemaining`: Expected balance.
  * `shouldReject`: Boolean indicating if budget overflow should trigger rejection.
