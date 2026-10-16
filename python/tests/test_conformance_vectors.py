import json
import os
import re
import unittest
from pathlib import Path


class TestConformanceVectors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Locate specs/conformance/test-vectors relative to repo root
        current_file = Path(__file__).resolve()
        repo_root = current_file.parent.parent.parent
        cls.vectors_dir = repo_root / "specs" / "conformance" / "test-vectors"
        assert cls.vectors_dir.exists(), f"Vectors directory not found at {cls.vectors_dir}"

    def _load_json(self, filename: str) -> dict:
        filepath = self.vectors_dir / filename
        self.assertTrue(filepath.exists(), f"Vector file {filename} does not exist")
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_policy_budget_vectors(self):
        """Validates policy budget test vectors integrity and mathematical invariants."""
        data = self._load_json("policy-budget.json")
        self.assertEqual(data["version"], "1.0.0")
        self.assertEqual(data["suite"], "policy-budget-conformance")

        policy = data["policy"]
        max_cap = int(policy["maxPerTxBaseUnits"])
        rolling_budget = int(policy["rollingBudget24hBaseUnits"])
        self.assertGreater(max_cap, 0)
        self.assertGreater(rolling_budget, max_cap)

        test_cases = data["testCases"]
        self.assertGreaterEqual(len(test_cases), 5)

        for tc in test_cases:
            self.assertIn("id", tc)
            self.assertIn("actions", tc)
            for action in tc["actions"]:
                self.assertIn("action", action)
                self.assertIn("expectedResult", action)
                if action["action"] == "reserve" and "amount" in action:
                    amount = int(action["amount"])
                    self.assertGreater(amount, 0)
                    if action["expectedResult"] == "REJECTED" and "expectedError" in action:
                        if action["expectedError"] == "SingleTxCapExceededException":
                            self.assertGreater(amount, max_cap)

    def test_eip712_authorization_vectors(self):
        """Validates EIP-712 structured data and domain separator schema."""
        data = self._load_json("eip712-authorization.json")
        self.assertEqual(data["version"], "1.0.0")
        self.assertEqual(data["suite"], "eip712-authorization-conformance")

        domain = data["domain"]
        self.assertEqual(domain["name"], "USD Coin")
        self.assertEqual(domain["chainId"], 84532)
        self.assertTrue(domain["verifyingContract"].startswith("0x"))

        types = data["types"]
        self.assertIn("EIP712Domain", types)
        self.assertIn("TransferWithAuthorization", types)

        test_cases = data["testCases"]
        self.assertGreaterEqual(len(test_cases), 2)
        for tc in test_cases:
            msg = tc["message"]
            self.assertTrue(msg["from"].startswith("0x"))
            self.assertTrue(msg["to"].startswith("0x"))
            self.assertTrue(msg["nonce"].startswith("0x"))
            self.assertEqual(len(msg["nonce"]), 66)  # 32 bytes hex with 0x prefix
            self.assertGreater(msg["validBefore"], msg["validAfter"])

    def test_x402_v2_challenge_vectors(self):
        """Validates x402 challenge schema and CAIP-2 regex constraints."""
        data = self._load_json("x402-v2-challenge.json")
        self.assertEqual(data["version"], "1.0.0")
        self.assertEqual(data["suite"], "x402-v2-challenge-conformance")

        caip2_pattern = re.compile(r"^[a-z0-9]+:[a-zA-Z0-9_-]+$")

        # Check valid challenges
        valid_challenges = data["validChallenges"]
        self.assertGreaterEqual(len(valid_challenges), 2)
        for item in valid_challenges:
            payload = item["payload"]
            self.assertEqual(payload["version"], "2.0")
            if "network" in payload:
                self.assertTrue(
                    caip2_pattern.match(payload["network"]),
                    f"Network '{payload['network']}' does not conform to CAIP-2",
                )

        # Check invalid challenges
        invalid_challenges = data["invalidChallenges"]
        self.assertGreaterEqual(len(invalid_challenges), 2)
        for item in invalid_challenges:
            self.assertIn("expectedError", item)


if __name__ == "__main__":
    unittest.main()
