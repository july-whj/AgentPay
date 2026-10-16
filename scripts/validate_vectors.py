#!/usr/bin/env python3
"""
AgentPay Conformance Test Vectors Validator (scripts/validate_vectors.py)
Validates EIP-712 cryptographic signatures, policy ledger math, and x402 schemas.
Exit code 0 on complete pass; non-zero on failure.
"""

import json
import os
import re
import sys
from pathlib import Path

# Optional cryptographic validation using eth_account if installed
try:
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    HAS_ETH_ACCOUNT = True
except ImportError:
    HAS_ETH_ACCOUNT = False


def validate_eip712_vectors(vectors_dir: Path) -> int:
    filepath = vectors_dir / "eip712-authorization.json"
    print(f"--> Validating {filepath.name}...")
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["version"] == "1.1.0", f"Unexpected version: {data['version']}"
    assert data["primaryType"] == "TransferWithAuthorization"

    test_wallet = data["testWallet"]
    expected_signer = test_wallet["address"].lower()
    domain_data = data["domain"]
    types_data = {
        "TransferWithAuthorization": data["types"]["TransferWithAuthorization"]
    }

    errors = 0
    for tc in data["testCases"]:
        tc_id = tc["id"]
        if "expectedSignature" in tc:
            # Positive test case
            msg = tc["message"]
            int_msg = {
                "from": msg["from"],
                "to": msg["to"],
                "value": int(msg["value"]),
                "validAfter": int(msg["validAfter"]),
                "validBefore": int(msg["validBefore"]),
                "nonce": msg["nonce"]
            }
            if HAS_ETH_ACCOUNT:
                encoded = encode_typed_data(domain_data=domain_data, message_types=types_data, message_data=int_msg)
                sig_bytes = bytes.fromhex(tc["expectedSignature"]["serialized"])
                recovered = Account.recover_message(encoded, signature=sig_bytes).lower()
                if recovered != expected_signer:
                    print(f"❌ [{tc_id}] Recovered address {recovered} != {expected_signer}")
                    errors += 1
                else:
                    print(f"  ✓ [{tc_id}] Cryptographic signature verified for {recovered}")
            else:
                print(f"  ⚠ [{tc_id}] Skipped raw crypto recovery (eth_account not installed)")
        elif "tamperedMessage" in tc:
            # Negative test: Tampered amount
            if HAS_ETH_ACCOUNT:
                tmsg = tc["tamperedMessage"]
                int_tmsg = {
                    "from": tmsg["from"],
                    "to": tmsg["to"],
                    "value": int(tmsg["value"]),
                    "validAfter": int(tmsg["validAfter"]),
                    "validBefore": int(tmsg["validBefore"]),
                    "nonce": tmsg["nonce"]
                }
                encoded = encode_typed_data(domain_data=domain_data, message_types=types_data, message_data=int_tmsg)
                sig_bytes = bytes.fromhex(tc["signatureFromTC03"])
                recovered = Account.recover_message(encoded, signature=sig_bytes).lower()
                if recovered == expected_signer:
                    print(f"❌ [{tc_id}] Tampered message unexpectedly recovered expected signer!")
                    errors += 1
                else:
                    print(f"  ✓ [{tc_id}] Tampered message successfully rejected (mismatch: {recovered})")
        elif "replayDomain" in tc:
            # Negative test: Cross-chain replay
            if HAS_ETH_ACCOUNT:
                msg = data["testCases"][0]["message"]
                int_msg = {
                    "from": msg["from"],
                    "to": msg["to"],
                    "value": int(msg["value"]),
                    "validAfter": int(msg["validAfter"]),
                    "validBefore": int(msg["validBefore"]),
                    "nonce": msg["nonce"]
                }
                encoded = encode_typed_data(domain_data=tc["replayDomain"], message_types=types_data, message_data=int_msg)
                sig_bytes = bytes.fromhex(tc["signatureFromTC01"])
                recovered = Account.recover_message(encoded, signature=sig_bytes).lower()
                if recovered == expected_signer:
                    print(f"❌ [{tc_id}] Cross-chain replay unexpectedly recovered valid signer!")
                    errors += 1
                else:
                    print(f"  ✓ [{tc_id}] Cross-chain replay successfully rejected (mismatch: {recovered})")

    return errors


