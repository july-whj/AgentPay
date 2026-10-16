"""
AgentPay Policy Engine — public API surface.

Exports all data models, error codes, configuration loader, static rule evaluator,
SQLite-backed engine implementation, connection factory, and the abstract engine interface.
"""

from agentpay.policy.config import load_policy_config
from agentpay.policy.db import create_buyer_connection, init_buyer_database
from agentpay.policy.errors import PolicyErrorCode, PolicyViolationError
from agentpay.policy.evaluator import StaticEvaluationResult, evaluate_static_rules
from agentpay.policy.interfaces import IPolicyEngine
from agentpay.policy.models import (
    BreakerState,
    LedgerEntryState,
    PaymentRequest,
    PolicyConfig,
    PolicyDecision,
    ReservationToken,
)
from agentpay.policy.sqlite_engine import SQLitePolicyEngine

__all__ = [
    "BreakerState",
    "IPolicyEngine",
    "LedgerEntryState",
    "PaymentRequest",
    "PolicyConfig",
    "PolicyDecision",
    "PolicyErrorCode",
    "PolicyViolationError",
    "ReservationToken",
    "SQLitePolicyEngine",
    "StaticEvaluationResult",
    "create_buyer_connection",
    "evaluate_static_rules",
    "init_buyer_database",
    "load_policy_config",
]
