import unittest

import agentpay


class TestSmoke(unittest.TestCase):
    def test_agentpay_version(self):
        """Smoke test ensuring agentpay module can be imported and version is defined."""
        self.assertEqual(agentpay.__version__, "0.1.0")


if __name__ == "__main__":
    unittest.main()
