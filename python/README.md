# AgentPay Python SDK (`agentpay`)

Official Python client and provider SDK for **AgentPay**, the open-source payment infrastructure and financial firewall for autonomous AI agents.

---

## 📦 Features

* **Transparent HTTP 402 Interceptor**: Seamlessly intercepts HTTP 402 responses using `httpx` or `requests`, triggers policy evaluation, signs an EIP-712/3009 authorization, and retries the request automatically.
* **Financial Firewall**: Independent, zero-trust policy engine enforcing per-transaction caps, rolling daily budgets, and merchant whitelists outside LLM context.
* **First-Class AI Ecosystem Integrations**: Native tool wrappers for **LangChain**, **CrewAI**, and **LlamaIndex**.
* **Provider Middleware**: Declarative `@requires_payment` decorators for **FastAPI**.

---

## 🚀 Quickstart (Coming in Phase 1)

```bash
pip install "agentpay[all]"
```
