# ADR-001: x402 V2 协议与官方 SDK “复用 vs 自研” 架构决策

* **状态 (Status)**: 已敲定 (Accepted)
* **日期 (Date)**: 2026-10-16
* **决策人 (Deciders)**: AgentPay 核心架构组
* **关联任务**: 2026 研发计划 T0.2 / T3.1

---

## 1. 背景与调研现状 (Context)

x402 是针对 HTTP 402 Payment Required 的开放标准协议（由 x402-foundation 维护）。在 2026 年的现状中：
1. **官方规范现状**：x402 V2 规范已固化基于 CAIP-2 链标识、EIP-712/3009 授权签名、以及结构化 `PAYMENT-REQUIRED` 与 `PAYMENT-SIGNATURE` 响应/请求头。
2. **官方 Python SDK 现状**：PyPI 上存在 `x402` 基础包（支持 `x402[httpx]`、`x402[requests]` 等），提供了基础的挑战提取和原生签名器绑定。
3. **AgentPay 的定位差距**：
   * 官方 SDK 定位为**“基础协议传输层（Raw Transport Wrapper）”**：只要遇到 402，就无条件签名并支付。
   * AgentPay 定位为**“智能体财务防火墙与容错基础设施（Financial Firewall & Reliability Layer）”**：核心价值在于 LLM 提议后的**刚性策略拦截（单笔上限、24h 预算池、白名单、死循环熔断、两阶段原子预留）**，以及**三态解耦恢复状态机**。

---

## 2. 决策维度对比 (Options Considered)

| 评估维度 | 选项 A：深度绑定官方 SDK (`x402` 包) | 选项 B：完全从零自研所有编解码与网络层 | 选项 C（推荐）：分层适配与自研防火墙核心 |
| :--- | :--- | :--- | :--- |
| **策略防火墙接入** | 困难。官方拦截器无两阶段预留切面，需 hack 私有属性。 | 完全自主可控，拦截与预留契合度极高。 | **优秀**。拦截器与策略引擎自研，底层协议格式对齐。 |
| **外部依赖负担** | 较重。引入官方可能携带的特定 web3 库版本冲突。 | 极轻（仅需 `httpx`, `pydantic`, `eth-account`）。 | **极轻**。核心包零多余依赖，可选依赖官方库。 |
| **协议演进兼容性** | 自动跟随官方最新补丁。 | 若官方协议头调整，需手动同步维护。 | **高**。自研 Pydantic Schema 严格遵守 V2 规范，加装转换器。 |
| **工时与进度风险** | 低（前期），高（后期定制防火墙与状态机时）。 | 适中（需编写约 200 行 Pydantic 编解码代码）。 | **最佳（约 3h 搞定纯净编解码，无调试黑盒）**。 |

---

## 3. 架构决策结论 (Decision)

我们决定采用 **选项 C（分层适配架构）**：

1. **核心协议编解码层 (`agentpay-core`) —— 采用 Pydantic 自研轻量实现**：
   * 不把第三方 `x402` 作为核心强依赖项，而是基于 x402 V2 规范直接用 `Pydantic V2` 定义 `PaymentChallenge` 与 `PaymentAuthorization` 模型。
   * 严格保证序列化/反序列化与官方规范 100% 字节级一致（由 `specs/conformance/test-vectors/` 保障）。
   * 收益：杜绝底层依赖地狱，消除黑盒，保证极佳的单测性能与排错透明度。

2. **网络拦截与签名生命周期 (`agentpay-client`) —— 深度自研**：
   * 自主实现基于 `httpx` 的 `AgentPayClient` 拦截器。
   * 拦截器显式编排：`捕获 402 -> 策略引擎 evaluate & reserve -> LocalSigner 签名 -> 重放请求 -> commit/release` 闭环。

3. **官方互操作性扩展 (`x402-interop`) —— 声明为可选扩展 (Optional)**：
   * 提供工具函数支持将 AgentPay Challenge 对象与官方 `x402` 库对象互转，方便用户平滑迁移已有项目。

---

## 4. 后续影响 (Consequences)

* **积极影响**：
  * T3.1 工作量完全收敛在可控范围（仅需 Pydantic schema 与 Base64URL 工具函数，耗时 $\le$ 3 小时）。
  * 策略引擎的两阶段预留机制（Reserve -> Commit -> Release）可以无缝、干净地注入到请求切面中。
  * 整个 Python SDK 的体积将极其轻量，冷启动开销极小。
* **风险与对策**：
  * 风险：x402 V2 官方未来若发布字段破坏性更新。
  * 对策：通过 GitHub Actions 定期跑一致性测试向量校验，确保与上游规范同步。