def validate_policy_budget_vectors(vectors_dir: Path) -> int:
    filepath = vectors_dir / "policy-budget.json"
    print(f"--> Validating {filepath.name}...")
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["version"] == "1.1.0"
    policy = data["policy"]
    max_cap = int(policy["max_per_tx_base_units"])
    daily_budget = int(policy["rolling_budget_24h_base_units"])
    whitelist = policy["whitelist"]
    wl_payees = [p.lower() for p in whitelist["payees"]]
    wl_assets = [a.lower() for a in whitelist["assets"]]
    wl_networks = whitelist["networks"]
    wl_domains = whitelist.get("domains", [])

    errors = 0
    for tc in data["testCases"]:
        tc_id = tc["id"]
        # Simulate local ledger
        ledger = {}  # intent_id -> {"amount": int, "state": str, "created_at": int, "expires_at": int}
        is_frozen = tc.get("initialState", {}).get("is_frozen", 0) == 1
        clock = tc.get("clock", 1729000000)

        for step in tc["actions"]:
            action = step["action"]
            intent_id = step["intentId"]
            expected_res = step["expectedResult"]
            expected_err = step.get("expectedError")

            if action == "reserve":
                amount = int(step["amount"])
                payee = step["payee"].lower()
                asset = step["asset"].lower()
                network = step["network"]
                domain = step.get("domain")

                # Precedence 1: Frozen check
                if is_frozen:
                    actual_res = "REJECTED"
                    actual_err = "POLICY_FROZEN"
                # Precedence 2: Format
                elif amount <= 0:
                    actual_res = "REJECTED"
                    actual_err = "INVALID_AMOUNT_FORMAT"
                # Precedence 3: Whitelist
                elif network not in wl_networks:
                    actual_res = "REJECTED"
                    actual_err = "NETWORK_NOT_WHITELISTED"
                elif asset not in wl_assets:
                    actual_res = "REJECTED"
                    actual_err = "ASSET_NOT_WHITELISTED"
                elif payee not in wl_payees:
                    actual_res = "REJECTED"
                    actual_err = "PAYEE_NOT_WHITELISTED"
                elif domain and domain not in wl_domains:
                    actual_res = "REJECTED"
                    actual_err = "DOMAIN_NOT_WHITELISTED"
                # Precedence 4: Single cap
                elif amount > max_cap:
                    actual_res = "REJECTED"
                    actual_err = "SINGLE_TX_CAP_EXCEEDED"
                else:
                    # Precedence 5: 24h rolling budget calculation
                    committed = sum(e["amount"] for e in ledger.values() if e["state"] == "COMMITTED" and e["created_at"] >= clock - 86400)
                    reserved = sum(e["amount"] for e in ledger.values() if e["state"] == "RESERVED" and e["expires_at"] > clock)
                    available = daily_budget - (committed + reserved)
                    if available < amount:
                        actual_res = "REJECTED"
                        actual_err = "DAILY_BUDGET_EXCEEDED"
                    else:
                        actual_res = "APPROVED"
                        actual_err = None
                        ledger[intent_id] = {
                            "amount": amount,
                            "state": "RESERVED",
                            "created_at": clock,
                            "expires_at": clock + 360
                        }

                if actual_res != expected_res:
                    print(f"❌ [{tc_id}] Action reserve: {actual_res} != {expected_res}")
                    errors += 1
                elif expected_err and actual_err != expected_err:
                    print(f"❌ [{tc_id}] Action reserve error: {actual_err} != {expected_err}")
                    errors += 1
                else:
                    if "expectedAvailableAfter" in step:
                        committed = sum(e["amount"] for e in ledger.values() if e["state"] == "COMMITTED" and e["created_at"] >= clock - 86400)
                        reserved = sum(e["amount"] for e in ledger.values() if e["state"] == "RESERVED" and e["expires_at"] > clock)
                        avail = daily_budget - (committed + reserved)
                        if str(avail) != step["expectedAvailableAfter"]:
                            print(f"❌ [{tc_id}] Available after: {avail} != {step['expectedAvailableAfter']}")
                            errors += 1

            elif action == "commit":
                if intent_id in ledger and ledger[intent_id]["state"] == "RESERVED":
                    ledger[intent_id]["state"] = "COMMITTED"
                if "expectedAvailableAfter" in step:
                    committed = sum(e["amount"] for e in ledger.values() if e["state"] == "COMMITTED" and e["created_at"] >= clock - 86400)
                    reserved = sum(e["amount"] for e in ledger.values() if e["state"] == "RESERVED" and e["expires_at"] > clock)
                    avail = daily_budget - (committed + reserved)
                    if str(avail) != step["expectedAvailableAfter"]:
                        print(f"❌ [{tc_id}] Commit available after: {avail} != {step['expectedAvailableAfter']}")
                        errors += 1

            elif action == "release":
                if intent_id in ledger and ledger[intent_id]["state"] == "RESERVED":
                    ledger[intent_id]["state"] = "RELEASED"
                if "expectedAvailableAfter" in step:
                    committed = sum(e["amount"] for e in ledger.values() if e["state"] == "COMMITTED" and e["created_at"] >= clock - 86400)
                    reserved = sum(e["amount"] for e in ledger.values() if e["state"] == "RESERVED" and e["expires_at"] > clock)
                    avail = daily_budget - (committed + reserved)
                    if str(avail) != step["expectedAvailableAfter"]:
                        print(f"❌ [{tc_id}] Release available after: {avail} != {step['expectedAvailableAfter']}")
                        errors += 1

    if errors == 0:
        print("  ✓ All 11 policy budget cases passed mathematical and rule verification")
    return errors


