# Security Policy

## Reporting Security Issues

The AgentPay team takes the security of autonomous payment infrastructure seriously. Because AgentPay controls cryptographic keys and financial settlement, we appreciate your efforts to responsibly disclose any vulnerabilities.

### Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

### How to Report a Vulnerability

**Please DO NOT file public GitHub issues for security vulnerabilities.**

If you believe you have discovered a vulnerability related to:
* Private key exposure or signer isolation bypasses
* Policy Engine bypasses (budget limit circumvention, whitelist evasion)
* Cryptographic flaws in EIP-712 / EIP-3009 verification
* Double-spending or replay vulnerabilities in settlement workflows

Please report it privately to our security team via GitHub Security Advisories or by emailing:
* **security@agentpay.dev** (or contact the repository maintainers directly)

Please provide:
1. Detailed description of the vulnerability.
2. Reproduction steps or a minimal proof of concept (POC).
3. Potential impact on agents, merchants, or facilitators.

### Response Time

We strive to acknowledge receipt of security reports within **24 hours** and provide regular status updates as we investigate and remediate the issue.
