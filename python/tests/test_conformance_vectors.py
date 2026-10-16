import sys
import unittest
from pathlib import Path

# Add repo root to sys.path to import scripts.validate_vectors
repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(repo_root))

from scripts.validate_vectors import (
    validate_eip712_vectors,
    validate_policy_budget_vectors,
    validate_x402_vectors,
)


class TestConformanceVectors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vectors_dir = repo_root / "specs" / "conformance" / "test-vectors"
        assert cls.vectors_dir.exists(), f"Vectors directory not found at {cls.vectors_dir}"

    def test_eip712_authorization_vectors(self):
        """Validates EIP-712 structured data and cryptographic signatures."""
        errors = validate_eip712_vectors(self.vectors_dir)
        self.assertEqual(errors, 0, f"EIP-712 conformance vectors had {errors} error(s)")

    def test_policy_budget_vectors(self):
        """Validates policy budget test vectors integrity and mathematical invariants."""
        errors = validate_policy_budget_vectors(self.vectors_dir)
        self.assertEqual(errors, 0, f"Policy budget conformance vectors had {errors} error(s)")

    def test_x402_v2_challenge_vectors(self):
        """Validates x402 challenge schema and CAIP-2 regex constraints."""
        errors = validate_x402_vectors(self.vectors_dir)
        self.assertEqual(errors, 0, f"x402 challenge conformance vectors had {errors} error(s)")


if __name__ == "__main__":
    unittest.main()