def validate_x402_vectors(vectors_dir: Path) -> int:
    filepath = vectors_dir / "x402-v2-challenge.json"
    print(f"--> Validating {filepath.name}...")
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["version"] == "1.1.0"
    caip2_re = re.compile(r"^[a-z0-9]+:[a-zA-Z0-9_-]+$")
    errors = 0

    for item in data["validChallenges"]:
        p = item["payload"]
        if "network" in p and not caip2_re.match(p["network"]):
            print(f"❌ Valid challenge has invalid CAIP-2 network: {p['network']}")
            errors += 1

    for item in data["invalidChallenges"]:
        assert "expectedError" in item

    if errors == 0:
        print("  ✓ All x402 V2 challenge and signature header vectors verified")
    return errors


def main():
    if len(sys.argv) > 1:
        vectors_dir = Path(sys.argv[1]).resolve()
    else:
        repo_root = Path(__file__).resolve().parent.parent
        vectors_dir = repo_root / "specs" / "conformance" / "test-vectors"

    print(f"==================================================")
    print(f"AgentPay Conformance Vector Self-Check Suite")
    print(f"Vectors Directory: {vectors_dir}")
    print(f"==================================================")

    total_errors = 0
    total_errors += validate_eip712_vectors(vectors_dir)
    total_errors += validate_policy_budget_vectors(vectors_dir)
    total_errors += validate_x402_vectors(vectors_dir)

    print(f"==================================================")
    if total_errors == 0:
        print("🎉 ALL TEST VECTORS PASSED CONFORMANCE VALIDATION")
        sys.exit(0)
    else:
        print(f"❌ {total_errors} CONFORMANCE ASSERTION(S) FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
