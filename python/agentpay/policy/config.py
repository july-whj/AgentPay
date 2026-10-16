"""
Policy configuration loading and validation.

Supports loading from:
  1. Native dict with snake_case field names (``PolicyConfig`` constructor format).
  2. JSON test-vector format (camelCase + nested ``whitelist`` object + string amounts).

Future: YAML file loading will be added when gateway config lands (T5.1).
"""

from __future__ import annotations

import time
from typing import Any

from agentpay.policy.models import PolicyConfig


def _normalise_vector_format(data: dict[str, Any]) -> dict[str, Any]:
    """
    Convert the JSON test-vector ``policy`` object format into the native
    ``PolicyConfig`` constructor format.

    Handles:
      - ``policyId`` → ``id``
      - ``walletAddress`` → ``wallet_address``
      - Nested ``whitelist`` dict → flat ``whitelist_*`` fields
      - String amounts → int
    """
    result: dict[str, Any] = {}

    # ID mapping
    if "policyId" in data:
        result["id"] = data["policyId"]
    elif "id" in data:
        result["id"] = data["id"]

    # Name
    if "name" in data:
        result["name"] = data["name"]

    # Wallet address
    if "walletAddress" in data:
        result["wallet_address"] = data["walletAddress"]
    elif "wallet_address" in data:
        result["wallet_address"] = data["wallet_address"]

    # Budget limits — pass through; the Pydantic model handles str→int
    for field in (
        "max_per_tx_base_units",
        "rolling_budget_24h_base_units",
        "lifetime_budget_base_units",
    ):
        if field in data:
            result[field] = data[field]

    # Nested whitelist object → flat fields
    if "whitelist" in data and isinstance(data["whitelist"], dict):
        wl = data["whitelist"]
        result["whitelist_payees"] = wl.get("payees", [])
        result["whitelist_assets"] = wl.get("assets", [])
        result["whitelist_networks"] = wl.get("networks", [])
        result["whitelist_domains"] = wl.get("domains", [])
    else:
        # Already flat format — pass through
        for field in (
            "whitelist_payees",
            "whitelist_assets",
            "whitelist_networks",
            "whitelist_domains",
        ):
            if field in data:
                result[field] = data[field]

    # Circuit breaker config
    for field in (
        "circuit_max_rpm",
        "circuit_consecutive_failures",
        "circuit_cooldown_seconds",
        "breaker_state",
        "tripped_at",
        "is_frozen",
    ):
        if field in data:
            result[field] = data[field]

    # Timestamps — default to now if absent
    now = int(time.time())
    result["created_at"] = data.get("created_at", now)
    result["updated_at"] = data.get("updated_at", now)

    return result


def load_policy_config(data: dict[str, Any]) -> PolicyConfig:
    """
    Load and validate a policy configuration from a dict.

    Accepts both native snake_case format and the camelCase JSON test-vector
    format used in ``specs/conformance/test-vectors/policy-budget.json``.

    Args:
        data: Configuration dictionary.

    Returns:
        A validated, frozen ``PolicyConfig`` instance.

    Raises:
        ValueError: If any field fails validation.
        pydantic.ValidationError: If required fields are missing.
    """
    # Detect test-vector format by presence of camelCase keys
    if "policyId" in data or "walletAddress" in data or "whitelist" in data:
        normalised = _normalise_vector_format(data)
    else:
        normalised = data

    return PolicyConfig(**normalised)
