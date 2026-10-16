"""
SQLite-backed Policy Engine implementation.

Enforces 24-hour true rolling budget, atomic slot reservation, lifetime limits,
circuit breaker (RPM rate limiting, consecutive failures, half-open test probes),
and strict two-phase commit/release protocol within BEGIN IMMEDIATE transactions.

References:
    - specs/policy-engine-spec.md §3, §4.1, §4.2, §5 (Precedence, Reservation, Circuit Breaker)
    - specs/domain-model.md §2.1 (Mathematical Invariant P0-6)
    - infra/sqlite/buyer-schema.sql (ap_policy, ap_budget_ledger)
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable

from pydantic import ValidationError

from agentpay.policy.errors import PolicyErrorCode, PolicyViolationError
from agentpay.policy.evaluator import evaluate_static_rules
from agentpay.policy.interfaces import IPolicyEngine
from agentpay.policy.models import (
    BreakerState,
    PaymentRequest,
    PolicyConfig,
    PolicyDecision,
    ReservationToken,
)


class SQLitePolicyEngine(IPolicyEngine):
    """
    SQLite-backed financial firewall, budget reservation, and circuit breaker engine.

    All reservations execute under ``BEGIN IMMEDIATE`` transactions to guarantee
    process-level exclusivity and prevent concurrent overspend races.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: PolicyConfig,
        clock: Callable[[], int] | None = None,
    ) -> None:
        """
        Initialize the SQLite policy engine.

        Args:
            conn: An open SQLite connection with schema initialized.
            config: The policy configuration instance.
            clock: Optional clock callable returning Unix seconds (defaults to time.time).
        """
        self._conn = conn
        self._config = config
        self._clock = clock if clock is not None else (lambda: int(time.time()))
        self._consecutive_failures = 0
        self._sync_policy_record()

    def _sync_policy_record(self) -> None:
        """
        Upsert the policy configuration into the ap_policy table.
        Preserves existing is_frozen, breaker_state, and tripped_at from DB
        so a restart does not overwrite operator actions (P1-2, P1-3).
        """
        now = self._clock()
        cfg = self._config
        with self._conn:
            cursor = self._conn.cursor()
            cursor.execute("PRAGMA table_info(ap_policy);")
            cols = {row[1] for row in cursor.fetchall()}
            if "consecutive_failures" not in cols:
                try:
                    cursor.execute(
                        "ALTER TABLE ap_policy ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0;"
                    )
                except sqlite3.OperationalError:
                    # Column already added by concurrent thread or schema init
                    pass
            self._conn.execute(
                """
                INSERT INTO ap_policy (
                    id, name, wallet_address, max_per_tx_base_units,
                    rolling_budget_24h_base_units, lifetime_budget_base_units,
                    whitelist_payees_json, whitelist_assets_json,
                    whitelist_networks_json, whitelist_domains_json,
                    circuit_max_rpm, circuit_consecutive_failures,
                    circuit_cooldown_seconds, breaker_state, tripped_at,
                    is_frozen, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    wallet_address = excluded.wallet_address,
                    max_per_tx_base_units = excluded.max_per_tx_base_units,
                    rolling_budget_24h_base_units = excluded.rolling_budget_24h_base_units,
                    lifetime_budget_base_units = excluded.lifetime_budget_base_units,
                    whitelist_payees_json = excluded.whitelist_payees_json,
                    whitelist_assets_json = excluded.whitelist_assets_json,
                    whitelist_networks_json = excluded.whitelist_networks_json,
                    whitelist_domains_json = excluded.whitelist_domains_json,
                    circuit_max_rpm = excluded.circuit_max_rpm,
                    circuit_consecutive_failures = excluded.circuit_consecutive_failures,
                    circuit_cooldown_seconds = excluded.circuit_cooldown_seconds,
                    -- If DB already has is_frozen=1 or custom breaker state, preserve it (P1-2)
                    is_frozen = MAX(ap_policy.is_frozen, excluded.is_frozen),
                    updated_at = excluded.updated_at;
                """,
                (
                    cfg.id,
                    cfg.name,
                    cfg.wallet_address,
                    cfg.max_per_tx_base_units,
                    cfg.rolling_budget_24h_base_units,
                    cfg.lifetime_budget_base_units,
                    json.dumps(sorted(cfg.whitelist_payees)),
                    json.dumps(sorted(cfg.whitelist_assets)),
                    json.dumps(sorted(cfg.whitelist_networks)),
                    json.dumps(sorted(cfg.whitelist_domains)),
                    cfg.circuit_max_rpm,
                    cfg.circuit_consecutive_failures,
                    cfg.circuit_cooldown_seconds,
                    cfg.breaker_state.value,
                    cfg.tripped_at,
                    1 if cfg.is_frozen else 0,
                    cfg.created_at or now,
                    now,
                ),
            )
            cursor.execute(
                "SELECT consecutive_failures FROM ap_policy WHERE id = ?;", (cfg.id,)
            )
            row = cursor.fetchone()
            if row:
                self._consecutive_failures = row[0]

    def _query_lifetime_spent(self, cursor: sqlite3.Cursor, now_seconds: int) -> int:
        """
        Calculate total lifetime commitment under this policy:
        Sum of all COMMITTED amounts + active RESERVED where expires_at >= now_seconds.
        Ensures concurrent reservations cannot break the lifetime budget ceiling (P0-1).
        """
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount_base_units), 0)
            FROM ap_budget_ledger
            WHERE policy_id = ?
              AND (
                  state = 'COMMITTED'
                  OR
                  (state = 'RESERVED' AND expires_at >= ?)
              );
            """,
            (self._config.id, now_seconds),
        )
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _query_rolling_usage(self, cursor: sqlite3.Cursor, now_seconds: int) -> int:
        """
        Calculate current rolling usage per P0-6:
        COMMITTED in [now - 86400, now] + active RESERVED where expires_at >= now.
        Unifies expiration boundary with commit() at now == expires_at (P0-2).
        """
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount_base_units), 0)
            FROM ap_budget_ledger
            WHERE policy_id = ?
              AND (
                  (state = 'COMMITTED' AND created_at >= ? - 86400)
                  OR
                  (state = 'RESERVED' AND expires_at >= ?)
              );
            """,
            (self._config.id, now_seconds, now_seconds),
        )
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _query_requests_in_last_60s(
        self, cursor: sqlite3.Cursor, now_seconds: int
    ) -> int:
        """Count reservations initiated in the last 60 seconds."""
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM ap_budget_ledger
            WHERE policy_id = ? AND created_at >= ? - 60;
            """,
            (self._config.id, now_seconds),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def _query_active_probe_count(
        self, cursor: sqlite3.Cursor, now_seconds: int
    ) -> int:
        """Count in-flight active reservations."""
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM ap_budget_ledger
            WHERE policy_id = ? AND state = 'RESERVED' AND expires_at >= ?;
            """,
            (self._config.id, now_seconds),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def get_available_rolling_budget(self) -> int:
        """Return the current available rolling budget in base units."""
        now = self._clock()
        cursor = self._conn.cursor()
        usage = self._query_rolling_usage(cursor, now)
        return max(0, self._config.rolling_budget_24h_base_units - usage)

    def get_breaker_state(self) -> BreakerState:
        """
        Get the current circuit breaker state, evaluating cooldown transition
        from OPEN -> HALF_OPEN if elapsed.
        """
        now = self._clock()
        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT breaker_state, tripped_at FROM ap_policy WHERE id = ?;",
            (self._config.id,),
        )
        row = cursor.fetchone()
        if not row:
            return BreakerState.CLOSED

        current_state = BreakerState(row[0])
        tripped_at = row[1]

        if (
            current_state == BreakerState.OPEN
            and tripped_at is not None
            and now >= tripped_at + self._config.circuit_cooldown_seconds
        ):
            # Transition to HALF_OPEN
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE ap_policy
                    SET breaker_state = 'HALF_OPEN', updated_at = ?
                    WHERE id = ?;
                    """,
                    (now, self._config.id),
                )
            return BreakerState.HALF_OPEN

        return current_state

    def record_failure(self, reason: str = "") -> None:
        """
        Record a transaction or upstream delivery failure.

        Increments consecutive failures in ap_policy (persisted across instances)
        and trips breaker to OPEN if threshold reached or if in HALF_OPEN (test probe failed) (P1-3).
        """
        now = self._clock()
        current_state = self.get_breaker_state()
        with self._conn:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT consecutive_failures, circuit_consecutive_failures
                FROM ap_policy
                WHERE id = ?;
                """,
                (self._config.id,),
            )
            row = cursor.fetchone()
            curr_fails = row[0] if row else self._consecutive_failures
            threshold = (
                row[1]
                if (row and row[1])
                else self._config.circuit_consecutive_failures
            )

            new_failures = curr_fails + 1
            self._consecutive_failures = new_failures

            should_trip = (
                current_state == BreakerState.HALF_OPEN or new_failures >= threshold
            )

            if should_trip:
                cursor.execute(
                    """
                    UPDATE ap_policy
                    SET consecutive_failures = ?, breaker_state = 'OPEN', tripped_at = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (new_failures, now, now, self._config.id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE ap_policy
                    SET consecutive_failures = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (new_failures, now, self._config.id),
                )

    def reset_breaker(self) -> None:
        """Explicitly reset the circuit breaker to CLOSED (operator unfreeze)."""
        now = self._clock()
        self._consecutive_failures = 0
        with self._conn:
            self._conn.execute(
                """
                UPDATE ap_policy
                SET breaker_state = 'CLOSED', consecutive_failures = 0, tripped_at = NULL, updated_at = ?
                WHERE id = ?;
                """,
                (now, self._config.id),
            )

    def reserve(
        self,
        intent_id: str,
        amount: int,
        payee: str,
        asset: str,
        network: str,
        domain: str | None = None,
        ttl_seconds: int = 360,
    ) -> PolicyDecision:
        """
        Atomically evaluate all precedence rules and lock quota in ap_budget_ledger.
        Fails-closed on any database error.
        """
        now = self._clock()

        # Step 0: Validate PaymentRequest format early
        try:
            req = PaymentRequest(
                intent_id=intent_id,
                amount=amount,
                payee=payee,
                asset=asset,
                network=network,
                domain=domain,
                ttl_seconds=ttl_seconds,
            )
        except (ValueError, TypeError, ValidationError):
            return PolicyDecision(
                approved=False,
                error_code=PolicyErrorCode.INVALID_AMOUNT_FORMAT,
            )

        cursor = self._conn.cursor()
        try:
            # SECURITY-REVIEW: BEGIN IMMEDIATE acquires exclusive write lock immediately
            cursor.execute("BEGIN IMMEDIATE;")

            # Step 1: Policy Administrative Lock (is_frozen) from authoritative DB record (P1-2)
            cursor.execute(
                "SELECT is_frozen, breaker_state, tripped_at FROM ap_policy WHERE id = ?;",
                (self._config.id,),
            )
            row = cursor.fetchone()
            is_frozen = (row[0] == 1) if row else self._config.is_frozen
            current_breaker = (
                BreakerState(row[1]) if row and row[1] else BreakerState.CLOSED
            )
            tripped_at = row[2] if row else None

            if is_frozen:
                cursor.execute("ROLLBACK;")
                return PolicyDecision(
                    approved=False,
                    error_code=PolicyErrorCode.POLICY_FROZEN,
                )

            # Step 2: Circuit Breaker State & RPM Check
            # Check cooldown transition to HALF_OPEN
            if (
                current_breaker == BreakerState.OPEN
                and tripped_at is not None
                and now >= tripped_at + self._config.circuit_cooldown_seconds
            ):
                current_breaker = BreakerState.HALF_OPEN
                cursor.execute(
                    """
                    UPDATE ap_policy
                    SET breaker_state = 'HALF_OPEN', updated_at = ?
                    WHERE id = ?;
                    """,
                    (now, self._config.id),
                )

            if current_breaker == BreakerState.OPEN:
                cursor.execute("ROLLBACK;")
                return PolicyDecision(
                    approved=False,
                    error_code=PolicyErrorCode.CIRCUIT_BREAKER_OPEN,
                )

            if current_breaker == BreakerState.HALF_OPEN:
                # Probe Locking: permit only 1 in-flight test probe transaction
                active_probes = self._query_active_probe_count(cursor, now)
                if active_probes >= 1:
                    cursor.execute("ROLLBACK;")
                    return PolicyDecision(
                        approved=False,
                        error_code=PolicyErrorCode.CIRCUIT_BREAKER_OPEN,
                    )

            # Check RPM in last 60s
            recent_requests = self._query_requests_in_last_60s(cursor, now)
            if recent_requests >= self._config.circuit_max_rpm:
                # Exceeded RPM threshold -> Trip breaker to OPEN
                cursor.execute(
                    """
                    UPDATE ap_policy
                    SET breaker_state = 'OPEN', tripped_at = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (now, now, self._config.id),
                )
                self._conn.commit()
                return PolicyDecision(
                    approved=False,
                    error_code=PolicyErrorCode.CIRCUIT_BREAKER_OPEN,
                )

            # Step 3-5: Evaluate static rules (Format, Whitelist, Single-Tx Cap, Lifetime)
            lifetime_spent = self._query_lifetime_spent(cursor, now)
            static_res = evaluate_static_rules(
                config=self._config,
                request=req,
                lifetime_spent=lifetime_spent,
            )
            if not static_res.passed:
                cursor.execute("ROLLBACK;")
                return PolicyDecision(
                    approved=False,
                    error_code=static_res.error_code,
                )

            # Step 6: 24h Rolling Budget Check (P0-6)
            rolling_usage = self._query_rolling_usage(cursor, now)
            available = self._config.rolling_budget_24h_base_units - rolling_usage
            if available < req.amount:
                cursor.execute("ROLLBACK;")
                return PolicyDecision(
                    approved=False,
                    error_code=PolicyErrorCode.DAILY_BUDGET_EXCEEDED,
                )

            # Step 7: Insert Reservation Slot
            reservation_id = f"res_{uuid.uuid4().hex}"
            expires_at = now + req.ttl_seconds
            cursor.execute(
                """
                INSERT INTO ap_budget_ledger (
                    id, policy_id, wallet_address, intent_id,
                    amount_base_units, state, created_at, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'RESERVED', ?, ?, ?);
                """,
                (
                    reservation_id,
                    self._config.id,
                    self._config.wallet_address,
                    req.intent_id,
                    req.amount,
                    now,
                    expires_at,
                    now,
                ),
            )
            self._conn.commit()

            new_available = available - req.amount
            token = ReservationToken(
                reservation_id=reservation_id,
                intent_id=req.intent_id,
                policy_id=self._config.id,
                amount=req.amount,
                expires_at=expires_at,
                created_at=now,
            )
            return PolicyDecision(
                approved=True,
                reservation=token,
                available_after=new_available,
            )
        except sqlite3.IntegrityError:
            try:
                if self._conn.in_transaction:
                    cursor.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            # Duplicate intent_id or unique constraint violation -> fail closed
            return PolicyDecision(
                approved=False,
                error_code=PolicyErrorCode.DAILY_BUDGET_EXCEEDED,
            )
        except Exception:  # noqa: BLE001
            try:
                if self._conn.in_transaction:
                    cursor.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            # Fail closed on any other unexpected DB or system errors
            return PolicyDecision(
                approved=False,
                error_code=PolicyErrorCode.POLICY_FROZEN,
            )

    def commit(self, intent_id: str) -> None:
        """
        Transition reservation from RESERVED to COMMITTED.
        Idempotent: Subsequent calls for already COMMITTED intent are no-ops.
        Resets consecutive failures to 0 and transitions HALF_OPEN to CLOSED.
        """
        now = self._clock()
        cursor = self._conn.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        try:
            cursor.execute(
                """
                SELECT state, expires_at
                FROM ap_budget_ledger
                WHERE intent_id = ?;
                """,
                (intent_id,),
            )
            row = cursor.fetchone()
            if not row:
                cursor.execute("ROLLBACK;")
                return

            state, expires_at = row[0], row[1]
            if state == "COMMITTED":
                cursor.execute("ROLLBACK;")
                return  # Idempotent no-op

            if now > expires_at:
                cursor.execute("ROLLBACK;")
                raise PolicyViolationError(
                    PolicyErrorCode.RESERVATION_EXPIRED,
                    f"Reservation for intent {intent_id} expired at {expires_at} (current: {now})",
                )

            cursor.execute(
                """
                UPDATE ap_budget_ledger
                SET state = 'COMMITTED', updated_at = ?
                WHERE intent_id = ? AND state = 'RESERVED';
                """,
                (now, intent_id),
            )

            # Success resets consecutive failure count & closes breaker if HALF_OPEN (P1-3)
            self._consecutive_failures = 0
            cursor.execute(
                """
                UPDATE ap_policy
                SET consecutive_failures = 0,
                    breaker_state = CASE WHEN breaker_state = 'HALF_OPEN' THEN 'CLOSED' ELSE breaker_state END,
                    tripped_at = CASE WHEN breaker_state = 'HALF_OPEN' THEN NULL ELSE tripped_at END,
                    updated_at = ?
                WHERE id = ?;
                """,
                (now, self._config.id),
            )
            self._conn.commit()
        except PolicyViolationError:
            raise
        except Exception:
            try:
                if self._conn.in_transaction:
                    cursor.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise

    def release(self, intent_id: str, reason: str | None = None) -> None:
        """
        Transition reservation from RESERVED to RELEASED.
        Guarded: If intent was already COMMITTED, raises RESERVATION_ALREADY_FINALIZED.
        Idempotent: Subsequent releases on already RELEASED intent are no-ops.
        """
        now = self._clock()
        cursor = self._conn.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        try:
            cursor.execute(
                """
                SELECT state
                FROM ap_budget_ledger
                WHERE intent_id = ?;
                """,
                (intent_id,),
            )
            row = cursor.fetchone()
            if not row:
                cursor.execute("ROLLBACK;")
                return

            state = row[0]
            if state == "COMMITTED":
                cursor.execute("ROLLBACK;")
                raise PolicyViolationError(
                    PolicyErrorCode.RESERVATION_ALREADY_FINALIZED,
                    f"Cannot release intent {intent_id}: already COMMITTED",
                )

            if state == "RELEASED":
                cursor.execute("ROLLBACK;")
                return  # Idempotent no-op

            cursor.execute(
                """
                UPDATE ap_budget_ledger
                SET state = 'RELEASED', updated_at = ?
                WHERE intent_id = ? AND state = 'RESERVED';
                """,
                (now, intent_id),
            )
            self._conn.commit()
        except PolicyViolationError:
            raise
        except Exception:
            try:
                if self._conn.in_transaction:
                    cursor.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise
