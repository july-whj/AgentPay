# Contributing to AgentPay

Thank you for your interest in contributing to **AgentPay**! We welcome contributions across our TypeScript packages, Java Spring Boot modules, specifications, and documentation.

---

## 1. Code of Conduct

We are committed to providing a welcoming, inclusive, and harassment-free experience for everyone. Please treat fellow contributors with respect.

---

## 2. Development Setup

AgentPay is a multi-language Monorepo:
* **Node.js**: >= 20.x with [pnpm](https://pnpm.io/) >= 9.x
* **Java**: OpenJDK >= 21 with Gradle >= 8.5
* **Docker**: Docker Desktop or Docker Engine (for PostgreSQL and devnet)

### TypeScript Workspace
```bash
# Install root and workspace dependencies
pnpm install

# Build all packages
pnpm build

# Run unit tests
pnpm test
```

### Java / Spring Boot Workspace
```bash
cd java
./gradlew build
./gradlew test
```

---

## 3. Pull Request Guidelines

1. **Branch Naming**: Use descriptive prefixes:
   * `feat/feature-name`
   * `fix/bug-fix`
   * `docs/documentation-update`
   * `spec/spec-change`
2. **Commit Messages**: Follow [Conventional Commits](https://www.conventionalcommits.org/):
   * `feat(policy): add rolling 24-hour window reset logic`
   * `fix(evm): handle zero-padded nonces in EIP-3009`
3. **Cross-Language Parity**: If modifying protocol behavior or serialization, update `specs/conformance/test-vectors/` and ensure both TypeScript and Java test suites pass.
4. **Code Quality**: All PRs must pass linting, typechecking, and test coverage thresholds.
