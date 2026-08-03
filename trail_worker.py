#!/usr/bin/env python3
"""One-shot server worker for batched trailing stop maintenance."""

from __future__ import annotations

import json
import math
import random
import secrets
import signal
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from simple_hyper.runtime import ensure_local_venv


ensure_local_venv(__file__)

from hl_order import (  # noqa: E402
    DEFAULT_SLIPPAGE,
    GRID_TARGET_ORDERS_PER_SIDE,
    SERVER_BATCH_PATH,
    asset_requires_isolated_margin,
    build_clients,
    build_grid_limit_order_plan,
    build_trigger_order_plan,
    clear_info_cache,
    collect_frontend_open_orders,
    decimal_or_none,
    decimal_to_plain,
    fill_matches_coin,
    format_signed_percent,
    grid_avg_multiplier,
    grid_avg_topup_params,
    grid_batch_open_oids,
    grid_limit_policy_from_row,
    load_economic_chain_realized_surplus_map,
    grid_order_allowed_by_max,
    grid_order_should_reduce_only,
    grid_order_would_add_risk,
    grid_position_bounds,
    grid_size_for_min_notional,
    is_post_only_reject_text,
    load_server_batch,
    log_event,
    mask,
    order_plan_request,
    position_matches_coin,
    signed_position_value,
    successful_cancel_oids,
    resolve_perp_asset,
    rounded_perp_price,
    save_server_batch,
    server_batch_lock,
    trail_stop_price,
    update_isolated_opening_leverage,
)
from simple_hyper.order_specs import MIN_NOTIONAL
from hyperliquid.utils.types import Cloid


RATE_LIMIT_LOG_PATH = SERVER_BATCH_PATH.parent / "logs" / "trail-rate-limit.jsonl"
ACTION_AUDIT_LOG_PATH = SERVER_BATCH_PATH.parent / "logs" / "trail-action-audit.jsonl"
API_TIMING_LOG_PATH = SERVER_BATCH_PATH.parent / "logs" / "trail-api-timing.jsonl"
DONE_RETENTION_DAYS = 7
DONE_RETENTION_MAX = 500
GRID_LEVEL_HISTORY_MAX = 120
GRID_FILL_LOOKBACK_SECONDS = 24 * 60 * 60
GRID_USER_FILLS_HARD_TIMEOUT_SECONDS = 30.0
GRID_MAX_SUBMISSIONS_PER_SIDE_PER_RUN = 1
GRID_ADD_RISK_BRAKE_STREAK = 2
GRID_ADD_RISK_BRAKE_PAIR_RETENTION_SECONDS = 24 * 60 * 60
GRID_ADD_RISK_BRAKE_HISTORY_RETENTION_SECONDS = 7 * 24 * 60 * 60
GRID_UNKNOWN_OID_RECOVERY_MAX_AGE_SECONDS = 30 * 60
GRID_ALO_PRICE_ATTEMPT_LIMIT = 20
GRID_ALO_SPACING_MULTIPLIER = Decimal("0.95")
GRID_NEAR_REGRID_STALE_GAP_MULTIPLE = Decimal("30")
GRID_NEAR_REGRID_TARGET_GAP_MULTIPLE = Decimal("15")
GRID_PANIC_RATIO_THRESHOLD = Decimal("100")
GRID_PANIC_REVERSAL_GAP_MULTIPLIER = Decimal("2")
GRID_EMERGENCY_ACTION_LIMIT_WAIT_SECONDS = 10
GRID_PANIC_REDUCE_MIN_NOTIONAL = MIN_NOTIONAL * Decimal("1.10")
GRID_LIMIT_CHASE_MIN_NOTIONAL_MULTIPLIER = Decimal("1.10")
GRID_PENDING_CANCEL_STATUS = "pending_cancel"
GRID_PENDING_CANCEL_MIN_RATE_PERCENT = Decimal("1")
GRID_PENDING_CANCEL_SPECIAL_RATE = Decimal("0.20")
GRID_ROE_MIN_POSITION_VALUE = Decimal("100")
GRID_ROE_DENSITY_THRESHOLD = Decimal("-0.10")
GRID_ROE_STOP_THRESHOLD = Decimal("-0.40")
GRID_SURVIVAL_ACTIVE_ORDERS_PER_SIDE = 1
GRID_WITHDRAWABLE_REDUCE_ONLY_THRESHOLD = Decimal("5")
GRID_WITHDRAWABLE_PAUSE_THRESHOLD = Decimal("10")
GRID_LIFECYCLE_VERSION = 2
GRID_LEGACY_PAUSE_STATUS = "legacy_pause"
GRID_MARGIN_STATUS = "margin"
GRID_CHAIN_DEBT_STATUS = "chain_debt"
GRID_BIRTH_INTENT_UNKNOWN_GRACE_SECONDS = 120
GRID_P6_WITHDRAWABLE_THRESHOLD = Decimal("3")
GRID_P7_RAW_DEFICIT_THRESHOLD = -100
GRID_P7_WITHDRAWABLE_THRESHOLD = Decimal("5")
GRID_P7_MIN_ACTIVE_PER_SIDE = 5
GRID_P7_MIN_ORDER_AGE_SECONDS = 10 * 60
GRID_P9_WITHDRAWABLE_THRESHOLD = Decimal("1")
GRID_P10_MARGIN_ADEQUACY_THRESHOLD = Decimal("1.1")
GRID_P10_REQUIRED_ACTION_HEADROOM = 3
GRID_P3_MAX_RESTORES_PER_RUN = 10
GRID_PANIC_RATIO_LEGACY_DEFAULT_THRESHOLDS = {
    Decimal("10"),
    Decimal("20"),
    Decimal("30"),
    Decimal("50"),
    Decimal("60"),
    Decimal("65"),
    Decimal("70"),
    Decimal("75"),
    Decimal("80"),
    Decimal("85"),
}
GRID_REPLACEMENT_PAUSE_STATUS = "paused_replacement"
GRID_RISK_DENSITY_PAUSE_STATUS = "paused_risk_density"
GRID_ROE_PAUSE_STATUS = "paused_roe"
GRID_ACTIVE_CAP_PAUSE_STATUS = "paused_active_cap"
GRID_WITHDRAWABLE_PAUSE_STATUS = "paused_withdrawable"
GRID_ACTION_LIMIT_PAUSE_STATUS = "paused_action_limit"
GRID_ACTION_LIMIT_P1_BUDGET_PER_RUN = 1
GRID_ACTION_LIMIT_P2_HEADROOM_THRESHOLD = 100
GRID_REPLACEMENT_ACTIVE_CAP_SUBMIT_THRESHOLD = 32
GRID_ACTION_PHASE_P0 = "p0"
GRID_ACTION_PHASE_P1_LATEST_REPLACEMENT = "p1_latest_replacement"
GRID_ACTION_PHASE_P1_PAUSED_REPLACEMENT = "p1_paused_replacement"
GRID_ACTION_PHASE_P1_CANCELS = "p1_cancels"
GRID_ACTION_PHASE_P1_TOPUP = "p1_topup"
GRID_ACTION_PHASE_P1_RESTORE = "p1_restore"
GRID_ACTION_PHASE_P1_WITHDRAWABLE = "p1_withdrawable"
GRID_ACTION_PHASE_P2 = "p2"
GRID_ACTION_PHASES = (
    GRID_ACTION_PHASE_P0,
    GRID_ACTION_PHASE_P1_LATEST_REPLACEMENT,
    GRID_ACTION_PHASE_P1_PAUSED_REPLACEMENT,
    GRID_ACTION_PHASE_P1_CANCELS,
    GRID_ACTION_PHASE_P1_TOPUP,
    GRID_ACTION_PHASE_P1_RESTORE,
    GRID_ACTION_PHASE_P1_WITHDRAWABLE,
    GRID_ACTION_PHASE_P2,
)
GRID_LIFECYCLE_PHASE_P0 = "p0"
GRID_LIFECYCLE_PHASE_P1 = "p1"
GRID_LIFECYCLE_PHASE_P2 = "p2"
GRID_LIFECYCLE_PHASE_P3 = "p3"
GRID_LIFECYCLE_PHASE_P4 = "p4"
GRID_LIFECYCLE_PHASE_P5 = "p5"
GRID_LIFECYCLE_PHASE_P6 = "p6"
GRID_LIFECYCLE_PHASE_P7 = "p7"
GRID_LIFECYCLE_PHASE_P8 = "p8"
GRID_LIFECYCLE_PHASE_P9 = "p9"
GRID_LIFECYCLE_PHASE_P10 = "p10"
GRID_LIFECYCLE_PHASES = (
    GRID_LIFECYCLE_PHASE_P0,
    GRID_LIFECYCLE_PHASE_P1,
    GRID_LIFECYCLE_PHASE_P2,
    GRID_LIFECYCLE_PHASE_P3,
    GRID_LIFECYCLE_PHASE_P4,
    GRID_LIFECYCLE_PHASE_P5,
    GRID_LIFECYCLE_PHASE_P6,
    GRID_LIFECYCLE_PHASE_P7,
    GRID_LIFECYCLE_PHASE_P8,
    GRID_LIFECYCLE_PHASE_P9,
    GRID_LIFECYCLE_PHASE_P10,
)
GRID_P8_REACCUMULATE_STATUS = "p8_reaccumulate"
GRID_MAX_LEVELS_PER_SIDE = 1024
GRID_MAX_ACTIVE_ORDERS_PER_SIDE = 16
GRID_PAUSED_STATUSES = {
    "paused_max",
    "paused_limit",
    "paused_margin",
    "paused_reduce_capacity",
    GRID_ACTION_LIMIT_PAUSE_STATUS,
    GRID_REPLACEMENT_PAUSE_STATUS,
    GRID_RISK_DENSITY_PAUSE_STATUS,
    GRID_ROE_PAUSE_STATUS,
    GRID_ACTIVE_CAP_PAUSE_STATUS,
    GRID_WITHDRAWABLE_PAUSE_STATUS,
}
GRID_PRICE_OCCUPANCY_STATUSES = {
    "active",
    "pending",
    "recovery_deferred",
    GRID_PENDING_CANCEL_STATUS,
    *GRID_PAUSED_STATUSES,
}
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
TRANSIENT_ERROR_TEXTS = (
    "connection reset",
    "connection aborted",
    "connection refused",
    "remote end closed connection",
    "temporarily unavailable",
    "timed out",
    "timeout",
)


class GridPostOnlyRejected(Exception):
    """A post-only grid child became marketable before submission."""


class GridActionBudgetUnavailable(Exception):
    """A P1 exchange action has no remaining pre-reserved request budget."""


class WorkerApiHardTimeout(TimeoutError):
    """A read-only SDK call exceeded the worker's wall-clock deadline."""


@contextmanager
def worker_api_hard_timeout(seconds: float, label: str):
    """Enforce a wall-clock deadline for a read call in the Linux worker."""
    if (
        seconds <= 0
        or threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "SIGALRM")
        or not hasattr(signal, "setitimer")
    ):
        yield
        return

    def timeout_handler(_signum: int, _frame: Any) -> None:
        raise WorkerApiHardTimeout(f"{label} hard timeout after {seconds:g}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


class WorkerApiProxy:
    """Measure high-level SDK calls without changing their return values."""

    def __init__(self, target: Any, cache: dict[str, Any], client: str) -> None:
        self._worker_api_target = target
        self._worker_api_cache = cache
        self._worker_api_client = client

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._worker_api_target, name)
        if not callable(attribute) or name.startswith("_") or name == "clear_cache":
            return attribute

        def measured(*args: Any, **kwargs: Any) -> Any:
            started_at = time.monotonic()
            error = False
            try:
                if self._worker_api_client == "info" and name == "user_fills_by_time":
                    with worker_api_hard_timeout(
                        GRID_USER_FILLS_HARD_TIMEOUT_SECONDS,
                        "info.user_fills_by_time",
                    ):
                        return attribute(*args, **kwargs)
                return attribute(*args, **kwargs)
            except Exception:
                error = True
                raise
            finally:
                record_worker_api_timing(
                    self._worker_api_cache,
                    self._worker_api_client,
                    name,
                    time.monotonic() - started_at,
                    error,
                )

        return measured


def worker_api_stat_key(cache: dict[str, Any], client: str, method: str) -> str:
    phase = str(cache.get("grid_action_phase") or cache.get("api_stat_phase") or "setup")
    context = str(cache.get("api_stat_context") or "-")
    return f"{phase}|{context}|{client}.{method}"


def record_worker_api_timing(
    cache: dict[str, Any],
    client: str,
    method: str,
    elapsed: float,
    error: bool = False,
) -> None:
    stats = cache.setdefault("api_stats", {})
    key = worker_api_stat_key(cache, client, method)
    item = stats.setdefault(
        key,
        {
            "phase": key.split("|", 1)[0],
            "context": key.split("|", 2)[1],
            "method": f"{client}.{method}",
            "count": 0,
            "errors": 0,
            "total_ms": 0.0,
            "max_ms": 0.0,
        },
    )
    elapsed_ms = max(0.0, elapsed * 1000)
    item["count"] += 1
    item["errors"] += int(error)
    item["total_ms"] += elapsed_ms
    item["max_ms"] = max(float(item["max_ms"]), elapsed_ms)


def instrument_worker_client_tuple(
    clients: tuple[Any, Any | None, str, str, dict[str, Any]],
    cache: dict[str, Any],
) -> tuple[Any, Any | None, str, str, dict[str, Any]]:
    info, exchange, account, signer, role = clients
    raw_info = getattr(info, "_info", None)
    if raw_info is not None:
        if not isinstance(raw_info, WorkerApiProxy):
            info._info = WorkerApiProxy(raw_info, cache, "info")
    elif not isinstance(info, WorkerApiProxy):
        info = WorkerApiProxy(info, cache, "info")
    if exchange is not None and not isinstance(exchange, WorkerApiProxy):
        exchange = WorkerApiProxy(exchange, cache, "exchange")
    return info, exchange, account, signer, role


def build_worker_clients(
    cache: dict[str, Any],
    network: str,
    timeout: float,
    raw_coin: str,
    *,
    need_exchange: bool = True,
) -> tuple[Any, Any | None, str, str, dict[str, Any]]:
    started_at = time.monotonic()
    error = False
    try:
        clients = build_clients(network, timeout, raw_coin, need_exchange=need_exchange)
    except Exception:
        error = True
        raise
    finally:
        record_worker_api_timing(
            cache,
            "client",
            "build_clients",
            time.monotonic() - started_at,
            error,
        )
    return instrument_worker_client_tuple(clients, cache)


def worker_api_stats_payload(cache: dict[str, Any]) -> dict[str, Any] | None:
    stats = cache.get("api_stats")
    if not isinstance(stats, dict) or not stats:
        return None
    methods = sorted(
        (
            {
                **item,
                "total_ms": round(float(item["total_ms"]), 3),
                "max_ms": round(float(item["max_ms"]), 3),
            }
            for item in stats.values()
        ),
        key=lambda item: (-float(item["total_ms"]), str(item["phase"]), str(item["method"])),
    )
    request_methods = [item for item in methods if not str(item["method"]).startswith("client.")]
    return {
        "ts": int(time.time()),
        "run_started_at": int(cache.get("run_started_at") or 0),
        "run_elapsed_ms": round((time.monotonic() - float(cache.get("run_monotonic_started_at") or 0)) * 1000, 3),
        "request_count": sum(int(item["count"]) for item in request_methods),
        "error_count": sum(int(item["errors"]) for item in request_methods),
        "api_total_ms": round(sum(float(item["total_ms"]) for item in request_methods), 3),
        "client_build_count": sum(
            int(item["count"]) for item in methods if str(item["method"]) == "client.build_clients"
        ),
        "observed_total_ms": round(sum(float(item["total_ms"]) for item in methods), 3),
        "methods": methods,
    }


def emit_worker_api_stats(cache: dict[str, Any]) -> dict[str, Any] | None:
    payload = worker_api_stats_payload(cache)
    if payload is None:
        return None
    try:
        API_TIMING_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with API_TIMING_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        print(f"trail_worker: api_stats_write_error={type(exc).__name__}: {exc}")
    top = payload["methods"][:8]
    top_text = "; ".join(
        f"{item['phase']}/{item['context']}/{item['method']}="
        f"{item['count']}x/{item['total_ms']:.1f}ms/max{item['max_ms']:.1f}/err{item['errors']}"
        for item in top
    )
    print(
        f"trail_worker: api_stats requests={payload['request_count']} errors={payload['error_count']} "
        f"api_total={payload['api_total_ms']:.1f}ms client_builds={payload['client_build_count']} "
        f"observed_total={payload['observed_total_ms']:.1f}ms "
        f"run_elapsed={payload['run_elapsed_ms']:.1f}ms top={top_text}"
    )
    return payload


def audit_grid_action(action: str, **payload: Any) -> None:
    record = {"ts": int(time.time()), "action": action, **payload}
    ACTION_AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ACTION_AUDIT_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


ECONOMIC_CHAIN_ID_RANDOM_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
ECONOMIC_CHAIN_ID_RANDOM_LENGTH = 4
ECONOMIC_CHAIN_ID_PREFIX = "C"
ECONOMIC_CHAIN_ID_CHINA_UTC_OFFSET_SECONDS = 8 * 60 * 60
_ECONOMIC_CHAIN_IDS_GENERATED_THIS_RUN: set[str] = set()
_ECONOMIC_CHAIN_IDS_BY_LINEAGE: dict[tuple[str, ...], str] = {}


def economic_chain_lineage_key(
    row: dict[str, Any],
    entry: dict[str, Any],
) -> tuple[str, ...]:
    """Return the best available stable lineage evidence for one order."""
    grid_id = str(row.get("id") or row.get("coin") or "grid")
    birth_intent_cloid = str(entry.get("birth_intent_cloid") or "").strip()
    if birth_intent_cloid:
        return (grid_id, "birth", birth_intent_cloid)
    for key in ("source_oid", "p9_source_oid", "confirmed_filled_oid", "oid"):
        value = entry.get(key)
        if value is not None and str(value).strip():
            return (grid_id, "oid", str(value))
    queue_seq = entry.get("p3_queue_seq")
    if queue_seq is not None:
        return (grid_id, "p3", str(queue_seq))
    created_at = (
        entry.get("submitted_at")
        or entry.get("created_at")
        or entry.get("chain_debt_at")
        or entry.get("margin_at")
        or 0
    )
    return (
        grid_id,
        "legacy",
        str(created_at),
        str(entry.get("side") or "-"),
        str(entry.get("price", entry.get("limit_px")) or "-"),
        str(entry.get("size") or "-"),
    )


def lifecycle_prepare_economic_chain_ids(rows: list[dict[str, Any]]) -> None:
    """Prepare persisted lineage inheritance and reset this-run ID tracking."""
    _ECONOMIC_CHAIN_IDS_GENERATED_THIS_RUN.clear()
    _ECONOMIC_CHAIN_IDS_BY_LINEAGE.clear()

    def visit(row: dict[str, Any], value: Any) -> None:
        if isinstance(value, dict):
            chain_id = str(value.get("economic_chain_id") or "").strip()
            if chain_id:
                _ECONOMIC_CHAIN_IDS_BY_LINEAGE.setdefault(
                    economic_chain_lineage_key(row, value), chain_id,
                )
            for nested in value.values():
                visit(row, nested)
        elif isinstance(value, list):
            for nested in value:
                visit(row, nested)

    for row in rows:
        visit(row, row)


def new_economic_chain_id() -> str:
    """Generate C+China YYMMDDHHmm+4 letters, unique within this worker run."""
    timestamp = time.strftime(
        "%y%m%d%H%M",
        time.gmtime(time.time() + ECONOMIC_CHAIN_ID_CHINA_UTC_OFFSET_SECONDS),
    )
    while True:
        suffix = "".join(
            secrets.choice(ECONOMIC_CHAIN_ID_RANDOM_ALPHABET)
            for _ in range(ECONOMIC_CHAIN_ID_RANDOM_LENGTH)
        )
        chain_id = f"{ECONOMIC_CHAIN_ID_PREFIX}{timestamp}{suffix}"
        if chain_id not in _ECONOMIC_CHAIN_IDS_GENERATED_THIS_RUN:
            _ECONOMIC_CHAIN_IDS_GENERATED_THIS_RUN.add(chain_id)
            return chain_id


def current_economic_chain_id(value: Any) -> bool:
    """Return whether an ID was born under the complete C+time+random scheme."""
    chain_id = str(value or "").strip()
    root, separator, branch = chain_id.partition("_")
    return (
        len(root) == 15
        and root.startswith(ECONOMIC_CHAIN_ID_PREFIX)
        and root[1:11].isdigit()
        and root[11:].isalpha()
        and (not separator or (branch.isdigit() and int(branch) > 0))
    )


def economic_chain_id(row: dict[str, Any], entry: dict[str, Any]) -> str:
    """Return one stable identifier for the economic order chain."""
    existing = str(entry.get("economic_chain_id") or "").strip()
    if existing:
        _ECONOMIC_CHAIN_IDS_BY_LINEAGE.setdefault(
            economic_chain_lineage_key(row, entry), existing,
        )
        entry["economic_chain_id"] = existing
        return existing
    lineage_key = economic_chain_lineage_key(row, entry)
    chain_id = _ECONOMIC_CHAIN_IDS_BY_LINEAGE.get(lineage_key)
    if chain_id is None:
        chain_id = new_economic_chain_id()
        _ECONOMIC_CHAIN_IDS_BY_LINEAGE[lineage_key] = chain_id
    entry["economic_chain_id"] = chain_id
    return chain_id


def pending_cancel_rate(deficit: int) -> Decimal | None:
    if deficit <= 0:
        return None
    if deficit == 1:
        return GRID_PENDING_CANCEL_SPECIAL_RATE
    return max(
        GRID_PENDING_CANCEL_MIN_RATE_PERCENT,
        Decimal("4") / Decimal(str(math.log10(deficit))),
    ) / Decimal("100")


def action_limit_deficit(cache: dict[str, Any] | None) -> int:
    if not isinstance(cache, dict):
        return 0
    try:
        return max(0, int(cache.get("action_limit_deficit") or 0))
    except (TypeError, ValueError):
        return 0


def raw_action_limit_deficit(cache: dict[str, Any] | None) -> int:
    """Return used-cap, preserving negative headroom for lifecycle phase gates."""
    if not isinstance(cache, dict):
        return 0
    if "action_limit_headroom" in cache and not action_limit_error(cache):
        try:
            return -max(0, int(cache.get("action_limit_headroom") or 0))
        except (TypeError, ValueError):
            return 0
    try:
        return int(cache.get("action_limit_raw_deficit", cache.get("action_limit_deficit", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def lifecycle_p3_pending_pool(
    rows: list[dict[str, Any]],
    active_grid_indexes: list[int],
) -> list[tuple[int, dict[str, Any]]]:
    """Snapshot P3 debt in the same order grids and their levels were queued."""
    pending: list[tuple[int, dict[str, Any]]] = []
    for index in active_grid_indexes:
        for entry in rows[index].get("levels") or []:
            if (
                isinstance(entry, dict)
                and str(entry.get("status") or "") in {GRID_MARGIN_STATUS, GRID_CHAIN_DEBT_STATUS}
            ):
                pending.append((index, entry))
    # P3 is a fixed FIFO pool. Queue positions are initialized before the
    # worker shuffles indexes for the other lifecycle phases.
    return sorted(pending, key=lambda item: int(item[1]["p3_queue_seq"]))


def lifecycle_p3_restore_budget(withdrawable: Decimal | None) -> int:
    """Snapshot the strict withdrawable ladder for one P3 worker phase."""
    if withdrawable is None:
        return 0
    return sum(
        1
        for threshold in range(1, GRID_P3_MAX_RESTORES_PER_RUN + 1)
        if withdrawable > Decimal(threshold)
    )


def lifecycle_initialize_p3_queue(
    rows: list[dict[str, Any]],
    active_grid_indexes: list[int],
) -> bool:
    """Backfill stable FIFO positions for debt loaded from older state."""
    maximum = -1
    for index in active_grid_indexes:
        for entry in rows[index].get("levels") or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("status") or "") not in {GRID_MARGIN_STATUS, GRID_CHAIN_DEBT_STATUS}:
                continue
            try:
                maximum = max(maximum, int(entry["p3_queue_seq"]))
            except (KeyError, TypeError, ValueError):
                continue
    changed = False
    next_seq = maximum + 1
    for index in sorted(active_grid_indexes):
        for entry in rows[index].get("levels") or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("status") or "") not in {GRID_MARGIN_STATUS, GRID_CHAIN_DEBT_STATUS}:
                continue
            try:
                int(entry["p3_queue_seq"])
            except (KeyError, TypeError, ValueError):
                entry["p3_queue_seq"] = next_seq
                next_seq += 1
                changed = True
    return changed


def lifecycle_backfill_economic_chain_ids(
    rows: list[dict[str, Any]],
    active_grid_indexes: list[int],
    now: int,
) -> tuple[int, dict[str, int], dict[str, int]]:
    """Persist stable chain IDs for historical active orders and P3 debt."""
    total = 0
    by_status: dict[str, int] = {}
    by_coin: dict[str, int] = {}
    target_statuses = {"active", GRID_MARGIN_STATUS, GRID_CHAIN_DEBT_STATUS}
    for index in sorted(active_grid_indexes):
        row = rows[index]
        row_count = 0
        for entry in row.get("levels") or []:
            if not isinstance(entry, dict):
                continue
            status = str(entry.get("status") or "")
            if status not in target_statuses:
                continue
            if str(entry.get("economic_chain_id") or "").strip():
                continue
            economic_chain_id(row, entry)
            entry["economic_chain_id_backfilled_at"] = now
            total += 1
            row_count += 1
            by_status[status] = by_status.get(status, 0) + 1
            coin = str(row.get("coin") or "-")
            by_coin[coin] = by_coin.get(coin, 0) + 1
        if row_count:
            row["economic_chain_id_backfilled_at"] = now
            row["economic_chain_id_backfilled_count"] = int(
                row.get("economic_chain_id_backfilled_count") or 0
            ) + row_count
    return total, by_status, by_coin


def lifecycle_assign_p3_queue_seq(entry: dict[str, Any], cache: dict[str, Any]) -> None:
    """Assign one worker-global FIFO position to newly created P3 debt."""
    if str(entry.get("status") or "") not in {GRID_MARGIN_STATUS, GRID_CHAIN_DEBT_STATUS}:
        return
    try:
        int(entry["p3_queue_seq"])
        return
    except (KeyError, TypeError, ValueError):
        pass
    next_seq = cache.get("lifecycle_p3_next_seq")
    if next_seq is None:
        maximum = -1
        for row in cache.get("grid_rows") or []:
            for candidate in row.get("levels") or []:
                try:
                    maximum = max(maximum, int(candidate.get("p3_queue_seq")))
                except (AttributeError, TypeError, ValueError):
                    continue
        next_seq = maximum + 1
    entry["p3_queue_seq"] = int(next_seq)
    cache["lifecycle_p3_next_seq"] = int(next_seq) + 1


def lifecycle_p3_failure_is_unknown(entry: dict[str, Any]) -> bool:
    """Only deprioritize errors without a known retry/recovery path."""
    error_text = str(entry.get("last_error") or "").strip()
    if not error_text:
        return False
    lowered = error_text.lower()
    known_retryable = (
        is_insufficient_margin_text(error_text)
        or is_post_only_reject_text(error_text)
        or is_cumulative_action_limit_text(error_text)
        or any(marker in lowered for marker in TRANSIENT_ERROR_TEXTS)
    )
    return not known_retryable


def lifecycle_p7_farthest_pair(
    row: dict[str, Any],
    now: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return the farthest eligible active leg-1 buy/sell pair for one grid."""
    buys: list[tuple[Decimal, dict[str, Any]]] = []
    sells: list[tuple[Decimal, dict[str, Any]]] = []
    active_buy_count = 0
    active_sell_count = 0
    for entry in row.get("levels") or []:
        if isinstance(entry, dict) and str(entry.get("status") or "") == "active":
            if str(entry.get("side") or "") == "buy":
                active_buy_count += 1
            elif str(entry.get("side") or "") == "sell":
                active_sell_count += 1
        if (
            not isinstance(entry, dict)
            or str(entry.get("status") or "") != "active"
            or lifecycle_leg(entry) != 1
            or entry.get("oid") is None
        ):
            continue
        price = decimal_or_none(entry.get("price", entry.get("limit_px")))
        if price is None or price <= 0:
            continue
        if now is not None:
            try:
                submitted_at = int(entry.get("submitted_at") or 0)
            except (TypeError, ValueError):
                submitted_at = 0
            # Missing/invalid timestamps are treated as infinitely old by policy.
            if submitted_at > 0 and now - submitted_at < GRID_P7_MIN_ORDER_AGE_SECONDS:
                continue
        side = str(entry.get("side") or "")
        if side == "buy":
            buys.append((price, entry))
        elif side == "sell":
            sells.append((price, entry))
    if (
        active_buy_count <= GRID_P7_MIN_ACTIVE_PER_SIDE
        or active_sell_count <= GRID_P7_MIN_ACTIVE_PER_SIDE
        or not buys
        or not sells
    ):
        return None
    return min(buys, key=lambda item: item[0])[1], max(sells, key=lambda item: item[0])[1]


def lifecycle_p7_debt_value(row: dict[str, Any]) -> Decimal | None:
    """Estimate the reworkable leg-1 debt value for P7 prioritization."""
    if lifecycle_p7_farthest_pair(row) is None:
        return None
    buy_size = Decimal("0")
    sell_size = Decimal("0")
    for entry in row.get("levels") or []:
        if (
            not isinstance(entry, dict)
            or str(entry.get("status") or "") != "active"
            or lifecycle_leg(entry) != 1
        ):
            continue
        size = decimal_or_none(entry.get("size"))
        if size is None or size <= 0:
            continue
        if str(entry.get("side") or "") == "buy":
            buy_size += size
        elif str(entry.get("side") or "") == "sell":
            sell_size += size
    min_order_value = decimal_or_none(row.get("min_order_value")) or MIN_NOTIONAL
    leverage = decimal_or_none(row.get("lifecycle_max_leverage"))
    if min(buy_size, sell_size) <= 0 or min_order_value <= 0 or leverage is None or leverage <= 0:
        return None
    return min(buy_size, sell_size) * min_order_value / leverage


def lifecycle_p7_priority_indexes(rows: list[dict[str, Any]], indexes: list[int]) -> list[int]:
    """Order P7 work by debt value, while finishing persisted intents first."""
    def priority(index: int) -> tuple[int, Decimal]:
        row = rows[index]
        value = lifecycle_p7_debt_value(row)
        return (
            int(isinstance(row.get("p7_restructure_intent"), dict)),
            value or Decimal("0"),
        )

    return sorted(indexes, key=priority, reverse=True)


def lifecycle_p7_source_snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    """Keep enough of a cancelled source order to restore it through P3."""
    snapshot = deepcopy(entry)
    snapshot["oid"] = None
    snapshot["status"] = GRID_CHAIN_DEBT_STATUS
    snapshot.pop("submitted_at", None)
    snapshot.pop("cancelled_at", None)
    snapshot.pop("pending_cancel_at", None)
    snapshot.pop("pending_cancel_reason", None)
    snapshot["p7_restore"] = True
    snapshot["p7_restructure"] = True
    return snapshot


def lifecycle_p7_cancel_result(
    exchange: Any,
    coin: str,
    requests: list[dict[str, int]],
    cache: dict[str, Any],
) -> tuple[set[int], list[str]]:
    """Cancel a P7 pair while retaining partial-success information."""
    try:
        reserve_grid_exchange_actions(cache, len(requests))
        result = exchange.bulk_cancel(requests)
    except Exception as exc:
        return set(), [str(exc)]
    audit_grid_action(
        "grid_p7_restructure_cancel",
        coin=coin,
        requests=requests,
        result=result,
        phase=cache.get("grid_action_phase"),
    )
    if not isinstance(result, dict) or result.get("status") != "ok":
        return set(), [str(result)]
    return successful_cancel_oids(result, requests)


def lifecycle_process_p7(row: dict[str, Any], ctx: dict[str, Any], cache: dict[str, Any]) -> tuple[bool, int]:
    """Compress one remote leg-1 pair and defer its replacement to next-round P3."""
    if (
        raw_action_limit_deficit(cache) >= GRID_P7_RAW_DEFICIT_THRESHOLD
        or ctx["withdrawable"] is None
        or ctx["withdrawable"] >= GRID_P7_WITHDRAWABLE_THRESHOLD
    ):
        return False, 0
    claims = cache.setdefault("lifecycle_p7_claims", set())
    # P7 has the same account-wide quota as P1: one pair per account per
    # worker round, merged across DEXes and coins.
    claim_key = (ctx["network"], str(ctx["account"]).lower())
    if claim_key in claims:
        return False, 0
    claims.add(claim_key)

    levels = row.setdefault("levels", [])
    intent = row.get("p7_restructure_intent")
    if not isinstance(intent, dict):
        pair = lifecycle_p7_farthest_pair(row, now=ctx["now"])
        if pair is None:
            return False, 0
        buy, sell = pair
        buy_oid, sell_oid = int(buy["oid"]), int(sell["oid"])
        if buy_oid not in ctx["open_oids"] or sell_oid not in ctx["open_oids"]:
            return False, 0
        intent = {
            "status": "cancelling",
            "created_at": ctx["now"],
            "buy_oid": buy_oid,
            "sell_oid": sell_oid,
            "buy_source": deepcopy(buy),
            "sell_source": deepcopy(sell),
            "buy_cancelled": False,
            "sell_cancelled": False,
        }
        row["p7_restructure_intent"] = intent

    requests: list[dict[str, int]] = []
    for side in ("buy", "sell"):
        if intent.get(f"{side}_cancelled"):
            continue
        oid = intent.get(f"{side}_oid")
        if oid is not None:
            requests.append({"coin": ctx["coin"], "oid": int(oid)})
    if requests:
        cancelled, errors = lifecycle_p7_cancel_result(ctx["exchange"], ctx["coin"], requests, cache)
        if errors:
            intent["last_error"] = "; ".join(errors)
            intent["last_attempt_at"] = ctx["now"]
        for side in ("buy", "sell"):
            oid = intent.get(f"{side}_oid")
            if oid is not None and int(oid) in cancelled:
                intent[f"{side}_cancelled"] = True
        if not cancelled:
            return True, 0

    buy_cancelled = bool(intent.get("buy_cancelled"))
    sell_cancelled = bool(intent.get("sell_cancelled"))
    if buy_cancelled != sell_cancelled:
        restored_side = "buy" if buy_cancelled else "sell"
        source = intent.get(f"{restored_side}_source")
        if isinstance(source, dict):
            restored_oid = intent.get(f"{restored_side}_oid")
            levels[:] = [
                entry for entry in levels
                if not isinstance(entry, dict) or entry.get("oid") != restored_oid
            ]
            levels.append(lifecycle_p7_source_snapshot(source))
        row.pop("p7_restructure_intent", None)
        return True, 1
    if not (buy_cancelled and sell_cancelled):
        return True, 0

    buy_source = intent.get("buy_source")
    sell_source = intent.get("sell_source")
    source_oids = {intent.get("buy_oid"), intent.get("sell_oid")}
    levels[:] = [
        entry for entry in levels
        if not isinstance(entry, dict) or entry.get("oid") not in source_oids
    ]

    def restore_both_sources() -> None:
        for source in (buy_source, sell_source):
            if isinstance(source, dict):
                levels.append(lifecycle_p7_source_snapshot(source))

    buy_price = decimal_or_none(buy_source.get("price")) if isinstance(buy_source, dict) else None
    sell_price = decimal_or_none(sell_source.get("price")) if isinstance(sell_source, dict) else None
    if buy_price is None or sell_price is None or sell_price <= buy_price:
        row["p7_restructure_error"] = "invalid source price span"
        restore_both_sources()
        row.pop("p7_restructure_intent", None)
        return True, 2
    spread = sell_price - buy_price
    half = spread / Decimal("2")
    sz_decimals = int(row.get("sz_decimals") or ctx["asset"]["szDecimals"])
    new_buy_price = rounded_perp_price(ctx["current_mid"] - half, sz_decimals)
    new_sell_price = rounded_perp_price(ctx["current_mid"] + half, sz_decimals)
    if new_buy_price <= 0 or new_buy_price >= new_sell_price:
        row["p7_restructure_error"] = "restructured prices are not tradable"
        restore_both_sources()
        row.pop("p7_restructure_intent", None)
        return True, 2
    buy_size = decimal_or_none(buy_source.get("size")) if isinstance(buy_source, dict) else None
    sell_size = decimal_or_none(sell_source.get("size")) if isinstance(sell_source, dict) else None
    if buy_size is None or sell_size is None or buy_size <= 0 or sell_size <= 0:
        row["p7_restructure_error"] = "invalid source size"
        restore_both_sources()
        row.pop("p7_restructure_intent", None)
        return True, 2
    step = Decimal(1).scaleb(-sz_decimals)
    paired_size = (min(buy_size, sell_size) / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if paired_size <= 0:
        row["p7_restructure_error"] = "paired source size rounds to zero"
        restore_both_sources()
        row.pop("p7_restructure_intent", None)
        return True, 2
    buy_residual_size = buy_size - paired_size
    sell_residual_size = sell_size - paired_size
    new_orders = [
        (
            grid_order_entry(row, ctx["coin"], ctx["asset"], True, new_buy_price, bool(buy_source.get("reduce_only")), size=paired_size, gap=Decimal(str(row["gap_rate"])), preserve_size=True),
            buy_source,
        ),
        (
            grid_order_entry(row, ctx["coin"], ctx["asset"], False, new_sell_price, bool(sell_source.get("reduce_only")), size=paired_size, gap=Decimal(str(row["gap_rate"])), preserve_size=True),
            sell_source,
        ),
    ]
    for order, source in new_orders:
        order["grid_leg"] = 1
        order["p7_restructure"] = True
        order["p7_source_buy_oid"] = intent.get("buy_oid")
        order["p7_source_sell_oid"] = intent.get("sell_oid")
        order["economic_chain_id"] = economic_chain_id(row, source)
        order["status"] = GRID_CHAIN_DEBT_STATUS
        order["chain_debt_at"] = ctx["now"]
        lifecycle_assign_p3_queue_seq(order, cache)
        levels.append(order)
    for source, residual_size, source_price in (
        (buy_source, buy_residual_size, buy_price),
        (sell_source, sell_residual_size, sell_price),
    ):
        if residual_size > 0:
            lifecycle_accumulate_p8_partial_debt(
                row,
                source,
                residual_size,
                source_price,
                ctx["now"],
            )
    row["p7_restructure"] = {
        "at": ctx["now"],
        "mid": decimal_to_plain(ctx["current_mid"]),
        "spread": decimal_to_plain(spread),
        "size": decimal_to_plain(paired_size),
        "buy_residual_size": decimal_to_plain(buy_residual_size),
        "sell_residual_size": decimal_to_plain(sell_residual_size),
        "buy_price": decimal_to_plain(new_buy_price),
        "sell_price": decimal_to_plain(new_sell_price),
    }
    row.pop("p7_restructure_intent", None)
    return True, 2


def grid_order_far_from_mid(entry: dict[str, Any], current_mid: Decimal, rate: Decimal) -> bool:
    price = decimal_or_none(entry.get("price", entry.get("limit_px")))
    if price is None or price <= 0 or current_mid <= 0:
        return False
    side = str(entry.get("side") or "")
    if side == "buy":
        return price < current_mid * (Decimal("1") - rate)
    if side == "sell":
        return price > current_mid * (Decimal("1") + rate)
    return False


def restore_pending_cancel_entries(
    row: dict[str, Any],
    current_mid: Decimal,
    cache: dict[str, Any] | None,
    now: int,
) -> int:
    rate = pending_cancel_rate(action_limit_deficit(cache))
    restored = 0
    for entry in row.get("levels") or []:
        if not isinstance(entry, dict) or str(entry.get("status")) != GRID_PENDING_CANCEL_STATUS:
            continue
        if entry.get("oid") is None:
            entry["status"] = "cancelled"
            entry["cancelled_at"] = now
            entry.pop("pending_cancel_reason", None)
            entry.pop("pending_cancel_at", None)
            entry.pop("pending_cancel_mid", None)
            entry.pop("pending_cancel_rate", None)
            restored += 1
            continue
        if rate is not None and grid_order_far_from_mid(entry, current_mid, rate):
            continue
        entry["status"] = "active"
        entry["pending_cancel_restored_at"] = now
        entry.pop("pending_cancel_reason", None)
        entry.pop("pending_cancel_at", None)
        entry.pop("pending_cancel_mid", None)
        entry.pop("pending_cancel_rate", None)
        audit_grid_action(
            "pending_cancel_restore",
            coin=row.get("coin"),
            side=entry.get("side"),
            oid=entry.get("oid"),
            price=entry.get("price", entry.get("limit_px")),
            deficit=action_limit_deficit(cache),
        )
        restored += 1
    return restored


def is_cumulative_action_limit_text(text: str) -> bool:
    lowered = text.lower()
    return "too many cumulative requests" in lowered and "cumulative volume traded" in lowered


def clear_cumulative_action_limit_retry_cache(cache: dict[str, Any] | None) -> None:
    if not isinstance(cache, dict):
        return
    cache.pop("action_limit_error", None)
    cache.pop("action_limit_at", None)
    cache.pop("action_limit_deficit", None)
    cache.pop("action_limit_raw_deficit", None)
    rate_cache = cache.get("user_action_rate_limits")
    if isinstance(rate_cache, dict):
        rate_cache.clear()


def submit_with_cumulative_action_limit_wait(
    submit: Any,
    *,
    cache: dict[str, Any] | None = None,
    on_wait: Any = None,
) -> Any:
    """Retry emergency actions every 10 seconds until cumulative capacity opens."""
    wait_count = 0
    while True:
        try:
            result = submit()
        except RuntimeError as exc:
            error_text = str(exc)
            if not is_cumulative_action_limit_text(error_text):
                raise
        else:
            error_text = str(result)
            if not is_cumulative_action_limit_text(error_text):
                return result
        wait_count += 1
        if callable(on_wait):
            on_wait(wait_count, error_text)
        time.sleep(GRID_EMERGENCY_ACTION_LIMIT_WAIT_SECONDS)
        clear_cumulative_action_limit_retry_cache(cache)


def is_temporary_action_limit_text(text: str) -> bool:
    lowered = text.lower()
    return is_cumulative_action_limit_text(text) or any(
        pattern in lowered
        for pattern in ("action limit", "rate limit", "too many requests", "status 429", "status_code=429")
    )


def action_limit_error(cache: dict[str, Any] | None) -> str | None:
    if not isinstance(cache, dict):
        return None
    error = cache.get("action_limit_error")
    return str(error) if error else None


def mark_action_limit_hit(cache: dict[str, Any] | None, error_text: str, now: int) -> None:
    if not isinstance(cache, dict):
        return
    cache["action_limit_error"] = error_text
    cache["action_limit_at"] = now
    cache.setdefault("action_limit_p1_budget_remaining", 0)


def pause_grid_order_for_action_limit(
    order: dict[str, Any],
    now: int,
    error_text: str,
    old_oid: int | None = None,
) -> None:
    order["action_limit_deferred_status"] = order.get("status")
    order["last_error"] = error_text
    order["action_limit_deferred_at"] = now
    if old_oid is not None:
        order["action_limit_deferred_oid"] = old_oid


def action_limit_p1_budget_remaining(cache: dict[str, Any] | None) -> int | None:
    if not isinstance(cache, dict):
        return 0
    if "action_limit_p1_budget_remaining" not in cache:
        return None
    try:
        return max(0, int(cache.get("action_limit_p1_budget_remaining") or 0))
    except (TypeError, ValueError):
        return 0


def action_limit_p1_enabled(cache: dict[str, Any] | None) -> bool:
    return bool(isinstance(cache, dict) and cache.get("action_limit_p1_enabled"))


def consume_action_limit_p1_budget(cache: dict[str, Any] | None) -> None:
    remaining = action_limit_p1_budget_remaining(cache)
    if remaining is None or not isinstance(cache, dict):
        return
    cache["action_limit_p1_budget_remaining"] = max(0, remaining - 1)


def consume_action_limit_headroom(cache: dict[str, Any] | None, count: int = 1) -> None:
    if not isinstance(cache, dict) or "action_limit_headroom" not in cache:
        return
    try:
        headroom = int(cache.get("action_limit_headroom") or 0)
    except (TypeError, ValueError):
        headroom = 0
    remaining_headroom = max(0, headroom - max(0, count))
    cache["action_limit_headroom"] = remaining_headroom
    if "action_limit_p1_budget_remaining" in cache:
        try:
            p1_remaining = max(0, int(cache.get("action_limit_p1_budget_remaining") or 0))
        except (TypeError, ValueError):
            p1_remaining = 0
        cache["action_limit_p1_budget_remaining"] = min(
            p1_remaining,
            max(0, remaining_headroom - 1),
        )


def reserve_grid_exchange_actions(
    cache: dict[str, Any] | None,
    count: int = 1,
    *,
    consume_p1_budget: bool = False,
) -> None:
    """Reserve address action capacity immediately before an exchange call."""
    count = max(0, count)
    if count == 0 or not isinstance(cache, dict):
        return
    if consume_p1_budget and p1_budget_tracked(cache):
        if not action_limit_p1_enabled(cache):
            raise GridActionBudgetUnavailable("P1 action budget is not enabled")
        remaining = action_limit_p1_budget_remaining(cache) or 0
        if remaining < count:
            raise GridActionBudgetUnavailable(
                f"P1 action budget exhausted before exchange submit: required={count} remaining={remaining}"
            )
        cache["action_limit_p1_budget_remaining"] = remaining - count
    consume_action_limit_headroom(cache, count)


def enable_action_limit_p1_budget(cache: dict[str, Any] | None) -> None:
    if isinstance(cache, dict):
        cache["action_limit_p1_enabled"] = True


def p1_budget_available(cache: dict[str, Any] | None) -> bool:
    remaining = action_limit_p1_budget_remaining(cache)
    return remaining is None or remaining > 0


def p1_budget_tracked(cache: dict[str, Any] | None) -> bool:
    return isinstance(cache, dict) and "action_limit_p1_budget_remaining" in cache


def noncritical_grid_work_allowed(cache: dict[str, Any] | None) -> bool:
    if isinstance(cache, dict) and cache.get("grid_action_phase") not in (None, GRID_ACTION_PHASE_P2):
        return False
    if action_limit_error(cache):
        return False
    if not isinstance(cache, dict) or "action_limit_headroom" not in cache:
        return True
    try:
        return int(cache.get("action_limit_headroom") or 0) > GRID_ACTION_LIMIT_P2_HEADROOM_THRESHOLD
    except (TypeError, ValueError):
        return False


def action_limit_p1_budget_for_deficit(deficit: int) -> int:
    if deficit < 3:
        return GRID_ACTION_LIMIT_P1_BUDGET_PER_RUN
    probability = Decimal("1") / Decimal(str(math.log(deficit)))
    return GRID_ACTION_LIMIT_P1_BUDGET_PER_RUN if random.random() < float(probability) else 0


def action_limit_p1_budget_for_headroom(headroom: int) -> int:
    return max(GRID_ACTION_LIMIT_P1_BUDGET_PER_RUN, headroom - 1)


def user_action_rate_limit(info: Any, account: str, cache: dict[str, Any], network: str) -> dict[str, Any] | None:
    rate_cache = cache.setdefault("user_action_rate_limits", {})
    rate_key = (network, account)
    if rate_key not in rate_cache:
        try:
            result = info.post("/info", {"type": "userRateLimit", "user": account})
        except Exception as exc:
            log_event("user_action_rate_limit_error", {"type": type(exc).__name__, "message": str(exc)})
            result = None
        else:
            log_event("user_action_rate_limit", result)
        rate_cache[rate_key] = result if isinstance(result, dict) else None
    return rate_cache[rate_key]


def precheck_action_limit(info: Any, account: str, cache: dict[str, Any], network: str, now: int) -> str | None:
    existing = action_limit_error(cache)
    if existing:
        return existing
    rate = user_action_rate_limit(info, account, cache, network)
    if not isinstance(rate, dict):
        return None
    try:
        used = int(rate.get("nRequestsUsed") or 0)
        cap = int(rate.get("nRequestsCap") or 0)
    except (TypeError, ValueError):
        return None
    cache["action_limit_raw_deficit"] = used - cap if cap > 0 else 0
    cache["action_limit_deficit"] = max(0, used - cap) if cap > 0 else 0
    audit_snapshots = cache.setdefault("rate_limit_audit_snapshots", {})
    audit_key = (network, account)
    snapshot = (used, cap)
    if audit_snapshots.get(audit_key) != snapshot:
        audit_grid_action(
            "rate_limit_snapshot",
            network=network,
            nRequestsUsed=used,
            nRequestsCap=cap,
            deficit=cache["action_limit_deficit"],
        )
        audit_snapshots[audit_key] = snapshot
    if cap > 0 and used >= cap:
        deficit = used - cap
        error_text = (
            "address action limit exhausted before P1 submissions: "
            f"nRequestsUsed={used} nRequestsCap={cap} deficit={deficit}"
        )
        mark_action_limit_hit(cache, error_text, now)
        if not cache.get("action_limit_p1_budget_initialized"):
            cache["action_limit_p1_budget_remaining"] = action_limit_p1_budget_for_deficit(deficit)
            cache["action_limit_p1_budget_initialized"] = True
        return error_text
    if cap > 0 and not cache.get("action_limit_p1_budget_initialized"):
        headroom = max(0, cap - used)
        cache["action_limit_p1_budget_remaining"] = action_limit_p1_budget_for_headroom(headroom)
        cache["action_limit_p1_budget_initialized"] = True
        cache["action_limit_headroom"] = headroom
    return None


def batch_row_raw_coin(row: dict[str, Any]) -> str:
    coin = str(row.get("coin") or "")
    if ":" in coin:
        return coin
    raw_coin = str(row.get("raw_coin") or row["coin"])
    dex = str(row.get("dex") or "")
    if dex and ":" not in raw_coin:
        return f"{dex}:{raw_coin}"
    return raw_coin


def transient_error_status(exc: Exception) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if status_code is None and exc.args:
        status_code = exc.args[0]
    try:
        status_int = int(status_code)
    except (TypeError, ValueError):
        return 0 if is_transient_error_text(str(exc)) else None
    return status_int if status_int in TRANSIENT_STATUS_CODES else None


def is_transient_error_text(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in TRANSIENT_ERROR_TEXTS)


def append_rate_limit_log(row: dict[str, Any], status_code: int, exc: Exception) -> None:
    RATE_LIMIT_LOG_PATH.parent.mkdir(exist_ok=True)
    entry = {
        "ts": int(time.time()),
        "status_code": status_code,
        "type": row.get("type"),
        "id": row.get("id"),
        "coin": row.get("coin"),
        "oid": row.get("oid"),
        "network": row.get("network", "mainnet"),
        "account": mask(str(row.get("account", ""))) if row.get("account") else "",
        "stop_px": row.get("stop_px"),
        "best_px": row.get("best_px"),
        "last_mid_px": row.get("last_mid_px"),
        "error": str(exc),
    }
    with RATE_LIMIT_LOG_PATH.open("a") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def transient_note(status_code: int) -> str:
    if status_code in TRANSIENT_STATUS_CODES:
        return f"transient HTTP {status_code}; will retry"
    return "transient network error; will retry"


def is_isolated_opening_leverage_error_text(text: str) -> bool:
    lowered = text.lower()
    return (
        "failed to set isolated opening leverage" in lowered
        and "order was not submitted" in lowered
    )


def is_grid_nonpositive_price_error_text(text: str) -> bool:
    """Return whether a dynamic grid gap temporarily produced no valid price."""
    return "price must be positive" in text.lower()


def is_grid_qualified_coin_key_error_text(text: str) -> bool:
    """Recognize a bare KeyError for a qualified DEX coin."""
    stripped = text.strip()
    if len(stripped) < 5 or stripped[0] != stripped[-1] or stripped[0] not in {"'", '"'}:
        return False
    coin = stripped[1:-1]
    dex, separator, symbol = coin.partition(":")
    return separator == ":" and dex in {"xyz", "mkts"} and bool(symbol) and not any(
        character.isspace() for character in coin
    )


def grid_row_recoverable_from_error(row: dict[str, Any]) -> bool:
    if row.get("type") != "grid":
        return False
    if row.get("status") == "active":
        return True
    if row.get("status") != "error":
        return False
    error_text = " ".join(str(row.get(key, "")) for key in ("error", "last_error", "note"))
    raw_error_texts = {str(row.get(key, "")).strip() for key in ("error", "last_error")}
    bare_coin_key_errors = {
        repr(str(row.get("coin") or "")),
        repr(str(row.get("raw_coin") or "")),
        repr(batch_row_raw_coin(row)),
    }
    if (
        is_post_only_reject_text(error_text)
        or is_transient_error_text(error_text)
        or is_min_order_value_error_text(error_text)
        or is_reduce_only_would_increase_text(error_text)
        or is_insufficient_margin_text(error_text)
        or is_cancel_terminal_race_text(error_text)
        or is_isolated_opening_leverage_error_text(error_text)
        or is_grid_nonpositive_price_error_text(error_text)
        or any(is_grid_qualified_coin_key_error_text(text) for text in raw_error_texts)
        or (
            "unknown perp coin" in error_text.lower()
            and batch_row_raw_coin(row) != str(row.get("raw_coin") or row.get("coin") or "")
        )
        or bool(raw_error_texts & bare_coin_key_errors)
    ):
        return True
    for entry in row.get("levels") or []:
        if isinstance(entry, dict) and is_post_only_reject_text(str(entry.get("error", ""))):
            return True
    return False


def is_min_order_value_error_text(text: str) -> bool:
    lowered = text.lower()
    return "minimum value" in lowered or "min value" in lowered or "mintradentlrejected" in lowered


def is_reduce_only_would_increase_text(text: str) -> bool:
    lowered = text.lower()
    return "reduce only" in lowered and "increase position" in lowered


def is_insufficient_margin_text(text: str) -> bool:
    return "insufficient margin" in text.lower()


def is_cancel_terminal_race_text(text: str) -> bool:
    """Return whether a cancel lost a race to an exchange terminal state."""
    lowered = text.lower()
    return (
        "order was never placed, already canceled, or filled" in lowered
        or "order was never placed, already cancelled, or filled" in lowered
        or "order was already filled" in lowered
        or "order was already canceled" in lowered
        or "order was already cancelled" in lowered
    )


def is_grid_child_order_reject_text(text: str) -> bool:
    return "failed to submit grid child order:" in text.lower()


def skip_grid_exchange_reject(order: dict[str, Any], error_text: str, now: int) -> bool:
    if not is_grid_child_order_reject_text(error_text):
        return False
    order["status"] = "skipped_exchange_reject"
    order["oid"] = None
    order["last_error"] = error_text
    order["skipped_at"] = now
    return True


def grid_margin_pause_active(
    row: dict[str, Any],
    side: str,
    now: int,
    position_value: Decimal,
    position_size: Decimal,
) -> bool:
    pauses = row.get("margin_pauses")
    if not isinstance(pauses, dict):
        return False
    pause = pauses.get(side)
    if not isinstance(pause, dict):
        return False

    paused_at = int(pause.get("paused_at") or 0)
    paused_position_value = decimal_or_none(pause.get("position_value"))
    stale_run = paused_at != now
    position_reduced = paused_position_value is not None and position_value < paused_position_value
    if stale_run or position_reduced:
        pauses.pop(side, None)
        if not pauses:
            row.pop("margin_pauses", None)
        return False
    return True


def account_spot_withdrawable(
    info: Any,
    account: str,
    network: str,
    cache: dict[str, Any],
) -> tuple[Decimal, Decimal] | None:
    states = cache.setdefault("spot_user_states", {})
    cache_key = (network, account)
    if cache_key not in states:
        try:
            states[cache_key] = info.spot_user_state(account)
        except Exception as exc:
            log_event("spot_user_state_error", {"type": type(exc).__name__, "message": str(exc)})
            states[cache_key] = None
    state = states[cache_key]
    if not isinstance(state, dict):
        return None
    for balance in state.get("balances", []):
        if not isinstance(balance, dict):
            continue
        if balance.get("token") != 0 and str(balance.get("coin", "")).upper() != "USDC":
            continue
        total = decimal_or_none(balance.get("total"))
        hold = decimal_or_none(balance.get("hold"))
        if total is None or hold is None:
            return None
        return max(Decimal("0"), total - hold), total
    return None


def account_withdrawable_reduce_only(withdrawable: Decimal | None) -> bool:
    return withdrawable is not None and withdrawable < GRID_WITHDRAWABLE_REDUCE_ONLY_THRESHOLD


def account_withdrawable_pause_active(withdrawable: Decimal | None) -> bool:
    return withdrawable is not None and withdrawable < GRID_WITHDRAWABLE_PAUSE_THRESHOLD


def account_withdrawable_pause_phase(withdrawable: Decimal | None) -> str | None:
    if withdrawable is None or withdrawable >= GRID_WITHDRAWABLE_PAUSE_THRESHOLD:
        return None
    if withdrawable == 0:
        return GRID_ACTION_PHASE_P0
    if withdrawable < GRID_WITHDRAWABLE_REDUCE_ONLY_THRESHOLD:
        return GRID_ACTION_PHASE_P1_WITHDRAWABLE
    return GRID_ACTION_PHASE_P2


def grid_limit_chase_direction(
    row: dict[str, Any],
    position_size: Decimal,
    position_value: Decimal,
) -> bool | None:
    """Return the market-chase direction for a signed --limit breach."""
    if grid_limit_policy_from_row(row) != "limit":
        return None
    minimum = Decimal(str(row.get("min_position_value") or "0"))
    maximum = Decimal(str(row.get("max_position_value") or "0"))
    lower_bound, upper_bound = grid_position_bounds("limit", minimum, maximum)
    signed_value = signed_position_value(position_size, position_value)
    if signed_value < lower_bound:
        return True
    if signed_value > upper_bound:
        return False
    return None


def grid_limit_chase_reduction_target_size(
    row: dict[str, Any],
    position_size: Decimal,
    position_value: Decimal,
    current_mid: Decimal,
    is_buy: bool,
    sz_decimals: int,
) -> Decimal | None:
    """Return the one-shot P4 reduction needed to reach the nearest limit bound."""
    if current_mid <= 0 or position_size == 0:
        return None
    if (is_buy and position_size >= 0) or (not is_buy and position_size <= 0):
        return None
    minimum = Decimal(str(row.get("min_position_value") or "0"))
    maximum = Decimal(str(row.get("max_position_value") or "0"))
    lower_bound, upper_bound = grid_position_bounds("limit", minimum, maximum)
    signed_value = signed_position_value(position_size, position_value)
    excess_value = lower_bound - signed_value if is_buy else signed_value - upper_bound
    if excess_value <= 0:
        return None
    step = Decimal(1).scaleb(-sz_decimals)
    return (excess_value / current_mid / step).to_integral_value(rounding=ROUND_CEILING) * step


def record_grid_limit_chase_candidate(
    cache: dict[str, Any],
    row: dict[str, Any],
    position_size: Decimal,
    position_value: Decimal,
) -> None:
    is_buy = grid_limit_chase_direction(row, position_size, position_value)
    if is_buy is None:
        return
    # P4 is reduction-only. Position deficits that would require adding risk
    # are handled exclusively by P10 through promotion of an existing order.
    if (is_buy and position_size >= 0) or (not is_buy and position_size <= 0):
        return
    seen = cache.setdefault("limit_chase_candidate_ids", set())
    row_id = id(row)
    if row_id in seen:
        return
    seen.add(row_id)
    cache.setdefault("limit_chase_candidates", []).append(
        {
            "row": row,
            "startup_is_buy": is_buy,
            "startup_position_size": decimal_to_plain(position_size),
            "startup_position_value": decimal_to_plain(
                signed_position_value(position_size, position_value)
            ),
        }
    )


def grid_reduce_only_capacity_available(
    row: dict[str, Any],
    order: dict[str, Any],
    position_size: Decimal,
    position_value: Decimal,
    *,
    withdrawable_protected_restore: bool = False,
) -> bool:
    if not bool(order.get("reduce_only", False)):
        return True
    requested_size = decimal_or_none(order.get("size")) or Decimal("0")
    if requested_size <= 0 or position_size == 0:
        return False
    reserved_size = Decimal("0")
    reserved_notional = Decimal("0")
    for entry in active_grid_entries(row, str(order.get("side"))):
        if entry is order:
            continue
        if not bool(entry.get("reduce_only", False)) and not (
            withdrawable_protected_restore
            and grid_order_reduces_position(entry, position_size)
        ):
            continue
        entry_size = decimal_or_none(entry.get("size")) or Decimal("0")
        entry_price = decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0")
        reserved_size += entry_size
        reserved_notional += entry_size * entry_price
    if reserved_size + requested_size > abs(position_size):
        return False
    if withdrawable_protected_restore:
        return True
    policy = grid_limit_policy_from_row(row)
    min_position_value = Decimal(str(row.get("min_position_value") or "0"))
    max_position_value = Decimal(str(row.get("max_position_value") or "0"))
    requested_price = decimal_or_none(order.get("price", order.get("limit_px"))) or Decimal("0")
    requested_notional = requested_size * requested_price
    if policy != "limit":
        position_matches_target = (policy == "long" and position_size > 0) or (policy == "short" and position_size < 0)
        minimum = min_position_value if position_matches_target else Decimal("0")
        return position_value - reserved_notional - requested_notional >= minimum

    lower_bound, upper_bound = grid_position_bounds(policy, min_position_value, max_position_value)
    signed_value = signed_position_value(position_size, position_value)
    if position_size < 0:
        projected_value = signed_value + reserved_notional + requested_notional
    else:
        projected_value = signed_value - reserved_notional - requested_notional
    return lower_bound <= projected_value <= upper_bound


def grid_order_reduces_position(order: dict[str, Any], position_size: Decimal) -> bool:
    side = str(order.get("side") or "")
    if side not in {"buy", "sell"} or position_size == 0:
        return False
    is_buy = bool(order.get("is_buy")) if order.get("is_buy") is not None else side == "buy"
    return not grid_order_would_add_risk(position_size, is_buy)


def withdrawable_protected_paused_restore(
    entry: dict[str, Any],
    position_size: Decimal,
    account_margin_protected: bool,
) -> bool:
    return account_margin_protected and grid_order_reduces_position(entry, position_size)


def withdrawable_protected_restore_submission_available(
    cache: dict[str, Any],
    network: str,
    account: Any,
    coin: str,
    side: str,
) -> bool:
    submitted = cache.setdefault("withdrawable_protected_restore_submissions", set())
    return (network, str(account), coin, side) not in submitted


def mark_withdrawable_protected_restore_submitted(
    cache: dict[str, Any],
    network: str,
    account: Any,
    coin: str,
    side: str,
) -> None:
    submitted = cache.setdefault("withdrawable_protected_restore_submissions", set())
    submitted.add((network, str(account), coin, side))


def pause_grid_margin_side(
    row: dict[str, Any],
    side: str,
    now: int,
    position_value: Decimal,
) -> None:
    pauses = row.setdefault("margin_pauses", {})
    pauses[side] = {
        "paused_at": now,
        "position_value": decimal_to_plain(position_value),
    }


def clear_stale_grid_margin_pauses(row: dict[str, Any], now: int) -> bool:
    pauses = row.get("margin_pauses")
    if not isinstance(pauses, dict):
        return False
    stale_sides = [
        side
        for side, pause in pauses.items()
        if not isinstance(pause, dict) or int(pause.get("paused_at") or 0) != now
    ]
    for side in stale_sides:
        pauses.pop(side, None)
    if not pauses:
        row.pop("margin_pauses", None)
    return bool(stale_sides)


def pause_grid_margin_side_entries(row: dict[str, Any], side: str, now: int, error_text: str) -> int:
    paused = 0
    for entry in row.get("levels") or []:
        if not isinstance(entry, dict) or str(entry.get("side") or "") != side:
            continue
        status = str(entry.get("status", "active"))
        has_oid = entry.get("oid") is not None
        if status == "active" and has_oid:
            continue
        if status not in {
            "active",
            "pending",
            "recovery_deferred",
            "paused_margin",
            "paused_reduce_capacity",
            GRID_REPLACEMENT_PAUSE_STATUS,
        }:
            continue
        entry["status"] = "paused_margin"
        entry["oid"] = None
        entry["last_error"] = error_text
        entry["paused_at"] = now
        paused += 1
    return paused


def find_current_position_from_state(state: dict[str, Any], coin: str) -> dict[str, Any] | None:
    for item in state.get("assetPositions", []):
        if not isinstance(item, dict):
            continue
        position = item.get("position", {})
        if not isinstance(position, dict) or not position_matches_coin(str(position.get("coin", "")), coin):
            continue
        size = decimal_or_none(position.get("szi")) or Decimal("0")
        if size != 0:
            return position
    return None


def best_bid_ask(info: Any, coin: str) -> tuple[Decimal | None, Decimal | None]:
    try:
        book = info.l2_snapshot(coin)
        log_event("grid_l2_snapshot", {"coin": coin, "book": book})
        levels = book.get("levels") if isinstance(book, dict) else None
        if not isinstance(levels, list) or len(levels) < 2:
            return None, None
        bid_level = levels[0][0] if isinstance(levels[0], list) and levels[0] else None
        ask_level = levels[1][0] if isinstance(levels[1], list) and levels[1] else None
        bid = decimal_or_none(bid_level.get("px")) if isinstance(bid_level, dict) else None
        ask = decimal_or_none(ask_level.get("px")) if isinstance(ask_level, dict) else None
        return bid, ask
    except Exception as exc:
        log_event("grid_l2_snapshot_error", {"coin": coin, "type": type(exc).__name__, "message": str(exc)})
        return None, None


def grid_reference_price(side: str, current_mid: Decimal, best_bid: Decimal | None, best_ask: Decimal | None) -> Decimal:
    if side == "buy" and best_bid is not None and best_bid > 0:
        return best_bid
    if side == "sell" and best_ask is not None and best_ask > 0:
        return best_ask
    return current_mid


def grid_recovery_price_would_cross_market(
    entry: dict[str, Any],
    current_mid: Decimal,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
) -> bool:
    price = decimal_or_none(entry.get("price", entry.get("limit_px")))
    if price is None or price <= 0:
        return False
    return grid_price_would_cross_market(str(entry.get("side") or ""), price, current_mid, best_bid, best_ask)


def grid_price_would_cross_market(
    side: str,
    price: Decimal,
    current_mid: Decimal,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
) -> bool:
    if side == "buy":
        reference = best_ask if best_ask is not None and best_ask > 0 else current_mid
        return price >= reference
    if side == "sell":
        reference = best_bid if best_bid is not None and best_bid > 0 else current_mid
        return price <= reference
    return False


def skip_stale_grid_recovery(
    entry: dict[str, Any],
    old_oid: int,
    now: int,
    current_mid: Decimal,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
) -> bool:
    if not grid_recovery_price_would_cross_market(entry, current_mid, best_bid, best_ask):
        return False
    price = decimal_or_none(entry.get("price", entry.get("limit_px")))
    entry["status"] = "skipped_stale_recovery"
    entry["oid"] = None
    entry["stale_recovery_oid"] = old_oid
    entry["stale_recovery_at"] = now
    entry["stale_recovery_mid"] = decimal_to_plain(current_mid)
    if price is not None:
        entry["stale_recovery_price"] = decimal_to_plain(price)
    if best_bid is not None:
        entry["stale_recovery_best_bid"] = decimal_to_plain(best_bid)
    if best_ask is not None:
        entry["stale_recovery_best_ask"] = decimal_to_plain(best_ask)
    entry["last_error"] = "missing order recovery skipped because saved price would immediately match current market"
    return True


def pause_reduce_only_canceled_entry(entry: dict[str, Any], old_oid: int, now: int) -> None:
    entry["status"] = "paused_reduce_capacity"
    entry["oid"] = None
    entry["reduce_only_canceled_oid"] = old_oid
    entry["reduce_only_canceled_at"] = now
    entry["last_error"] = "exchange canceled reduce-only order; waiting for restore when reduce capacity is available"
    entry["paused_at"] = now


def defer_paused_grid_restore_if_crossing(
    entry: dict[str, Any],
    now: int,
    current_mid: Decimal,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
) -> bool:
    if not grid_recovery_price_would_cross_market(entry, current_mid, best_bid, best_ask):
        return False
    price = decimal_or_none(entry.get("price", entry.get("limit_px")))
    entry["restore_deferred_at"] = now
    entry["restore_deferred_reason"] = "would_cross_market"
    entry["restore_deferred_mid"] = decimal_to_plain(current_mid)
    if price is not None:
        entry["restore_deferred_price"] = decimal_to_plain(price)
    if best_bid is not None:
        entry["restore_deferred_best_bid"] = decimal_to_plain(best_bid)
    if best_ask is not None:
        entry["restore_deferred_best_ask"] = decimal_to_plain(best_ask)
    return True


def grid_order_status_name(order_status: Any) -> str:
    if not isinstance(order_status, dict):
        return ""
    order = order_status.get("order")
    if isinstance(order, dict) and order.get("status") is not None:
        return str(order.get("status") or "")
    if order_status.get("status") is not None:
        return str(order_status.get("status") or "")
    return ""


def grid_order_status_is_cancelled(order_status: Any) -> bool:
    status_name = grid_order_status_name(order_status).strip().lower()
    return status_name.endswith(("cancel", "canceled", "cancelled"))


def grid_order_status_partial_fill_size(order_status: Any) -> Decimal | None:
    """Return exchange-confirmed filled size when a terminal order was partial."""
    if not isinstance(order_status, dict):
        return None
    status_order = order_status.get("order")
    if not isinstance(status_order, dict):
        return None
    exchange_order = status_order.get("order")
    if not isinstance(exchange_order, dict):
        exchange_order = status_order
    original_size = decimal_or_none(exchange_order.get("origSz"))
    remaining_size = decimal_or_none(exchange_order.get("sz"))
    if (
        original_size is None
        or remaining_size is None
        or original_size <= 0
        or remaining_size <= 0
        or remaining_size >= original_size
    ):
        return None
    return original_size - remaining_size


def grid_order_status_partial_remainder(
    order_status: Any,
) -> tuple[Decimal, Decimal | None] | None:
    """Return exchange-confirmed unfilled size and saved limit price."""
    if not isinstance(order_status, dict):
        return None
    status_order = order_status.get("order")
    if not isinstance(status_order, dict):
        return None
    exchange_order = status_order.get("order")
    if not isinstance(exchange_order, dict):
        exchange_order = status_order
    original_size = decimal_or_none(exchange_order.get("origSz"))
    remaining_size = decimal_or_none(exchange_order.get("sz"))
    if (
        original_size is None
        or remaining_size is None
        or original_size <= 0
        or remaining_size <= 0
        or remaining_size >= original_size
    ):
        return None
    return remaining_size, decimal_or_none(exchange_order.get("limitPx"))


def mark_pending_cancel_confirmed_cancelled(
    entry: dict[str, Any],
    old_oid: int,
    now: int,
    order_status: Any,
) -> bool:
    if str(entry.get("status")) != GRID_PENDING_CANCEL_STATUS:
        return False
    if not grid_order_status_is_cancelled(order_status):
        return False
    entry["status"] = "cancelled"
    entry["oid"] = None
    entry["cancelled_oid"] = old_oid
    entry["cancelled_at"] = now
    entry["exchange_cancel_status"] = grid_order_status_name(order_status)
    entry.pop("pending_cancel_reason", None)
    entry.pop("pending_cancel_at", None)
    entry.pop("pending_cancel_mid", None)
    entry.pop("pending_cancel_rate", None)
    return True


def mark_missing_order_confirmed_open(
    entry: dict[str, Any],
    old_oid: int,
    now: int,
    order_status: Any,
    open_orders: list[dict[str, Any]] | None,
    coin: str,
) -> bool:
    if grid_order_status_name(order_status).strip().lower() != "open":
        return False
    entry["status"] = "active"
    entry["oid"] = old_oid
    entry["confirmed_open_oid"] = old_oid
    entry["confirmed_open_at"] = now
    record_submitted_open_grid_order(open_orders, coin, entry, old_oid, now)
    return True


def grid_entry_age_seconds(entry: dict[str, Any], now: int) -> int:
    for key in ("submitted_at", "recovered_at", "filled_at", "cancelled_at", "skipped_at", "paused_at"):
        try:
            value = int(entry.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return max(0, now - value)
    return 0


def skip_unknown_oid_grid_recovery(
    entry: dict[str, Any],
    old_oid: int,
    now: int,
    order_status: Any,
) -> bool:
    status_name = grid_order_status_name(order_status)
    if status_name != "unknownOid":
        return False
    age_seconds = grid_entry_age_seconds(entry, now)
    if age_seconds < GRID_UNKNOWN_OID_RECOVERY_MAX_AGE_SECONDS:
        return False
    entry["status"] = "skipped_unknown_oid"
    entry["oid"] = None
    entry["unknown_oid"] = old_oid
    entry["unknown_oid_at"] = now
    entry["unknown_oid_age_seconds"] = age_seconds
    entry["unknown_oid_status"] = status_name
    entry["last_error"] = "missing order recovery skipped because exchange returned unknownOid with no fill"
    return True


def find_open_order_by_oid(info: Any, account: str, dex: str, oid: int) -> dict[str, Any] | None:
    open_orders = collect_frontend_open_orders(info, account, dex)
    return next((order for order in open_orders if order.get("oid") is not None and int(order.get("oid", -1)) == oid), None)


def find_replacement_trail_order(info: Any, account: str, dex: str, row: dict[str, Any]) -> dict[str, Any] | None:
    target_trigger = Decimal(str(row["stop_px"]))
    target_size = Decimal(str(row["size"]))
    target_coin = str(row["coin"]).upper()
    target_side = str(row["side"]).upper()
    open_orders = collect_frontend_open_orders(info, account, dex)
    for order in open_orders:
        if str(order.get("coin", "")).upper() != target_coin:
            continue
        if str(order.get("side", "")).upper() != target_side:
            continue
        trigger_px = decimal_or_none(order.get("triggerPx"))
        if trigger_px is None or trigger_px != target_trigger:
            continue
        size = decimal_or_none(order.get("sz", order.get("origSz")))
        if size is not None and size != target_size:
            continue
        return order
    return None


def modify_trail_stop(
    row: dict[str, Any],
    mid_px: Decimal,
    api_cache: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    network = str(row.get("network") or "mainnet")
    timeout = float(row.get("timeout") or 20)
    raw_coin = batch_row_raw_coin(row)
    clients = (
        build_clients(network, timeout, raw_coin)
        if api_cache is None
        else build_worker_clients(api_cache, network, timeout, raw_coin)
    )
    info, exchange, account, _signer, _role = clients
    coin, asset = resolve_perp_asset(info, batch_row_raw_coin(row))
    dex = str(row.get("dex") or "")
    oid = int(row["oid"])

    if find_open_order_by_oid(info, account, dex, oid) is None:
        replacement = find_replacement_trail_order(info, account, dex, row)
        if replacement is not None and replacement.get("oid") is not None:
            oid = int(replacement["oid"])
            row["oid"] = oid
            row["note"] = "recovered replacement oid after modify"
            row["updated_at"] = int(time.time())
        else:
            row["status"] = "done"
            row["done_at"] = int(time.time())
            row["note"] = "order is no longer open"
            return row, True

    if find_open_order_by_oid(info, account, dex, oid) is None:
        row["status"] = "done"
        row["done_at"] = int(time.time())
        row["note"] = "order is no longer open"
        return row, True

    is_buy = bool(row["is_buy"])
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    best_px = Decimal(str(row["best_px"]))
    old_stop_px = Decimal(str(row["stop_px"]))
    distance = Decimal(str(row["trail_distance"]))

    if is_buy:
        best_px = min(best_px, mid_px)
    else:
        best_px = max(best_px, mid_px)

    new_stop_px = trail_stop_price(best_px, distance, is_buy, sz_decimals)
    should_modify = new_stop_px < old_stop_px if is_buy else new_stop_px > old_stop_px
    row["best_px"] = decimal_to_plain(best_px)
    row["last_mid_px"] = decimal_to_plain(mid_px)
    row["checked_at"] = int(time.time())
    if not should_modify:
        return row, True

    plan = build_trigger_order_plan(
        coin,
        is_buy,
        Decimal(str(row["amount"])),
        asset,
        exchange,
        Decimal(str(row.get("slippage") or DEFAULT_SLIPPAGE)),
        "trail-stop",
        rounded_perp_price(new_stop_px, sz_decimals),
        None,
        bool(row.get("reduce_only", True)),
        tpsl="sl",
        size=Decimal(str(row["size"])),
    )
    result = exchange.modify_order(
        oid,
        coin,
        is_buy,
        float(plan["size"]),
        float(plan["limit_px"]),
        plan["order_type"],
        reduce_only=bool(row.get("reduce_only", True)),
    )
    log_event("trail_modify_order", {"id": row.get("id"), "oid": oid, "stop_px": decimal_to_plain(new_stop_px), "result": result})
    if result.get("status") != "ok":
        if is_temporary_action_limit_text(str(result)):
            row["status"] = "active"
            row["last_error"] = str(result)
            row["note"] = "temporary action/rate limit; retrying"
            row["updated_at"] = int(time.time())
            return row, True
        row["status"] = "error"
        row["error"] = str(result)
        row["updated_at"] = int(time.time())
        return row, True

    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    status_errors = [str(status["error"]) for status in statuses if isinstance(status, dict) and status.get("error")]
    if status_errors:
        if all(is_temporary_action_limit_text(error) for error in status_errors):
            row["status"] = "active"
            row["last_error"] = str(statuses)
            row["note"] = "temporary action/rate limit; retrying"
            row["updated_at"] = int(time.time())
            return row, True
        row["status"] = "error"
        row["error"] = str(statuses)
        row["updated_at"] = int(time.time())
        return row, True

    row["stop_px"] = decimal_to_plain(new_stop_px)
    row["updated_at"] = int(time.time())
    row["plan"] = plan
    for status in statuses:
        if isinstance(status, dict) and "resting" in status and status["resting"].get("oid") is not None:
            row["oid"] = int(status["resting"]["oid"])
            break
        if isinstance(status, dict) and "filled" in status and status["filled"].get("oid") is not None:
            row["oid"] = int(status["filled"]["oid"])
            break
    return row, True


def open_order_oids(info: Any, account: str, dex: str, coin: str, open_orders: list[dict[str, Any]] | None = None) -> set[int]:
    oids: set[int] = set()
    for order in open_orders if open_orders is not None else collect_frontend_open_orders(info, account, dex):
        if not fill_matches_coin(str(order.get("coin", "")), coin):
            continue
        try:
            oids.add(int(order["oid"]))
        except (KeyError, TypeError, ValueError):
            continue
    return oids


def recent_fills_by_oid(
    info: Any,
    account: str,
    coin: str,
    start_ms: int,
    end_ms: int,
    fills: list[dict[str, Any]] | None = None,
) -> dict[int, dict[str, Any]]:
    if fills is None:
        fills = info.user_fills_by_time(account, start_ms, end_ms)
        log_event("grid_user_fills_by_time", {"coin": coin, "start_ms": start_ms, "end_ms": end_ms, "count": len(fills)})
    by_oid: dict[int, dict[str, Any]] = {}
    for fill in fills:
        if not isinstance(fill, dict) or not fill_matches_coin(str(fill.get("coin", "")), coin):
            continue
        try:
            oid = int(fill["oid"])
            fill_time = int(fill.get("time") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if fill_time < start_ms or fill_time > end_ms:
            continue
        old = by_oid.get(oid)
        if old is None or fill_time >= int(old.get("time") or 0):
            by_oid[oid] = fill
    return by_oid


def submit_grid_child_order(
    exchange: Any,
    coin: str,
    order: dict[str, Any],
    cache: dict[str, Any] | None = None,
    *,
    consume_p1_budget: bool = False,
    increment_iteration: bool = False,
) -> tuple[int | None, str, dict[str, Any] | None]:
    plan = order.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("grid child order is missing its saved plan")
    reserve_grid_exchange_actions(cache, consume_p1_budget=consume_p1_budget)
    if increment_iteration:
        order["iteration"] = lifecycle_iteration(order) + 1
    result = exchange.order(
        coin,
        bool(plan["is_buy"]),
        float(plan["size"]),
        float(plan["limit_px"]),
        plan["order_type"],
        reduce_only=bool(plan.get("reduce_only", False)),
    )
    audit_grid_action(
        "grid_order_submit",
        coin=coin,
        side="buy" if plan["is_buy"] else "sell",
        price=decimal_to_plain(plan["limit_px"]),
        size=decimal_to_plain(plan["size"]),
        reduce_only=bool(plan.get("reduce_only", False)),
        replacement=bool(order.get("replacement_order")),
        phase=order.get("audit_phase"),
        deficit=order.get("audit_deficit"),
        economic_chain_id=order.get("economic_chain_id") or order.get("birth_intent_cloid"),
        grid_id=order.get("grid_id"),
        birth_source=order.get("birth_source"),
        birth_slot=order.get("birth_slot"),
        grid_leg=order.get("grid_leg"),
        iteration=order.get("iteration"),
        source_oid=order.get("source_oid"),
        p9_source_oid=order.get("p9_source_oid"),
        p3_queue_seq=order.get("p3_queue_seq"),
        result=result,
    )
    log_event("grid_child_order", {"side": "buy" if plan["is_buy"] else "sell", "result": result})
    if result.get("status") != "ok":
        if is_post_only_reject_text(str(result)):
            raise GridPostOnlyRejected(str(result))
        raise RuntimeError(f"Failed to submit grid child order: {result}")
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    for status in statuses:
        if not isinstance(status, dict):
            continue
        if status.get("error"):
            if is_post_only_reject_text(str(status["error"])):
                raise GridPostOnlyRejected(str(status["error"]))
            raise RuntimeError(f"Failed to submit grid child order: {status['error']}")
        if isinstance(status.get("resting"), dict):
            return int(status["resting"]["oid"]), "active", status
        if isinstance(status.get("filled"), dict):
            return int(status["filled"].get("oid", 0)), "filled", status
    raise RuntimeError(f"Grid child order response did not include an order id: {result}")


def matching_open_grid_order(
    open_orders: list[dict[str, Any]] | None,
    coin: str,
    order: dict[str, Any],
    row: dict[str, Any],
) -> dict[str, Any] | None:
    if not open_orders:
        return None
    plan = order.get("plan")
    if not isinstance(plan, dict):
        return None
    desired_side = "B" if bool(plan.get("is_buy")) else "A"
    desired_price = decimal_or_none(plan.get("limit_px"))
    desired_size = decimal_or_none(plan.get("size"))
    desired_reduce_only = bool(plan.get("reduce_only", False))
    if desired_price is None or desired_size is None:
        return None
    tracked_oids = grid_batch_open_oids(row)
    try:
        current_oid = int(order.get("oid") or 0)
    except (TypeError, ValueError):
        current_oid = 0
    if current_oid:
        tracked_oids.discard(current_oid)
    for open_order in open_orders:
        if not isinstance(open_order, dict):
            continue
        if not position_matches_coin(str(open_order.get("coin", "")), coin):
            continue
        if str(open_order.get("side") or "") != desired_side:
            continue
        oid = open_order.get("oid")
        try:
            oid_int = int(oid)
        except (TypeError, ValueError):
            continue
        if oid_int in tracked_oids:
            continue
        open_price = decimal_or_none(open_order.get("limitPx"))
        open_size = decimal_or_none(open_order.get("sz"))
        if open_price != desired_price or open_size != desired_size:
            continue
        if bool(open_order.get("reduceOnly", False)) != desired_reduce_only:
            continue
        return open_order
    return None


def adopt_matching_open_grid_order(
    open_orders: list[dict[str, Any]] | None,
    coin: str,
    order: dict[str, Any],
    now: int,
    row: dict[str, Any],
) -> bool:
    open_order = matching_open_grid_order(open_orders, coin, order, row)
    if open_order is None:
        return False
    oid = int(open_order["oid"])
    order["oid"] = oid
    order["status"] = "active"
    order["submitted_at"] = now
    order["adopted_open_order_at"] = now
    order["last_submit_status"] = {
        "adopted_open_order": {
            "oid": oid,
            "limitPx": open_order.get("limitPx"),
            "sz": open_order.get("sz"),
            "side": open_order.get("side"),
            "timestamp": open_order.get("timestamp"),
        }
    }
    log_event(
        "grid_child_order_adopted",
        {
            "coin": coin,
            "side": order.get("side"),
            "oid": oid,
            "price": order.get("price", order.get("limit_px")),
            "size": order.get("size"),
        },
    )
    return True


def record_submitted_open_grid_order(
    open_orders: list[dict[str, Any]] | None,
    coin: str,
    order: dict[str, Any],
    oid: int | None,
    now: int,
) -> None:
    if open_orders is None or oid is None or oid <= 0:
        return
    plan = order.get("plan")
    if not isinstance(plan, dict):
        return
    open_orders.append(
        {
            "coin": coin,
            "side": "B" if bool(plan.get("is_buy")) else "A",
            "limitPx": str(plan.get("limit_px")),
            "sz": str(plan.get("size")),
            "oid": oid,
            "timestamp": now * 1000,
            "reduceOnly": bool(plan.get("reduce_only", False)),
        }
    )


def ensure_grid_order_min_notional(row: dict[str, Any], asset: dict[str, Any], order: dict[str, Any]) -> None:
    if bool(order.get("replace_never_cancel")):
        return
    plan = order.get("plan")
    if not isinstance(plan, dict):
        return
    price = decimal_or_none(order.get("price", order.get("limit_px"))) or decimal_or_none(plan.get("limit_px"))
    size = decimal_or_none(order.get("size")) or decimal_or_none(plan.get("size"))
    if price is None or price <= 0 or size is None or size <= 0:
        return
    min_notional = Decimal(str(row.get("min_order_value") or "10"))
    next_size = grid_size_for_min_notional(size, price, int(asset["szDecimals"]), min_notional)
    if next_size <= size:
        return
    order["size"] = decimal_to_plain(next_size)
    plan["size"] = next_size
    notional = next_size * Decimal(str(plan.get("limit_px", price)))
    plan["notional"] = notional
    plan["target_notional"] = notional
    plan["worst_notional"] = notional
    order["resized_min_notional_at"] = int(time.time())
    order["resized_min_notional_from"] = decimal_to_plain(size)


def bump_grid_order_size_one_step(asset: dict[str, Any], order: dict[str, Any]) -> None:
    if bool(order.get("replace_never_cancel")):
        return
    plan = order.get("plan")
    if not isinstance(plan, dict):
        return
    size = decimal_or_none(order.get("size")) or decimal_or_none(plan.get("size"))
    if size is None or size <= 0:
        return
    step = Decimal(1).scaleb(-int(asset["szDecimals"]))
    next_size = size + step
    order["size"] = decimal_to_plain(next_size)
    plan["size"] = next_size
    price = Decimal(str(plan.get("limit_px", order.get("price", order.get("limit_px", "0")))))
    notional = next_size * price
    plan["notional"] = notional
    plan["target_notional"] = notional
    plan["worst_notional"] = notional
    order["resized_min_retry_from"] = decimal_to_plain(size)


def refresh_grid_order_reduce_only(order: dict[str, Any], position_size: Decimal, policy: str) -> None:
    if bool(order.get("limit_chase_replacement")):
        set_grid_order_reduce_only(order, False)
        return
    reduce_only = grid_order_should_reduce_only(position_size, bool(order.get("is_buy")), policy)
    order["reduce_only"] = reduce_only
    plan = order.get("plan")
    if isinstance(plan, dict):
        plan["reduce_only"] = reduce_only


def set_grid_order_reduce_only(order: dict[str, Any], reduce_only: bool) -> None:
    order["reduce_only"] = reduce_only
    plan = order.get("plan")
    if isinstance(plan, dict):
        plan["reduce_only"] = reduce_only


def grid_reduce_only_canceled_restore_without_reduce_only(order: dict[str, Any]) -> bool:
    return str(order.get("status")) == "paused_reduce_capacity" and order.get("reduce_only_canceled_oid") is not None


def pause_refresh_reduce_only_replacement(entry: dict[str, Any], now: int) -> bool:
    if str(entry.get("status")) != "refresh_reduce_only" or not bool(entry.get("replacement_order")):
        return False
    entry["status"] = GRID_REPLACEMENT_PAUSE_STATUS
    entry["oid"] = None
    entry["replacement_order"] = True
    entry["replacement_pause_reason"] = "refresh_reduce_only"
    entry["refresh_reduce_only_paused_at"] = now
    entry.setdefault("paused_at", now)
    set_grid_order_reduce_only(entry, False)
    return True


def set_grid_order_tif(order: dict[str, Any], tif: str) -> None:
    plan = order.get("plan")
    if not isinstance(plan, dict):
        return
    plan["order_type"] = {"limit": {"tif": tif}}


def refresh_grid_order_tif(order: dict[str, Any]) -> None:
    permanent_replacement = bool(order.get("panic_reversal_order")) or bool(
        order.get("limit_chase_replacement")
    )
    set_grid_order_tif(order, "Gtc" if permanent_replacement else "Alo")


def set_grid_order_size_exact(order: dict[str, Any], size: Decimal) -> None:
    size_text = decimal_to_plain(size)
    order["size"] = size_text
    plan = order.get("plan")
    if not isinstance(plan, dict):
        return
    plan["size"] = size
    price = decimal_or_none(plan.get("limit_px")) or decimal_or_none(order.get("price")) or Decimal("0")
    notional = size * price
    plan["notional"] = notional
    plan["target_notional"] = notional
    plan["worst_notional"] = notional


def set_grid_order_price(order: dict[str, Any], price: Decimal) -> None:
    price_text = decimal_to_plain(price)
    order["limit_px"] = price_text
    order["price"] = price_text
    plan = order.get("plan")
    if not isinstance(plan, dict):
        return
    plan["limit_px"] = price
    plan["reference_price"] = price
    plan["min_value_price"] = price
    size = decimal_or_none(plan.get("size")) or decimal_or_none(order.get("size")) or Decimal("0")
    notional = size * price
    plan["notional"] = notional
    plan["target_notional"] = notional
    plan["worst_notional"] = notional


def next_outward_grid_price(row: dict[str, Any], asset: dict[str, Any], order: dict[str, Any]) -> Decimal | None:
    price = decimal_or_none(order.get("price", order.get("limit_px")))
    if price is None or price <= 0:
        return None
    plan = order.get("plan")
    gap = decimal_or_none(plan.get("grid_gap")) if isinstance(plan, dict) else None
    if gap is None or gap <= 0:
        gap = Decimal(str(row["gap_rate"]))
    is_buy = bool(order.get("is_buy"))
    multiplier = Decimal("1") - gap if is_buy else Decimal("1") + gap
    return rounded_perp_price(price * multiplier, int(row.get("sz_decimals") or asset["szDecimals"]))


def paused_only_grid_price_blocker(
    row: dict[str, Any],
    order: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the nearest paused-only spacing blocker for a fresh replacement."""
    if not bool(order.get("replacement_order")):
        return None
    side = str(order.get("side") or "")
    price = decimal_or_none(order.get("price", order.get("limit_px")))
    if not side or price is None or price <= 0:
        return None
    threshold = price * Decimal(str(row["gap_rate"])) * GRID_ALO_SPACING_MULTIPLIER
    blockers = [
        entry
        for entry in grid_price_occupancy_entries(row, side)
        if entry is not order
        and (existing := decimal_or_none(entry.get("price", entry.get("limit_px")))) is not None
        and existing > 0
        and abs(existing - price) <= threshold
    ]
    if not blockers or any(str(entry.get("status") or "") not in GRID_PAUSED_STATUSES for entry in blockers):
        return None
    return min(
        blockers,
        key=lambda entry: abs(
            (decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0")) - price
        ),
    )


def defer_fresh_replacement_behind_paused(
    row: dict[str, Any],
    asset: dict[str, Any],
    order: dict[str, Any],
    now: int,
    *,
    max_attempts: int = GRID_ALO_PRICE_ATTEMPT_LIMIT,
) -> bool:
    """Restore the paused blocker next and keep the fresh replacement as an outward paused marker."""
    blocker = paused_only_grid_price_blocker(row, order)
    if blocker is None:
        return False
    blocker_price = decimal_or_none(blocker.get("price", blocker.get("limit_px")))
    original_price = decimal_or_none(order.get("price", order.get("limit_px")))
    if blocker_price is None or blocker_price <= 0 or original_price is None or original_price <= 0:
        return False
    set_grid_order_price(order, blocker_price)
    for _attempt in range(max_attempts):
        candidate = next_outward_grid_price(row, asset, order)
        if candidate is None or candidate <= 0:
            set_grid_order_price(order, original_price)
            return False
        set_grid_order_price(order, candidate)
        if not active_price_too_close(
            row,
            str(order.get("side") or ""),
            candidate,
            exclude=order,
            spacing_multiplier=GRID_ALO_SPACING_MULTIPLIER,
        ):
            break
    else:
        set_grid_order_price(order, original_price)
        return False
    blocker["fresh_replacement_restore_target_at"] = now
    blocker["fresh_replacement_restore_target_price"] = decimal_to_plain(blocker_price)
    prioritize_grid_entry_for_restore(row.setdefault("levels", []), blocker)
    order["fresh_replacement_deferred_at"] = now
    order["fresh_replacement_deferred_behind_price"] = decimal_to_plain(blocker_price)
    preserve_replacement_order(
        row.setdefault("levels", []),
        order,
        now,
        "paused_occupancy_yield",
    )
    return True


def grid_order_target_gap(row: dict[str, Any], side: str, order: dict[str, Any] | None = None) -> Decimal:
    plan = order.get("plan") if isinstance(order, dict) else None
    gap = decimal_or_none(plan.get("grid_gap")) if isinstance(plan, dict) else None
    if gap is not None and gap > 0:
        return gap
    gap_key = "topup_buy_gap" if side == "buy" else "topup_sell_gap"
    gap = decimal_or_none(row.get(gap_key))
    if gap is not None and gap > 0:
        return gap
    return Decimal(str(row["gap_rate"]))


def grid_insert_price_between_active_gap(
    row: dict[str, Any],
    asset: dict[str, Any],
    order: dict[str, Any],
    *,
    target_gap: Decimal | None = None,
    respect_order_boundary: bool = True,
    boundary_price: Decimal | None = None,
) -> Decimal | None:
    side = str(order.get("side") or "")
    price = decimal_or_none(order.get("price", order.get("limit_px")))
    if not side or price is None or price <= 0:
        return None
    prices = {
        existing
        for entry in grid_price_occupancy_entries(row, side)
        if entry is not order
        if (existing := decimal_or_none(entry.get("price", entry.get("limit_px")))) is not None and existing > 0
    }
    if boundary_price is not None and boundary_price > 0:
        prices.add(boundary_price)
    prices = sorted(prices)
    if len(prices) < 2:
        return None
    gap_rate = target_gap if target_gap is not None and target_gap > 0 else grid_order_target_gap(row, side, order)
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    candidates: list[Decimal] = []
    for lower, upper in zip(prices, prices[1:]):
        midpoint = rounded_perp_price((lower + upper) / Decimal("2"), sz_decimals)
        if midpoint <= 0:
            continue
        if (upper - lower) <= midpoint * gap_rate * Decimal("1.95"):
            continue
        if respect_order_boundary:
            if side == "buy" and midpoint > price:
                continue
            if side == "sell" and midpoint < price:
                continue
        if active_price_too_close(row, side, midpoint, exclude=order, spacing_multiplier=GRID_ALO_SPACING_MULTIPLIER):
            continue
        candidates.append(midpoint)
    if not candidates:
        return None
    return max(candidates) if side == "buy" else min(candidates)


def move_grid_order_away_from_active(
    row: dict[str, Any],
    asset: dict[str, Any],
    order: dict[str, Any],
    *,
    max_attempts: int = GRID_ALO_PRICE_ATTEMPT_LIMIT,
) -> bool:
    side = str(order.get("side") or "")
    if not side:
        return False
    price = decimal_or_none(order.get("price", order.get("limit_px")))
    if price is not None and paused_only_grid_price_blocker(row, order) is not None:
        return True
    for _attempt in range(max_attempts):
        price = decimal_or_none(order.get("price", order.get("limit_px")))
        if price is None or price <= 0:
            return False
        if paused_only_grid_price_blocker(row, order) is not None:
            return True
        if not active_price_too_close(row, side, price, exclude=order, spacing_multiplier=GRID_ALO_SPACING_MULTIPLIER):
            return True
        next_price = grid_insert_price_between_active_gap(row, asset, order)
        if next_price is None or next_price <= 0 or next_price == price:
            next_price = next_outward_grid_price(row, asset, order)
        if next_price is None or next_price <= 0 or next_price == price:
            return False
        set_grid_order_price(order, next_price)
    return False


def advance_grid_order_away_from_active(row: dict[str, Any], asset: dict[str, Any], order: dict[str, Any]) -> bool:
    side = str(order.get("side") or "")
    price = decimal_or_none(order.get("price", order.get("limit_px")))
    if not side or price is None or price <= 0:
        return False
    if active_price_too_close(row, side, price, exclude=order, spacing_multiplier=GRID_ALO_SPACING_MULTIPLIER):
        next_price = grid_insert_price_between_active_gap(row, asset, order)
        if next_price is None or next_price <= 0 or next_price == price:
            next_price = next_outward_grid_price(row, asset, order)
    else:
        next_price = next_outward_grid_price(row, asset, order)
        if next_price is None or next_price <= 0 or next_price == price:
            next_price = grid_insert_price_between_active_gap(row, asset, order)
    if next_price is None or next_price <= 0 or next_price == price:
        return False
    set_grid_order_price(order, next_price)
    return move_grid_order_away_from_active(row, asset, order)


def ensure_grid_base_sizes(row: dict[str, Any]) -> bool:
    changed = False
    for side in ("buy", "sell"):
        base_key = f"base_{side}_size"
        legacy_key = f"{side}_size"
        if decimal_or_none(row.get(base_key)) is not None:
            continue
        legacy_size = decimal_or_none(row.get(legacy_key))
        if legacy_size is None or legacy_size <= 0:
            sizes = [
                decimal_or_none(entry.get("size"))
                for entry in row.get("levels") or []
                if isinstance(entry, dict) and str(entry.get("side") or "") == side
            ]
            sizes = [size for size in sizes if size is not None and size > 0]
            legacy_size = sizes[-1] if sizes else None
        if legacy_size is None or legacy_size <= 0:
            continue
        row[base_key] = decimal_to_plain(legacy_size)
        if decimal_or_none(row.get(legacy_key)) is None:
            row[legacy_key] = decimal_to_plain(legacy_size)
        changed = True
    return changed


def grid_order_entry(
    row: dict[str, Any],
    coin: str,
    asset: dict[str, Any],
    is_buy: bool,
    price: Decimal,
    reduce_only: bool,
    size: Decimal | None = None,
    gap: Decimal | None = None,
    preserve_size: bool = False,
) -> dict[str, Any]:
    ensure_grid_base_sizes(row)
    size_key = "base_buy_size" if is_buy else "base_sell_size"
    if size is None:
        size = Decimal(str(row.get(size_key) or row.get("buy_size" if is_buy else "sell_size") or "0"))
    if size <= 0:
        raise ValueError(f"grid row is missing {size_key}")
    min_notional = Decimal(str(row.get("min_order_value") or "10"))
    if not preserve_size:
        size = grid_size_for_min_notional(size, price, int(asset["szDecimals"]), min_notional)
    side = "buy" if is_buy else "sell"
    plan = build_grid_limit_order_plan(coin, is_buy, size, price, asset, reduce_only, side)
    plan["grid_gap"] = gap if gap is not None else Decimal(str(row["gap_rate"]))
    return {
        "side": side,
        "status": "pending",
        "oid": None,
        "is_buy": is_buy,
        "limit_px": decimal_to_plain(Decimal(str(plan["limit_px"]))),
        "price": decimal_to_plain(Decimal(str(plan["limit_px"]))),
        "size": decimal_to_plain(size),
        "mode": "grid-limit",
        "reduce_only": reduce_only,
        "plan": plan,
    }


def replacement_order_from_fill(
    row: dict[str, Any],
    coin: str,
    asset: dict[str, Any],
    submitted_limit_px: Decimal,
    filled_is_buy: bool,
    position_size: Decimal,
    position_value: Decimal,
    max_position_value: Decimal,
    policy: str,
) -> dict[str, Any] | None:
    gap = Decimal(str(row["gap_rate"]))
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    if submitted_limit_px <= 0:
        return None
    next_is_buy = not filled_is_buy
    multiplier = Decimal("1") - gap if next_is_buy else Decimal("1") + gap
    next_px = rounded_perp_price(submitted_limit_px * multiplier, sz_decimals)
    reduce_only = grid_order_should_reduce_only(position_size, next_is_buy, policy)
    order = grid_order_entry(row, coin, asset, next_is_buy, next_px, reduce_only, gap=gap)
    order["replacement_order"] = True
    return order


def panic_reversal_order_from_reduce(
    row: dict[str, Any],
    coin: str,
    asset: dict[str, Any],
    reduce_fill_price: Decimal,
    reduced_is_buy: bool,
    reduced_size: Decimal,
    position_size: Decimal,
    policy: str,
    anchor_source: str = "market_fill",
) -> dict[str, Any] | None:
    gap = Decimal(str(row["gap_rate"]))
    if reduce_fill_price <= 0 or gap <= 0:
        return None
    next_is_buy = not reduced_is_buy
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    panic_gap = gap * GRID_PANIC_REVERSAL_GAP_MULTIPLIER
    multiplier = Decimal("1") - panic_gap if next_is_buy else Decimal("1") + panic_gap
    next_px = rounded_perp_price(reduce_fill_price * multiplier, sz_decimals)
    if next_px <= 0:
        return None
    reduce_only = grid_order_should_reduce_only(position_size, next_is_buy, policy)
    if reduced_size <= 0:
        return None
    order = grid_order_entry(
        row,
        coin,
        asset,
        next_is_buy,
        next_px,
        reduce_only,
        size=reduced_size,
        gap=gap,
        preserve_size=True,
    )
    order["replacement_order"] = True
    order["panic_reversal_order"] = True
    order["replace_never_cancel"] = True
    order["grid_leg"] = 1
    order["birth_source"] = "panic"
    plan = order.get("plan")
    if isinstance(plan, dict):
        plan["label"] = "grid-panic-reversal"
        plan["panic_reversal_gap_multiplier"] = GRID_PANIC_REVERSAL_GAP_MULTIPLIER
        plan["panic_reversal_anchor_price"] = reduce_fill_price
        plan["panic_reversal_anchor_source"] = anchor_source
        plan["order_type"] = {"limit": {"tif": "Gtc"}}
    return order


def build_grid_limit_chase_market_order(
    exchange: Any,
    row: dict[str, Any],
    coin: str,
    asset: dict[str, Any],
    current_mid: Decimal,
    is_buy: bool,
    max_size: Decimal | None = None,
    target_size: Decimal | None = None,
) -> dict[str, Any] | None:
    if current_mid <= 0:
        return None
    size_key = "base_buy_size" if is_buy else "base_sell_size"
    size = decimal_or_none(row.get(size_key))
    if size is None or size <= 0:
        return None
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    step = Decimal(1).scaleb(-sz_decimals)
    base_size = (size / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if base_size <= 0:
        return None
    row_min_notional = decimal_or_none(row.get("min_order_value")) or MIN_NOTIONAL
    min_notional = max(MIN_NOTIONAL, row_min_notional) * GRID_LIMIT_CHASE_MIN_NOTIONAL_MULTIPLIER
    base_size = grid_size_for_min_notional(base_size, current_mid, sz_decimals, min_notional)
    size = base_size * Decimal("2")
    if target_size is not None and target_size > 0:
        size = max(size, target_size)
    if max_size is not None:
        size = min(size, max(Decimal("0"), max_size))
    size = (size / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if size <= 0:
        return None
    if size * current_mid < min_notional:
        return None
    slippage = Decimal(str(row.get("slippage") or DEFAULT_SLIPPAGE))
    limit_px = Decimal(str(exchange._slippage_price(coin, is_buy, float(slippage), float(current_mid))))
    limit_px = rounded_perp_price(limit_px, sz_decimals)
    if limit_px <= 0:
        return None
    notional = size * limit_px
    return {
        "side": "buy" if is_buy else "sell",
        "is_buy": is_buy,
        "size": decimal_to_plain(size),
        "price": decimal_to_plain(limit_px),
        "limit_px": decimal_to_plain(limit_px),
        "reduce_only": True,
        "limit_chase_order": True,
        "plan": {
            "label": "grid-limit-chase",
            "coin": coin,
            "is_buy": is_buy,
            "size": size,
            "limit_px": limit_px,
            "order_type": {"limit": {"tif": "Ioc"}},
            "reduce_only": True,
            "mode": "market",
            "notional": notional,
            "target_notional": notional,
            "worst_notional": notional,
            "reference_price": current_mid,
            "reference_notional": size * current_mid,
            "min_notional_buffer": min_notional,
            "limit_chase_target_size": target_size,
            "limit_chase_reduce_to_boundary": True,
            "price_source": f"mid with {slippage} slippage protection",
        },
    }


def limit_chase_replacement_order_from_market(
    row: dict[str, Any],
    coin: str,
    asset: dict[str, Any],
    market_fill_price: Decimal,
    market_is_buy: bool,
    size: Decimal,
    anchor_source: str = "market_fill",
) -> dict[str, Any] | None:
    gap = Decimal(str(row["gap_rate"]))
    if market_fill_price <= 0 or gap <= 0 or size <= 0:
        return None
    replacement_is_buy = not market_is_buy
    chase_gap = gap * GRID_PANIC_REVERSAL_GAP_MULTIPLIER
    multiplier = Decimal("1") - chase_gap if replacement_is_buy else Decimal("1") + chase_gap
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    replacement_px = rounded_perp_price(market_fill_price * multiplier, sz_decimals)
    if replacement_px <= 0:
        return None
    order = grid_order_entry(
        row,
        coin,
        asset,
        replacement_is_buy,
        replacement_px,
        False,
        size=size,
        gap=gap,
        preserve_size=True,
    )
    order["replacement_order"] = True
    order["limit_chase_replacement"] = True
    order["replace_never_cancel"] = True
    order["grid_leg"] = 1
    order["birth_source"] = "limit_chase"
    set_grid_order_reduce_only(order, False)
    plan = order.get("plan")
    if isinstance(plan, dict):
        plan["label"] = "grid-limit-chase-replacement"
        plan["limit_chase_gap_multiplier"] = GRID_PANIC_REVERSAL_GAP_MULTIPLIER
        plan["limit_chase_anchor_price"] = market_fill_price
        plan["limit_chase_anchor_source"] = anchor_source
        plan["order_type"] = {"limit": {"tif": "Gtc"}}
    return order


def preserve_replacement_order(
    levels: list[Any],
    order: dict[str, Any],
    now: int,
    reason: str | None = None,
    normalize_margin: bool = False,
) -> None:
    status = str(order.get("status") or "pending")
    order["replacement_order"] = True
    order["replacement_pause_reason"] = reason or status
    if normalize_margin and status == "paused_margin":
        order["status"] = GRID_REPLACEMENT_PAUSE_STATUS
    elif status not in GRID_PAUSED_STATUSES:
        order["status"] = GRID_REPLACEMENT_PAUSE_STATUS
    order["oid"] = None
    order.pop("replacement_pending", None)
    order.setdefault("paused_at", now)
    if order not in levels:
        levels.append(order)


def normalize_margin_paused_replacement(entry: dict[str, Any], now: int) -> bool:
    if not bool(entry.get("replacement_order")) or str(entry.get("status")) != "paused_margin":
        return False
    preserve_replacement_order([], entry, now, "paused_margin", normalize_margin=True)
    return True


def pause_skipped_account_margin_replacement(levels: list[Any], entry: dict[str, Any], now: int) -> bool:
    if str(entry.get("status")) != "skipped_account_margin" or not bool(entry.get("replacement_order")):
        return False
    preserve_replacement_order(levels, entry, now, "skipped_account_margin")
    return True


def active_grid_entries(row: dict[str, Any], side: str | None = None) -> list[dict[str, Any]]:
    entries = [
        entry
        for entry in row.get("levels") or []
        if isinstance(entry, dict)
        and entry.get("side")
        and str(entry.get("status", "active")) in {"active", GRID_PENDING_CANCEL_STATUS}
        and (side is None or str(entry.get("side")) == side)
    ]
    return entries


def grid_entry_timestamp_ms(entry: dict[str, Any]) -> int | None:
    timestamp = decimal_or_none(entry.get("timestamp"))
    if timestamp is not None and timestamp >= 0:
        return int(timestamp)
    submitted_at = decimal_or_none(entry.get("submitted_at"))
    if submitted_at is not None and submitted_at >= 0:
        return int(submitted_at * Decimal("1000"))
    return None


def oldest_active_non_reduce_only_grid_entry(
    rows: list[dict[str, Any]],
    network: str,
    account: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    account_key = account.lower()
    candidates: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
    for candidate_row in rows:
        if not isinstance(candidate_row, dict) or candidate_row.get("type") != "grid":
            continue
        if str(candidate_row.get("status") or "") != "active":
            continue
        if str(candidate_row.get("network") or "mainnet") != network:
            continue
        if str(candidate_row.get("account") or "").lower() != account_key:
            continue
        for entry in candidate_row.get("levels") or []:
            if not isinstance(entry, dict) or str(entry.get("status") or "") != "active":
                continue
            if bool(entry.get("reduce_only")) or grid_order_is_never_cancel(entry):
                continue
            try:
                oid = int(entry["oid"])
            except (KeyError, TypeError, ValueError):
                continue
            timestamp = grid_entry_timestamp_ms(entry)
            if timestamp is None:
                continue
            candidates.append((timestamp, oid, candidate_row, entry))
    if not candidates:
        return None
    _timestamp, _oid, candidate_row, entry = min(candidates, key=lambda item: (item[0], item[1]))
    return candidate_row, entry


def claim_withdrawable_pause_entry(
    row: dict[str, Any],
    rows: list[dict[str, Any]],
    network: str,
    account: str,
    cache: dict[str, Any],
) -> dict[str, Any] | None:
    account_key = (network, account.lower())
    attempted = cache.setdefault("withdrawable_pause_attempted_accounts", set())
    if account_key in attempted:
        return None
    candidate = oldest_active_non_reduce_only_grid_entry(rows, network, account)
    if candidate is None:
        attempted.add(account_key)
        return None
    candidate_row, entry = candidate
    if candidate_row is not row:
        return None
    attempted.add(account_key)
    return entry


def grid_order_is_never_cancel(entry: dict[str, Any]) -> bool:
    return bool(entry.get("replace_never_cancel"))


def grid_price_occupancy_entries(row: dict[str, Any], side: str | None = None) -> list[dict[str, Any]]:
    return [
        entry
        for entry in row.get("levels") or []
        if isinstance(entry, dict)
        and entry.get("side")
        and str(entry.get("status", "active")) in GRID_PRICE_OCCUPANCY_STATUSES
        and (side is None or str(entry.get("side")) == side)
    ]


def active_grid_oids(row: dict[str, Any], side: str | None = None) -> set[int]:
    oids: set[int] = set()
    for entry in active_grid_entries(row, side):
        try:
            oids.add(int(entry["oid"]))
        except (KeyError, TypeError, ValueError):
            continue
    return oids


def farthest_active_price(row: dict[str, Any], side: str, current_mid: Decimal) -> Decimal:
    entries = active_grid_entries(row, side)
    prices = [
        price
        for entry in entries
        if (price := decimal_or_none(entry.get("price", entry.get("limit_px")))) is not None and price > 0
    ]
    if not prices:
        return current_mid
    return min(prices) if side == "buy" else max(prices)


def nearest_active_price(row: dict[str, Any], side: str) -> Decimal | None:
    prices = [
        price
        for entry in active_grid_entries(row, side)
        if (price := decimal_or_none(entry.get("price", entry.get("limit_px")))) is not None and price > 0
    ]
    if not prices:
        return None
    return max(prices) if side == "buy" else min(prices)


def grid_panic_ratio_threshold(row: dict[str, Any]) -> Decimal:
    threshold = decimal_or_none(row.get("panic_ratio_threshold"))
    if threshold is None or threshold <= 0:
        return GRID_PANIC_RATIO_THRESHOLD
    if threshold in GRID_PANIC_RATIO_LEGACY_DEFAULT_THRESHOLDS:
        return GRID_PANIC_RATIO_THRESHOLD
    return threshold


def grid_panic_ratio(
    row: dict[str, Any],
    position_size: Decimal,
    current_mid: Decimal,
    liquidation_px: Decimal | None,
) -> Decimal | None:
    if position_size == 0 or current_mid <= 0 or liquidation_px is None or liquidation_px <= 0:
        return None
    reduce_side = reduce_side_for_position(position_size)
    if reduce_side is None:
        return None
    nearest_reduce_px = nearest_active_price(row, reduce_side)
    if nearest_reduce_px is None or nearest_reduce_px <= 0:
        return None
    if position_size < 0:
        if liquidation_px <= current_mid or nearest_reduce_px >= current_mid:
            return None
        liquidation_distance = liquidation_px - current_mid
        reduce_distance = current_mid - nearest_reduce_px
    else:
        if liquidation_px >= current_mid or nearest_reduce_px <= current_mid:
            return None
        liquidation_distance = current_mid - liquidation_px
        reduce_distance = nearest_reduce_px - current_mid
    if liquidation_distance <= 0 or reduce_distance <= 0:
        return None
    return liquidation_distance / reduce_distance


def grid_entry_gap_rate(row: dict[str, Any], entry: dict[str, Any]) -> Decimal:
    plan = entry.get("plan")
    if isinstance(plan, dict):
        gap = decimal_or_none(plan.get("grid_gap"))
        if gap is not None and gap > 0:
            return gap
    gap = decimal_or_none(entry.get("grid_gap"))
    if gap is not None and gap > 0:
        return gap
    return Decimal(str(row["gap_rate"]))


def min_grid_spacing(row: dict[str, Any], entry: dict[str, Any], price: Decimal) -> Decimal:
    return price * grid_entry_gap_rate(row, entry) * Decimal("0.75")


def active_price_too_close(
    row: dict[str, Any],
    side: str,
    price: Decimal,
    exclude: dict[str, Any] | None = None,
    spacing_multiplier: Decimal = Decimal("0.75"),
) -> bool:
    for entry in grid_price_occupancy_entries(row, side):
        if exclude is not None and entry is exclude:
            continue
        existing = decimal_or_none(entry.get("price", entry.get("limit_px")))
        if existing is None or existing <= 0:
            continue
        if abs(existing - price) <= price * Decimal(str(row["gap_rate"])) * spacing_multiplier:
            return True
    return False


def dense_grid_entries(row: dict[str, Any]) -> list[dict[str, Any]]:
    dense: list[dict[str, Any]] = []
    for side in ("buy", "sell"):
        entries = [
            entry
            for entry in active_grid_entries(row, side)
            if not grid_order_is_never_cancel(entry)
            if (decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0")) > 0
        ]
        reverse = side == "buy"
        entries.sort(key=lambda entry: decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0"), reverse=reverse)
        kept_prices: list[Decimal] = []
        for entry in entries:
            price = decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0")
            if any(abs(price - kept) < min_grid_spacing(row, entry, price) for kept in kept_prices):
                dense.append(entry)
                continue
            kept_prices.append(price)
    return dense


def regrid_dense_entries(
    exchange: Any,
    coin: str,
    row: dict[str, Any],
    asset: dict[str, Any],
    now: int,
    position_size: Decimal,
    position_value: Decimal,
    policy: str,
    account_margin_protected: bool,
    isolated_leverage_ready: set[str],
    margin_blocked_sides: set[tuple[str, str]] | None = None,
    current_mid: Decimal | None = None,
    cache: dict[str, Any] | None = None,
) -> int:
    if account_margin_protected:
        return 0
    regridded = 0
    for entry in dense_grid_entries(row):
        if str(entry.get("status", "active")) != "active":
            continue
        try:
            old_oid = int(entry["oid"])
        except (KeyError, TypeError, ValueError):
            continue
        old_snapshot = deepcopy(entry)
        old_price = decimal_or_none(entry.get("price", entry.get("limit_px")))
        if not advance_grid_order_away_from_active(row, asset, entry):
            entry.clear()
            entry.update(old_snapshot)
            entry["dense_regrid_deferred_at"] = now
            entry["dense_regrid_deferred_reason"] = "no_farther_price"
            continue
        new_price = decimal_or_none(entry.get("price", entry.get("limit_px")))
        if new_price is None or new_price <= 0 or new_price == old_price:
            entry.clear()
            entry.update(old_snapshot)
            entry["dense_regrid_deferred_at"] = now
            entry["dense_regrid_deferred_reason"] = "unchanged_price"
            continue
        try:
            cancelled = cancel_grid_entries(
                exchange,
                coin,
                [entry],
                now,
                "dense_regrid",
                row=row,
                current_mid=current_mid,
                cache=cache,
                raise_on_unconfirmed=True,
            )
        except RuntimeError as exc:
            entry.clear()
            entry.update(old_snapshot)
            if is_cancel_terminal_race_text(str(exc)):
                # The order disappeared between the open-order snapshot and
                # this cancel. Keep the original oid so the next missing-order
                # scan can distinguish a fill from a cancellation before any
                # replacement is submitted.
                entry["status"] = GRID_PENDING_CANCEL_STATUS
                entry["pending_cancel_reason"] = "dense_regrid"
                entry["pending_cancel_at"] = now
                entry["dense_regrid_pending"] = True
                entry["last_error"] = str(exc)
                continue
            raise
        if not cancelled:
            if str(entry.get("status")) != GRID_PENDING_CANCEL_STATUS:
                entry.clear()
                entry.update(old_snapshot)
                raise RuntimeError("Failed to cancel dense grid order before regrid: cancel was not confirmed")
            else:
                entry.clear()
                entry.update(old_snapshot)
                prepare_grid_cancel_entries(
                    row, [entry], now, "dense_regrid", current_mid, cache
                )
                entry["dense_regrid_pending"] = True
            continue
        entry["oid"] = None
        entry["dense_regrid_from_oid"] = old_oid
        if old_price is not None:
            entry["dense_regrid_from_price"] = decimal_to_plain(old_price)
        entry["dense_regrid_at"] = now
        submitted = submit_grid_order_entry(
            exchange,
            coin,
            entry,
            now,
            row,
            asset,
            position_size,
            position_value,
            policy,
            False,
            isolated_leverage_ready,
            retry_alo_reject=True,
            margin_blocked_sides=margin_blocked_sides,
            cache=cache,
        )
        if submitted:
            regridded += 1
    return regridded


def grid_entry_near_to_far_key(entry: dict[str, Any], side: str) -> Decimal:
    price = decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0")
    return -price if side == "buy" else price


def grid_entries_near_first_per_side(entries: list[Any]) -> list[Any]:
    """Give each side's nearest order first, then continue near-to-far fairly."""
    grid_entries = [entry for entry in entries if isinstance(entry, dict)]
    by_side = {
        side: sorted(
            [entry for entry in grid_entries if str(entry.get("side") or "") == side],
            key=lambda entry: grid_entry_near_to_far_key(entry, side),
        )
        for side in ("buy", "sell")
    }
    ordered: list[Any] = []
    index = 0
    while index < max((len(side_entries) for side_entries in by_side.values()), default=0):
        for side in ("buy", "sell"):
            if index < len(by_side[side]):
                ordered.append(by_side[side][index])
        index += 1
    ordered_ids = {id(entry) for entry in ordered}
    ordered.extend(entry for entry in entries if id(entry) not in ordered_ids)
    return ordered


def grid_nearest_non_crossing_paused_entries(
    entries: list[Any],
    current_mid: Decimal,
    best_bid: Decimal | None = None,
    best_ask: Decimal | None = None,
) -> dict[str, dict[str, Any]]:
    """Lock each side to its nearest paused level so farther levels cannot leapfrog it."""
    nearest: dict[str, dict[str, Any]] = {}
    for side in ("buy", "sell"):
        candidates = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and str(entry.get("side") or "") == side
            and str(entry.get("status") or "") in GRID_PAUSED_STATUSES
            and not grid_recovery_price_would_cross_market(entry, current_mid, best_bid, best_ask)
        ]
        if candidates:
            nearest[side] = min(
                candidates,
                key=lambda entry: grid_entry_near_to_far_key(entry, side),
            )
    return nearest


def grid_level_side_cap_clear_key(entry: dict[str, Any]) -> tuple[int, int, Decimal]:
    status = str(entry.get("status") or "")
    replacement_order = bool(entry.get("replacement_order"))
    if replacement_order and status == "active":
        priority = 5
    elif replacement_order:
        priority = 4
    elif status == "active":
        priority = 3
    elif status in {"pending", "recovery_deferred"}:
        priority = 2
    elif status in GRID_PAUSED_STATUSES:
        priority = 1
    else:
        priority = 0
    price = decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0")
    return priority, grid_level_updated_at(entry), price


def grid_side_cap_clear_candidates(row: dict[str, Any], max_per_side: int = GRID_MAX_LEVELS_PER_SIDE) -> list[dict[str, Any]]:
    levels = row.get("levels")
    if not isinstance(levels, list):
        return []
    capped_statuses = {"active", "pending", "recovery_deferred", *GRID_PAUSED_STATUSES}
    clear: list[dict[str, Any]] = []
    for side in ("buy", "sell"):
        side_entries = [
            entry
            for entry in levels
            if isinstance(entry, dict)
            and str(entry.get("side") or "") == side
            and str(entry.get("status") or "") in capped_statuses
            and not grid_order_is_never_cancel(entry)
        ]
        overflow = len(side_entries) - max_per_side
        if overflow <= 0:
            continue
        side_entries.sort(key=grid_level_side_cap_clear_key)
        clear.extend(side_entries[:overflow])
    return clear


def clear_grid_side_cap_entries(exchange: Any, coin: str, row: dict[str, Any], now: int) -> int:
    levels = row.get("levels")
    if not isinstance(levels, list):
        return 0
    to_clear = grid_side_cap_clear_candidates(row)
    if not to_clear:
        return 0
    active_to_cancel = [
        entry
        for entry in to_clear
        if str(entry.get("status") or "") == "active" and entry.get("oid") is not None
    ]
    if active_to_cancel:
        cancel_grid_entries(exchange, coin, active_to_cancel, now, "cleared_side_cap", row=row)
    clear_ids = {
        id(entry)
        for entry in to_clear
        if str(entry.get("status") or "") not in {"active", GRID_PENDING_CANCEL_STATUS}
    }
    row["levels"] = [entry for entry in levels if id(entry) not in clear_ids]
    row["side_cap_cleared_at"] = now
    row["side_cap_cleared_count"] = int(row.get("side_cap_cleared_count") or 0) + len(clear_ids)
    return len(clear_ids)


def grid_risk_density_multiplier(row: dict[str, Any], side: str, margin_gap_multiplier: Decimal) -> Decimal:
    multiplier = Decimal("1")
    avg_multiplier = decimal_or_none(row.get("avg_multiplier"))
    avg_favored_side = row.get("avg_favored_side")
    if avg_multiplier is not None and avg_multiplier > multiplier and avg_favored_side in {"buy", "sell"}:
        if side != str(avg_favored_side):
            multiplier = avg_multiplier
    if margin_gap_multiplier > multiplier:
        multiplier = margin_gap_multiplier
    return multiplier


def grid_risk_density_allowed(target_per_side: int, multiplier: Decimal) -> int:
    if target_per_side <= 0:
        return 0
    if multiplier <= 1:
        return target_per_side
    allowed = int((Decimal(target_per_side) / multiplier).to_integral_value(rounding=ROUND_FLOOR))
    return max(1, min(target_per_side, allowed))


def grid_near_far_add_risk_allowed(
    current_add_risk: int,
    prospective_add_risk: int,
    allowed_add_risk: int,
) -> bool:
    """Allow P2 near/far swaps that do not increase existing add-risk density."""
    return prospective_add_risk <= allowed_add_risk or prospective_add_risk <= current_add_risk


def grid_target_orders_per_side(row: dict[str, Any]) -> int:
    saved_target = int(row.get("target_orders_per_side") or GRID_TARGET_ORDERS_PER_SIDE)
    if saved_target in {5, 10}:
        return GRID_TARGET_ORDERS_PER_SIDE
    return saved_target


def grid_risk_density_pause_candidates(
    row: dict[str, Any],
    side: str,
    position_size: Decimal,
    target_per_side: int,
    margin_gap_multiplier: Decimal,
) -> tuple[list[dict[str, Any]], int, Decimal]:
    is_buy = side == "buy"
    if not grid_order_would_add_risk(position_size, is_buy):
        return [], target_per_side, Decimal("1")
    active_add_risk = [
        entry
        for entry in active_grid_entries(row, side)
        if grid_order_would_add_risk(position_size, bool(entry.get("is_buy")))
    ]
    multiplier = grid_risk_density_multiplier(row, side, margin_gap_multiplier)
    allowed = grid_risk_density_allowed(target_per_side, multiplier)
    if len(active_add_risk) <= allowed:
        return [], allowed, multiplier
    active_add_risk.sort(key=lambda entry: grid_entry_near_to_far_key(entry, side))
    to_pause = [
        entry
        for index, entry in enumerate(active_add_risk)
        if index >= allowed and not bool(entry.get("replacement_order"))
    ]
    return to_pause, allowed, multiplier


def grid_roe_add_risk_allowed(target_per_side: int, roe: Decimal | None) -> int:
    if target_per_side <= 0:
        return 0
    if roe is None or roe >= GRID_ROE_DENSITY_THRESHOLD:
        return target_per_side
    if roe <= GRID_ROE_STOP_THRESHOLD:
        return min(target_per_side, GRID_SURVIVAL_ACTIVE_ORDERS_PER_SIDE)
    span = GRID_ROE_DENSITY_THRESHOLD - GRID_ROE_STOP_THRESHOLD
    remaining = (roe - GRID_ROE_STOP_THRESHOLD) / span
    allowed = int((Decimal(target_per_side) * remaining).to_integral_value(rounding=ROUND_FLOOR))
    return max(1, min(target_per_side, allowed))


def grid_survival_slot_available(row: dict[str, Any], side: str) -> bool:
    return len(active_grid_entries(row, side)) < GRID_SURVIVAL_ACTIVE_ORDERS_PER_SIDE


def grid_order_allowed_by_max_or_survival(
    row: dict[str, Any],
    entry: dict[str, Any],
    side: str,
    position_size: Decimal,
    projected_position_value: Decimal,
    order_notional: Decimal,
    max_position_value: Decimal,
    policy: str,
    min_position_value: Decimal,
) -> bool:
    allowed = grid_order_allowed_by_max(
        position_size,
        projected_position_value,
        bool(entry.get("is_buy")),
        order_notional,
        max_position_value,
        policy,
        min_position_value,
        position_value_is_signed=policy == "limit",
    )
    if allowed:
        entry.pop("limit_survival_slot", None)
        return True
    if not grid_survival_slot_available(row, side):
        return False
    entry["limit_survival_slot"] = True
    return True


def grid_roe_for_position_value(position_value: Decimal, roe: Decimal | None) -> Decimal | None:
    if abs(position_value) <= GRID_ROE_MIN_POSITION_VALUE:
        return None
    return roe


def grid_roe_pause_candidates(
    row: dict[str, Any],
    side: str,
    position_size: Decimal,
    target_per_side: int,
    roe: Decimal | None,
) -> tuple[list[dict[str, Any]], int]:
    if roe is None or roe >= GRID_ROE_DENSITY_THRESHOLD:
        return [], target_per_side
    is_buy = side == "buy"
    if not grid_order_would_add_risk(position_size, is_buy):
        return [], target_per_side
    active_add_risk = [
        entry
        for entry in active_grid_entries(row, side)
        if grid_order_would_add_risk(position_size, bool(entry.get("is_buy")))
        and not grid_order_is_never_cancel(entry)
    ]
    allowed = grid_roe_add_risk_allowed(target_per_side, roe)
    if len(active_add_risk) <= allowed:
        return [], allowed
    active_add_risk.sort(key=lambda entry: grid_entry_near_to_far_key(entry, side))
    to_pause = active_add_risk[allowed:]
    return to_pause, allowed


def grid_bypassed_replacement_margin_pause_candidates(
    row: dict[str, Any],
    position_size: Decimal,
    account_margin_protected: bool,
    now: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for side in ("buy", "sell"):
        active_add_risk = [
            entry
            for entry in active_grid_entries(row, side)
            if grid_order_would_add_risk(position_size, bool(entry.get("is_buy")))
        ]
        active_add_risk.sort(key=lambda entry: grid_entry_near_to_far_key(entry, side))
        keep_count = GRID_SURVIVAL_ACTIVE_ORDERS_PER_SIDE
        keep_ids = {id(entry) for entry in active_add_risk[:keep_count]}
        for entry in active_add_risk:
            bypassed_at = int(entry.get("immediate_control_bypass_at") or 0)
            if not bypassed_at or bypassed_at >= now:
                continue
            if not account_margin_protected or id(entry) in keep_ids:
                entry.pop("immediate_control_bypass_at", None)
                continue
            if not grid_order_is_never_cancel(entry):
                candidates.append(entry)
    return candidates


def grid_active_cap_pause_candidates(
    row: dict[str, Any],
    side: str,
    max_active_per_side: int = GRID_MAX_ACTIVE_ORDERS_PER_SIDE,
) -> tuple[list[dict[str, Any]], int]:
    active = active_grid_entries(row, side)
    if len(active) <= max_active_per_side:
        return [], max_active_per_side
    keep_ids = grid_active_cap_keep_ids(active, side, max_active_per_side)
    return [entry for entry in active if id(entry) not in keep_ids], max_active_per_side


def grid_active_cap_keep_ids(entries: list[dict[str, Any]], side: str, max_active_per_side: int) -> set[int]:
    if len(entries) <= max_active_per_side:
        return {id(entry) for entry in entries}
    protected = [entry for entry in entries if grid_order_is_never_cancel(entry)]
    replacements = [
        entry
        for entry in entries
        if bool(entry.get("replacement_order")) and not grid_order_is_never_cancel(entry)
    ]
    regular = [
        entry
        for entry in entries
        if not bool(entry.get("replacement_order")) and not grid_order_is_never_cancel(entry)
    ]
    replacements.sort(key=lambda entry: grid_entry_near_to_far_key(entry, side))
    regular.sort(key=lambda entry: grid_entry_near_to_far_key(entry, side))
    keep_ids = {id(entry) for entry in protected}
    remaining_capacity = max(0, max_active_per_side - len(protected))
    if len(replacements) >= remaining_capacity:
        keep_ids.update(id(entry) for entry in replacements[:remaining_capacity])
        return keep_ids
    regular_keep_count = remaining_capacity - len(replacements)
    keep_ids.update(id(entry) for entry in replacements)
    keep_ids.update(id(entry) for entry in regular[:regular_keep_count])
    return keep_ids


def grid_entry_notional(entry: dict[str, Any]) -> Decimal:
    return Decimal(str(entry.get("size"))) * Decimal(str(entry.get("price", entry.get("limit_px"))))


def grid_entries_fit_within_max(
    entries: list[dict[str, Any]],
    side: str,
    position_size: Decimal,
    position_value: Decimal,
    max_position_value: Decimal,
    policy: str,
    min_position_value: Decimal,
    position_value_is_signed: bool = False,
) -> Decimal | None:
    projected_position_value = (
        position_value
        if policy != "limit" or position_value_is_signed
        else signed_position_value(position_size, position_value)
    )
    ordered = sorted(
        entries,
        key=lambda entry: decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0"),
        reverse=side == "buy",
    )
    for entry in ordered:
        order_notional = grid_entry_notional(entry)
        if not grid_order_allowed_by_max(
            position_size,
            projected_position_value,
            bool(entry.get("is_buy")),
            order_notional,
            max_position_value,
            policy,
            min_position_value,
            position_value_is_signed=policy == "limit",
        ):
            return None
        if policy == "limit":
            projected_position_value += order_notional if bool(entry.get("is_buy")) else -order_notional
        elif grid_order_would_add_risk(position_size, bool(entry.get("is_buy"))):
            projected_position_value += order_notional
        else:
            projected_position_value = max(Decimal("0"), projected_position_value - order_notional)
    return projected_position_value


def grid_replacement_rebalance_keep_ids(
    row: dict[str, Any],
    side: str,
    position_size: Decimal,
    position_value: Decimal,
    max_position_value: Decimal,
    policy: str,
    min_position_value: Decimal = Decimal("0"),
) -> set[int]:
    active = [
        entry
        for entry in active_grid_entries(row, side)
        if bool(entry.get("replacement_order"))
    ]
    paused = [
        entry
        for entry in row.get("levels") or []
        if isinstance(entry, dict)
        and str(entry.get("side") or "") == side
        and str(entry.get("status")) == GRID_REPLACEMENT_PAUSE_STATUS
        and bool(entry.get("replacement_order"))
    ]
    if not active or not paused:
        return set()
    regular_active = [
        entry
        for entry in active_grid_entries(row, side)
        if not bool(entry.get("replacement_order"))
    ]
    regular_projected_position_value = grid_entries_fit_within_max(
        regular_active,
        side,
        position_size,
        position_value,
        max_position_value,
        policy,
        min_position_value,
    )
    if regular_projected_position_value is None:
        return set()
    combined = active + paused
    combined.sort(key=lambda entry: grid_entry_near_to_far_key(entry, side))
    keep_count = min(len(active), len(combined))
    while keep_count > 0:
        keep_entries = combined[:keep_count]
        if (
            grid_entries_fit_within_max(
                keep_entries,
                side,
                position_size,
                regular_projected_position_value,
                max_position_value,
                policy,
                min_position_value,
                position_value_is_signed=policy == "limit",
            )
            is not None
        ):
            return {id(entry) for entry in keep_entries}
        keep_count -= 1
    return set()


def grid_replacement_rebalance_pair(
    row: dict[str, Any],
    side: str,
    position_size: Decimal,
    position_value: Decimal,
    max_position_value: Decimal,
    policy: str,
    min_position_value: Decimal = Decimal("0"),
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    keep_ids = grid_replacement_rebalance_keep_ids(
        row,
        side,
        position_size,
        position_value,
        max_position_value,
        policy,
        min_position_value,
    )
    if not keep_ids:
        return None, None
    active = [
        entry
        for entry in active_grid_entries(row, side)
        if bool(entry.get("replacement_order"))
    ]
    paused = [
        entry
        for entry in row.get("levels") or []
        if isinstance(entry, dict)
        and str(entry.get("side") or "") == side
        and str(entry.get("status")) == GRID_REPLACEMENT_PAUSE_STATUS
        and bool(entry.get("replacement_order"))
    ]
    if not active or not paused:
        return None, None
    active_ids = {id(entry) for entry in active}
    combined = active + paused
    combined.sort(key=lambda entry: grid_entry_near_to_far_key(entry, side))
    restore_entry = next(
        (
            entry
            for entry in combined
            if id(entry) in keep_ids and str(entry.get("status")) == GRID_REPLACEMENT_PAUSE_STATUS
        ),
        None,
    )
    pause_entry = next(
        (
            entry
            for entry in reversed(combined)
            if id(entry) in active_ids
            and id(entry) not in keep_ids
            and not grid_order_is_never_cancel(entry)
        ),
        None,
    )
    return pause_entry, restore_entry


def grid_near_far_rebalance_pair(
    row: dict[str, Any],
    side: str,
    position_size: Decimal,
    position_value: Decimal,
    max_position_value: Decimal,
    policy: str,
    min_position_value: Decimal = Decimal("0"),
    paused_candidates: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    active = active_grid_entries(row, side)
    paused = paused_candidates if paused_candidates is not None else [
        entry
        for entry in row.get("levels") or []
        if isinstance(entry, dict)
        and str(entry.get("side") or "") == side
        and str(entry.get("status") or "") in GRID_PAUSED_STATUSES
    ]
    if not active or not paused:
        return None, None

    combined = active + paused
    combined.sort(key=lambda entry: grid_entry_near_to_far_key(entry, side))
    keep_count = min(len(active), len(combined))
    keep_ids: set[int] = set()
    while keep_count > 0:
        keep_entries = combined[:keep_count]
        if (
            grid_entries_fit_within_max(
                keep_entries,
                side,
                position_size,
                position_value,
                max_position_value,
                policy,
                min_position_value,
            )
            is not None
        ):
            keep_ids = {id(entry) for entry in keep_entries}
            break
        keep_count -= 1
    if not keep_ids:
        return None, None

    active_ids = {id(entry) for entry in active}
    paused_ids = {id(entry) for entry in paused}
    pause_entry = next(
        (
            entry
            for entry in reversed(combined)
            if id(entry) in active_ids
            and id(entry) not in keep_ids
            and not grid_order_is_never_cancel(entry)
        ),
        None,
    )
    if pause_entry is None:
        return None, None
    pause_key = grid_entry_near_to_far_key(pause_entry, side)
    for restore_entry in combined:
        if id(restore_entry) not in paused_ids:
            continue
        restore_key = grid_entry_near_to_far_key(restore_entry, side)
        if restore_key >= pause_key:
            break
        prospective_active = [
            entry for entry in active if id(entry) != id(pause_entry)
        ] + [restore_entry]
        if (
            grid_entries_fit_within_max(
                prospective_active,
                side,
                position_size,
                position_value,
                max_position_value,
                policy,
                min_position_value,
            )
            is not None
        ):
            return pause_entry, restore_entry
    return None, None


def grid_risk_density_restore_allowed(
    row: dict[str, Any],
    entry: dict[str, Any],
    side: str,
    position_size: Decimal,
    target_per_side: int,
    margin_gap_multiplier: Decimal,
) -> bool:
    if not grid_order_would_add_risk(position_size, bool(entry.get("is_buy"))):
        return True
    active_add_risk = [
        active
        for active in active_grid_entries(row, side)
        if grid_order_would_add_risk(position_size, bool(active.get("is_buy")))
    ]
    if len(active_add_risk) >= grid_risk_density_allowed(
        target_per_side,
        grid_risk_density_multiplier(row, side, margin_gap_multiplier),
    ):
        return False
    if entry.get("near_far_rebalance_target_at") is not None:
        return True
    paused_add_risk = [
        paused
        for paused in row.get("levels") or []
        if isinstance(paused, dict)
        and str(paused.get("side") or "") == side
        and str(paused.get("status")) == GRID_RISK_DENSITY_PAUSE_STATUS
        and not bool(paused.get("replacement_order"))
        and grid_order_would_add_risk(position_size, bool(paused.get("is_buy")))
    ]
    combined = active_add_risk + paused_add_risk
    multiplier = grid_risk_density_multiplier(row, side, margin_gap_multiplier)
    allowed = grid_risk_density_allowed(target_per_side, multiplier)
    if len(combined) <= allowed:
        return True
    combined.sort(key=lambda item: grid_entry_near_to_far_key(item, side))
    keep_ids = {id(item) for item in combined[:allowed]}
    return id(entry) in keep_ids


def grid_roe_restore_allowed(
    row: dict[str, Any],
    entry: dict[str, Any],
    side: str,
    position_size: Decimal,
    target_per_side: int,
    roe: Decimal | None,
) -> bool:
    if not grid_order_would_add_risk(position_size, bool(entry.get("is_buy"))):
        return True
    allowed = grid_roe_add_risk_allowed(target_per_side, roe)
    if allowed <= 0:
        return False
    active_add_risk = [
        active
        for active in active_grid_entries(row, side)
        if grid_order_would_add_risk(position_size, bool(active.get("is_buy")))
    ]
    if len(active_add_risk) >= allowed:
        return False
    if entry.get("near_far_rebalance_target_at") is not None:
        return True
    paused_add_risk = [
        paused
        for paused in row.get("levels") or []
        if isinstance(paused, dict)
        and str(paused.get("side") or "") == side
        and str(paused.get("status")) == GRID_ROE_PAUSE_STATUS
        and grid_order_would_add_risk(position_size, bool(paused.get("is_buy")))
    ]
    combined = active_add_risk + paused_add_risk
    if len(combined) <= allowed:
        return True
    combined.sort(key=lambda item: grid_entry_near_to_far_key(item, side))
    keep_ids = {id(item) for item in combined[:allowed]}
    return id(entry) in keep_ids


def grid_latest_replacement_roe_allowed(
    row: dict[str, Any],
    entry: dict[str, Any],
    side: str,
    position_size: Decimal,
    target_per_side: int,
    roe: Decimal | None,
) -> bool:
    """Do not apply the normal target-side density cap to fresh replacements."""
    if roe is None or roe >= GRID_ROE_DENSITY_THRESHOLD:
        return True
    return grid_roe_restore_allowed(row, entry, side, position_size, target_per_side, roe)


def grid_active_cap_restore_allowed(
    row: dict[str, Any],
    entry: dict[str, Any],
    side: str,
    max_active_per_side: int = GRID_MAX_ACTIVE_ORDERS_PER_SIDE,
) -> bool:
    active = active_grid_entries(row, side)
    if len(active) >= max_active_per_side:
        return False
    if entry.get("near_far_rebalance_target_at") is not None:
        return True
    paused = [
        paused_entry
        for paused_entry in row.get("levels") or []
        if isinstance(paused_entry, dict)
        and str(paused_entry.get("side") or "") == side
        and str(paused_entry.get("status")) == GRID_ACTIVE_CAP_PAUSE_STATUS
    ]
    keep_ids = grid_active_cap_keep_ids(active + paused, side, max_active_per_side)
    return id(entry) in keep_ids


def replacement_active_cap_submit_allowed(
    row: dict[str, Any],
    side: str,
    threshold: int = GRID_REPLACEMENT_ACTIVE_CAP_SUBMIT_THRESHOLD,
) -> bool:
    """Keep replacement orders pending while a side is badly over the active cap."""
    return len(active_grid_entries(row, side)) <= threshold


def pending_cancel_overflow_candidates(
    row: dict[str, Any],
    open_oids: set[int],
    threshold: int = GRID_REPLACEMENT_ACTIVE_CAP_SUBMIT_THRESHOLD,
) -> list[dict[str, Any]]:
    side_candidates: list[tuple[int, str, list[dict[str, Any]]]] = []
    for side in ("buy", "sell"):
        live_entries: list[dict[str, Any]] = []
        for entry in active_grid_entries(row, side):
            try:
                oid = int(entry["oid"])
            except (KeyError, TypeError, ValueError):
                continue
            if oid in open_oids:
                live_entries.append(entry)
        overflow = len(live_entries) - threshold
        if overflow <= 0:
            continue
        pending = [
            entry
            for entry in live_entries
            if str(entry.get("status")) == GRID_PENDING_CANCEL_STATUS
            and not grid_order_is_never_cancel(entry)
        ]
        pending.sort(
            key=lambda entry: decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0"),
            reverse=side == "sell",
        )
        if pending:
            side_candidates.append((overflow, side, pending[:overflow]))
    side_candidates.sort(key=lambda item: (-item[0], item[1]))
    return [entry for _overflow, _side, entries in side_candidates for entry in entries]


def grid_missing_recovery_allowed(
    row: dict[str, Any],
    side: str,
    open_oids: set[int],
    max_active_per_side: int = GRID_MAX_ACTIVE_ORDERS_PER_SIDE,
) -> bool:
    live_active = 0
    for entry in active_grid_entries(row, side):
        try:
            oid = int(entry["oid"])
        except (KeyError, TypeError, ValueError):
            continue
        if oid in open_oids:
            live_active += 1
    return live_active < max_active_per_side


def next_depth_order(
    row: dict[str, Any],
    coin: str,
    asset: dict[str, Any],
    side: str,
    current_mid: Decimal,
    position_size: Decimal,
    position_value: Decimal,
    max_position_value: Decimal,
    policy: str,
    reference_px: Decimal | None = None,
    best_bid: Decimal | None = None,
    best_ask: Decimal | None = None,
) -> dict[str, Any] | None:
    gap_key = "topup_buy_gap" if side == "buy" else "topup_sell_gap"
    gap = Decimal(str(row.get(gap_key) or row["gap_rate"]))
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    is_buy = side == "buy"
    adds_risk = grid_order_would_add_risk(position_size, is_buy)
    gap_probe = {"side": side, "is_buy": is_buy, "price": decimal_to_plain(reference_px or current_mid)}
    boundary_needed = any(
        grid_price_would_cross_market(side, existing, current_mid, None, None)
        for entry in grid_price_occupancy_entries(row, side)
        if (existing := decimal_or_none(entry.get("price", entry.get("limit_px")))) is not None and existing > 0
    )
    next_px = grid_insert_price_between_active_gap(
        row,
        asset,
        gap_probe,
        target_gap=gap,
        respect_order_boundary=boundary_needed,
        boundary_price=current_mid if boundary_needed else None,
    )
    if next_px is not None and grid_price_would_cross_market(side, next_px, current_mid, best_bid, best_ask):
        next_px = None
    if next_px is None:
        base_px = farthest_active_price(row, side, reference_px or current_mid)
        multiplier = Decimal("1") - gap if is_buy else Decimal("1") + gap
        raw_next_px = base_px * multiplier
        if raw_next_px <= 0:
            return None
        next_px = rounded_perp_price(raw_next_px, sz_decimals)
    if next_px <= 0 or grid_price_would_cross_market(side, next_px, current_mid, best_bid, best_ask):
        return None
    reduce_only = grid_order_should_reduce_only(position_size, is_buy, policy)
    base_size_key = "base_buy_size" if is_buy else "base_sell_size"
    topup_size_key = "topup_buy_size" if is_buy else "topup_sell_size"
    size_key = topup_size_key if adds_risk else base_size_key
    topup_size = Decimal(str(row.get(size_key) or row.get(base_size_key) or "0"))
    return grid_order_entry(row, coin, asset, is_buy, next_px, reduce_only, size=topup_size, gap=gap)


def submit_grid_order_entry(
    exchange: Any,
    coin: str,
    order: dict[str, Any],
    now: int,
    row: dict[str, Any],
    asset: dict[str, Any],
    position_size: Decimal,
    position_value: Decimal,
    policy: str,
    account_margin_protected: bool,
    isolated_leverage_ready: set[str],
    retry_alo_reject: bool = False,
    margin_blocked_sides: set[tuple[str, str]] | None = None,
    open_orders: list[dict[str, Any]] | None = None,
    cache: dict[str, Any] | None = None,
    consume_p1_budget: bool = False,
    bypass_margin_controls: bool = False,
) -> bool:
    preserve_panic_reversal_price = grid_order_is_never_cancel(order)
    restore_without_reduce_only = grid_reduce_only_canceled_restore_without_reduce_only(order)
    refresh_grid_order_reduce_only(order, position_size, policy)
    refresh_grid_order_tif(order)
    side = str(order.get("side") or "")
    margin_side_key = (coin, side)
    if (
        not bypass_margin_controls
        and side
        and margin_blocked_sides is not None
        and margin_side_key in margin_blocked_sides
    ):
        error_text = "same-run insufficient margin pause"
        order["status"] = "paused_margin"
        order["oid"] = None
        order["last_error"] = error_text
        order["paused_at"] = now
        pause_grid_margin_side_entries(row, side, now, error_text)
        return False
    if account_margin_protected and not bypass_margin_controls:
        if grid_order_would_add_risk(position_size, bool(order.get("is_buy"))):
            if grid_survival_slot_available(row, side):
                order["account_margin_survival_slot"] = True
            elif bool(order.get("replacement_order")):
                order["status"] = "paused_account_margin"
                order["oid"] = None
                order["paused_at"] = now
                return False
            else:
                # Account protection skips this price entirely. Once protection clears,
                # the regular top-up pass builds a fresh level from the live market.
                order["status"] = "skipped_account_margin"
                order["oid"] = None
                order["skipped_at"] = now
                return False
        elif not bool(order.get("limit_chase_replacement")):
            set_grid_order_reduce_only(order, True)
    elif restore_without_reduce_only:
        set_grid_order_reduce_only(order, False)
        order["reduce_only_canceled_restore_without_reduce_only_at"] = now
    protected_reduce_only_restore = (
        account_margin_protected
        and not bypass_margin_controls
        and not bool(order.get("limit_chase_replacement"))
        and grid_order_reduces_position(order, position_size)
    )
    if not grid_reduce_only_capacity_available(
        row,
        order,
        position_size,
        position_value,
        withdrawable_protected_restore=protected_reduce_only_restore,
    ):
        order["status"] = "paused_reduce_capacity"
        order["oid"] = None
        order["paused_at"] = now
        return False
    plan = order.get("plan")
    reduce_only = bool(plan.get("reduce_only", False)) if isinstance(plan, dict) else bool(order.get("reduce_only", False))
    if (
        position_size == 0
        and not reduce_only
        and asset_requires_isolated_margin(asset)
        and coin not in isolated_leverage_ready
    ):
        try:
            reserve_grid_exchange_actions(cache, consume_p1_budget=consume_p1_budget)
        except GridActionBudgetUnavailable as exc:
            pause_grid_order_for_action_limit(order, now, str(exc))
            return False
        leverage, leverage_result = update_isolated_opening_leverage(
            exchange,
            int(asset["maxLeverage"]),
            coin,
        )
        if leverage_result.get("status") != "ok":
            raise RuntimeError(
                f"Failed to set isolated opening leverage to {leverage}x for {coin}; order was not submitted."
            )
        isolated_leverage_ready.add(coin)
    if (
        retry_alo_reject
        and not preserve_panic_reversal_price
        and not move_grid_order_away_from_active(row, asset, order)
    ):
        order["status"] = "skipped_alo_price_search"
        order["oid"] = None
        order["skipped_at"] = now
        order["alo_price_attempts"] = GRID_ALO_PRICE_ATTEMPT_LIMIT
        return False
    if retry_alo_reject and not preserve_panic_reversal_price and defer_fresh_replacement_behind_paused(
        row,
        asset,
        order,
        now,
    ):
        return False
    ensure_grid_order_min_notional(row, asset, order)
    if adopt_matching_open_grid_order(open_orders, coin, order, now, row):
        return True

    def try_reduce_only_after_margin_reject(error_text: str) -> dict[str, Any] | None:
        if restore_without_reduce_only or bool(order.get("limit_chase_replacement")):
            return None
        if grid_order_would_add_risk(position_size, bool(order.get("is_buy"))):
            return None
        plan = order.get("plan")
        already_reduce_only = bool(order.get("reduce_only", False)) or (
            isinstance(plan, dict) and bool(plan.get("reduce_only", False))
        )
        if already_reduce_only:
            return None
        set_grid_order_reduce_only(order, True)
        order["margin_reduce_only_retry_at"] = now
        order["margin_reduce_only_retry_error"] = error_text
        if not grid_reduce_only_capacity_available(row, order, position_size, position_value):
            order["status"] = "paused_reduce_capacity"
            order["oid"] = None
            order["last_error"] = error_text
            order["paused_at"] = now
            return {"handled": True}
        try:
            if adopt_matching_open_grid_order(open_orders, coin, order, now, row):
                return {"adopted": True}
            retry_oid, retry_state, retry_status = submit_grid_child_order(
                exchange,
                coin,
                order,
                cache,
                consume_p1_budget=consume_p1_budget,
            )
            return {"submitted": True, "oid": retry_oid, "state": retry_state, "status": retry_status}
        except GridActionBudgetUnavailable as retry_exc:
            pause_grid_order_for_action_limit(order, now, str(retry_exc))
            return {"handled": True}
        except GridPostOnlyRejected as retry_exc:
            order["status"] = "skipped_post_only"
            order["oid"] = None
            order["last_error"] = str(retry_exc)
            order["skipped_at"] = now
            return {"handled": True}
        except RuntimeError as retry_exc:
            retry_error_text = str(retry_exc)
            if is_reduce_only_would_increase_text(retry_error_text):
                order["status"] = "skipped_reduce_only"
                order["oid"] = None
                order["last_error"] = retry_error_text
                order["skipped_at"] = now
                return {"handled": True}
            if is_insufficient_margin_text(retry_error_text):
                return {"error_text": retry_error_text}
            raise

    try:
        oid, state, status = submit_grid_child_order(
            exchange,
            coin,
            order,
            cache,
            consume_p1_budget=consume_p1_budget,
        )
    except GridActionBudgetUnavailable as exc:
        pause_grid_order_for_action_limit(order, now, str(exc))
        return False
    except GridPostOnlyRejected as exc:
        if not retry_alo_reject:
            order["status"] = "skipped_post_only"
            order["oid"] = None
            order["last_error"] = str(exc)
            order["skipped_at"] = now
            return False
        if preserve_panic_reversal_price:
            order["status"] = "skipped_post_only"
            order["oid"] = None
            order["last_error"] = str(exc)
            order["skipped_at"] = now
            order["panic_reversal_price_preserved"] = True
            return False
        order["last_error"] = str(exc)
        oid = None
        state = ""
        status = None
        alo_rejects = 1
        side = str(order.get("side") or "")
        if not advance_grid_order_away_from_active(row, asset, order):
            order["status"] = "skipped_post_only"
            order["oid"] = None
            order["skipped_at"] = now
            order["alo_rejects"] = alo_rejects
            return False
        if defer_fresh_replacement_behind_paused(row, asset, order, now):
            order["alo_rejects"] = alo_rejects
            return False
        set_grid_order_tif(order, "Gtc")
        for attempt in range(1, GRID_ALO_PRICE_ATTEMPT_LIMIT):
            if defer_fresh_replacement_behind_paused(row, asset, order, now):
                order["alo_rejects"] = alo_rejects
                return False
            price = decimal_or_none(order.get("price", order.get("limit_px")))
            if price is None or price <= 0:
                order["status"] = "skipped_alo_price_search"
                order["oid"] = None
                order["skipped_at"] = now
                order["alo_price_attempts"] = attempt
                return False
            if active_price_too_close(
                row,
                side,
                price,
                exclude=order,
                spacing_multiplier=GRID_ALO_SPACING_MULTIPLIER,
            ):
                next_price = grid_insert_price_between_active_gap(row, asset, order) or next_outward_grid_price(row, asset, order)
                if next_price is None or next_price <= 0 or next_price == price:
                    order["status"] = "skipped_alo_price_search"
                    order["oid"] = None
                    order["skipped_at"] = now
                    order["alo_price_attempts"] = attempt + 1
                    return False
                set_grid_order_price(order, next_price)
                continue
            ensure_grid_order_min_notional(row, asset, order)
            if adopt_matching_open_grid_order(open_orders, coin, order, now, row):
                return True
            try:
                oid, state, status = submit_grid_child_order(
                    exchange,
                    coin,
                    order,
                    cache,
                    consume_p1_budget=consume_p1_budget,
                )
                break
            except GridActionBudgetUnavailable as retry_exc:
                pause_grid_order_for_action_limit(order, now, str(retry_exc))
                return False
            except GridPostOnlyRejected as retry_exc:
                alo_rejects += 1
                order["last_error"] = str(retry_exc)
                if not advance_grid_order_away_from_active(row, asset, order):
                    order["status"] = "skipped_post_only"
                    order["oid"] = None
                    order["skipped_at"] = now
                    order["alo_rejects"] = alo_rejects
                    return False
                continue
            except RuntimeError as exc:
                error_text = str(exc)
                if is_reduce_only_would_increase_text(error_text):
                    order["status"] = "skipped_reduce_only"
                    order["oid"] = None
                    order["last_error"] = error_text
                    order["skipped_at"] = now
                    return False
                if is_insufficient_margin_text(error_text):
                    retry_result = try_reduce_only_after_margin_reject(error_text)
                    if retry_result is not None:
                        if retry_result.get("submitted"):
                            oid = retry_result["oid"]
                            state = retry_result["state"]
                            status = retry_result["status"]
                            break
                        if retry_result.get("adopted"):
                            return True
                        if retry_result.get("handled"):
                            return False
                        error_text = str(retry_result.get("error_text") or error_text)
                    order["status"] = "paused_margin"
                    order["oid"] = None
                    order["last_error"] = error_text
                    order["paused_at"] = now
                    if side and grid_order_would_add_risk(position_size, bool(order.get("is_buy"))):
                        if margin_blocked_sides is not None:
                            margin_blocked_sides.add(margin_side_key)
                        pause_grid_margin_side(row, side, now, position_value)
                        pause_grid_margin_side_entries(row, side, now, error_text)
                    return False
                if is_cumulative_action_limit_text(error_text):
                    raise RuntimeError(error_text)
                if skip_grid_exchange_reject(order, error_text, now):
                    return False
                if is_min_order_value_error_text(error_text) and grid_order_is_never_cancel(order):
                    order["status"] = GRID_REPLACEMENT_PAUSE_STATUS
                    order["oid"] = None
                    order["last_error"] = error_text
                    order["paused_at"] = now
                    return False
                if not is_min_order_value_error_text(error_text) or order.get("resized_min_retry_at"):
                    raise
                bump_grid_order_size_one_step(asset, order)
                order["resized_min_retry_at"] = now
                try:
                    oid, state, status = submit_grid_child_order(
                        exchange,
                        coin,
                        order,
                        cache,
                        consume_p1_budget=consume_p1_budget,
                    )
                except GridActionBudgetUnavailable as retry_exc:
                    pause_grid_order_for_action_limit(order, now, str(retry_exc))
                    return False
                break
        else:
            order["status"] = "skipped_post_only"
            order["oid"] = None
            order["skipped_at"] = now
            order["alo_rejects"] = alo_rejects
            order["alo_price_attempts"] = GRID_ALO_PRICE_ATTEMPT_LIMIT
            return False
    except RuntimeError as exc:
        error_text = str(exc)
        if is_reduce_only_would_increase_text(error_text):
            order["status"] = "skipped_reduce_only"
            order["oid"] = None
            order["last_error"] = error_text
            order["skipped_at"] = now
            return False
        if is_insufficient_margin_text(error_text):
            retry_result = try_reduce_only_after_margin_reject(error_text)
            if retry_result is not None:
                if retry_result.get("submitted"):
                    oid = retry_result["oid"]
                    state = retry_result["state"]
                    status = retry_result["status"]
                    order["oid"] = oid
                    order["status"] = state
                    order["submitted_at"] = now
                    order["last_submit_status"] = status
                    if state == "active":
                        record_submitted_open_grid_order(open_orders, coin, order, oid, now)
                    if state == "filled":
                        order["filled_at"] = now
                        order["replacement_pending"] = True
                    return True
                if retry_result.get("adopted"):
                    return True
                if retry_result.get("handled"):
                    return False
                error_text = str(retry_result.get("error_text") or error_text)
            order["status"] = "paused_margin"
            order["oid"] = None
            order["last_error"] = error_text
            order["paused_at"] = now
            if side and grid_order_would_add_risk(position_size, bool(order.get("is_buy"))):
                if margin_blocked_sides is not None:
                    margin_blocked_sides.add(margin_side_key)
                pause_grid_margin_side(row, side, now, position_value)
                pause_grid_margin_side_entries(row, side, now, error_text)
            return False
        if is_cumulative_action_limit_text(error_text):
            raise RuntimeError(error_text)
        if skip_grid_exchange_reject(order, error_text, now):
            return False
        if is_min_order_value_error_text(error_text) and grid_order_is_never_cancel(order):
            order["status"] = GRID_REPLACEMENT_PAUSE_STATUS
            order["oid"] = None
            order["last_error"] = error_text
            order["paused_at"] = now
            return False
        if not is_min_order_value_error_text(error_text) or order.get("resized_min_retry_at"):
            raise
        bump_grid_order_size_one_step(asset, order)
        order["resized_min_retry_at"] = now
        try:
            oid, state, status = submit_grid_child_order(
                exchange,
                coin,
                order,
                cache,
                consume_p1_budget=consume_p1_budget,
            )
        except GridActionBudgetUnavailable as retry_exc:
            pause_grid_order_for_action_limit(order, now, str(retry_exc))
            return False
        except GridPostOnlyRejected as exc:
            order["status"] = "skipped_post_only"
            order["oid"] = None
            order["last_error"] = str(exc)
            order["skipped_at"] = now
            return False
    order["oid"] = oid
    order["status"] = state
    order["submitted_at"] = now
    order["last_submit_status"] = status
    if state == "active":
        record_submitted_open_grid_order(open_orders, coin, order, oid, now)
    if retry_alo_reject and "alo_rejects" in locals() and alo_rejects:
        order["alo_rejects"] = alo_rejects
    if state == "filled":
        order["filled_at"] = now
        order["replacement_pending"] = True
    return True


def build_grid_panic_reduce_order(
    exchange: Any,
    row: dict[str, Any],
    coin: str,
    asset: dict[str, Any],
    current_mid: Decimal,
    position_size: Decimal,
) -> dict[str, Any] | None:
    if position_size == 0 or current_mid <= 0:
        return None
    is_buy = position_size < 0
    side = "buy" if is_buy else "sell"
    size_key = "base_buy_size" if is_buy else "base_sell_size"
    size = decimal_or_none(row.get(size_key))
    if size is None or size <= 0:
        return None
    max_size = abs(position_size)
    if max_size <= 0:
        return None
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    step = Decimal(1).scaleb(-sz_decimals)
    base_size = (size / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if base_size <= 0:
        return None
    slippage = Decimal(str(row.get("slippage") or DEFAULT_SLIPPAGE))
    limit_px = Decimal(str(exchange._slippage_price(coin, is_buy, float(slippage), float(current_mid))))
    limit_px = rounded_perp_price(limit_px, sz_decimals)
    if limit_px <= 0:
        return None
    if base_size * limit_px < GRID_PANIC_REDUCE_MIN_NOTIONAL:
        base_size = grid_size_for_min_notional(
            base_size,
            limit_px,
            sz_decimals,
            GRID_PANIC_REDUCE_MIN_NOTIONAL,
        )
    position_fraction_cap = (
        (max_size * Decimal("0.4") / step).to_integral_value(rounding=ROUND_FLOOR) * step
    )
    if position_fraction_cap < base_size:
        return None
    position_fraction_target = max_size * Decimal("0.2")
    size = min(
        max(base_size * Decimal("2"), position_fraction_target),
        position_fraction_cap,
        max_size,
    )
    size = (size / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if size < base_size:
        return None
    if size * limit_px < GRID_PANIC_REDUCE_MIN_NOTIONAL:
        size = grid_size_for_min_notional(
            size,
            limit_px,
            sz_decimals,
            GRID_PANIC_REDUCE_MIN_NOTIONAL,
        )
        size = min(size, position_fraction_cap, max_size)
        size = (size / step).to_integral_value(rounding=ROUND_FLOOR) * step
        if size < base_size:
            return None
        if size * limit_px < GRID_PANIC_REDUCE_MIN_NOTIONAL:
            return None
    notional = size * limit_px
    return {
        "side": side,
        "is_buy": is_buy,
        "size": decimal_to_plain(size),
        "price": decimal_to_plain(limit_px),
        "limit_px": decimal_to_plain(limit_px),
        "reduce_only": True,
        "plan": {
            "label": "grid-panic-reduce",
            "coin": coin,
            "is_buy": is_buy,
            "size": size,
            "limit_px": limit_px,
            "order_type": {"limit": {"tif": "Ioc"}},
            "reduce_only": True,
            "mode": "market",
            "notional": notional,
            "target_notional": notional,
            "worst_notional": notional,
            "reference_price": current_mid,
            "price_source": f"mid with {slippage} slippage protection",
            "base_size": base_size,
            "position_fraction_target": position_fraction_target,
            "position_fraction_cap": position_fraction_cap,
        },
    }


def submit_grid_panic_reduce(
    exchange: Any,
    coin: str,
    order: dict[str, Any],
    now: int,
    row: dict[str, Any],
    cache: dict[str, Any] | None = None,
    cloid: Cloid | None = None,
) -> bool:
    plan = order.get("plan")
    if not isinstance(plan, dict):
        return False
    def submit_once() -> Any:
        reserve_grid_exchange_actions(cache)
        args = (
            coin,
            bool(plan["is_buy"]),
            float(plan["size"]),
            float(plan["limit_px"]),
            plan["order_type"],
        )
        if cloid is not None:
            return exchange.order(*args, reduce_only=True, cloid=cloid)
        return exchange.order(*args, reduce_only=True)

    def record_wait(wait_count: int, error_text: str) -> None:
        row["panic_reduce_action_limit_wait_seconds"] = GRID_EMERGENCY_ACTION_LIMIT_WAIT_SECONDS
        row["panic_reduce_action_limit_wait_at"] = now
        row["panic_reduce_action_limit_wait_count"] = wait_count
        row["panic_reduce_action_limit_wait_error"] = error_text

    result = submit_with_cumulative_action_limit_wait(
        submit_once,
        cache=cache,
        on_wait=record_wait,
    )
    audit_grid_action(
        "panic_reduce_submit",
        coin=coin,
        side=order.get("side"),
        price=order.get("price"),
        size=order.get("size"),
        reduce_only=True,
        economic_chain_id=(
            order.get("economic_chain_id")
            or (str(cloid) if cloid is not None else None)
        ),
        birth_source="panic",
        grid_id=row.get("id"),
        result=result,
    )
    log_event("grid_panic_reduce_order", {"coin": coin, "side": order.get("side"), "result": result})
    if result.get("status") != "ok":
        row["panic_reduce_error"] = str(result)
        row["panic_reduce_error_at"] = now
        return False
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    for status in statuses:
        if not isinstance(status, dict):
            continue
        if status.get("error"):
            row["panic_reduce_error"] = str(status["error"])
            row["panic_reduce_error_at"] = now
            return False
        if isinstance(status.get("filled"), dict):
            filled = status["filled"]
            order["oid"] = int(filled.get("oid", 0))
            order["status"] = "filled"
            order["filled_at"] = now
            order["last_submit_status"] = status
            filled_size = decimal_or_none(filled.get("totalSz", filled.get("sz")))
            if filled_size is not None and filled_size > 0:
                order["filled_size"] = decimal_to_plain(filled_size)
            filled_avg_px = decimal_or_none(filled.get("avgPx"))
            if filled_avg_px is not None and filled_avg_px > 0:
                order["filled_avg_px"] = decimal_to_plain(filled_avg_px)
            return True
        if isinstance(status.get("resting"), dict):
            row["panic_reduce_error"] = f"panic IOC unexpectedly rested: {status}"
            row["panic_reduce_error_at"] = now
            return False
    row["panic_reduce_error"] = f"panic reduce response did not include an order id: {result}"
    row["panic_reduce_error_at"] = now
    return False


def apply_panic_reversal_submit_status(order: dict[str, Any], status: Any, now: int) -> bool:
    order["last_submit_status"] = status
    if not isinstance(status, dict):
        order["status"] = GRID_REPLACEMENT_PAUSE_STATUS
        order["oid"] = None
        order["last_error"] = f"panic reversal response was invalid: {status}"
        order["paused_at"] = now
        return False
    if status.get("error"):
        order["status"] = GRID_REPLACEMENT_PAUSE_STATUS
        order["oid"] = None
        order["last_error"] = str(status["error"])
        order["paused_at"] = now
        return False
    resting = status.get("resting")
    if isinstance(resting, dict):
        order["oid"] = int(resting.get("oid", 0))
        order["status"] = "active"
        order["submitted_at"] = now
        return True
    filled = status.get("filled")
    if isinstance(filled, dict):
        order["oid"] = int(filled.get("oid", 0))
        order["status"] = "filled"
        order["filled_at"] = now
        order["replacement_pending"] = True
        return True
    order["status"] = GRID_REPLACEMENT_PAUSE_STATUS
    order["oid"] = None
    order["last_error"] = f"panic reversal response did not include an order id: {status}"
    order["paused_at"] = now
    return False


def cancel_unbacked_panic_reversal(
    exchange: Any,
    coin: str,
    order: dict[str, Any],
    now: int,
    cache: dict[str, Any] | None,
) -> bool:
    if str(order.get("status")) != "active" or not order.get("oid"):
        return True
    order.pop("replace_never_cancel", None)
    try:
        cancelled = cancel_grid_entries(
            exchange,
            coin,
            [order],
            now,
            "cancelled_panic_unbacked",
            cache=cache,
            raise_on_unconfirmed=True,
            preserve_active_floor=False,
        )
    except (GridActionBudgetUnavailable, RuntimeError) as exc:
        order["status"] = GRID_PENDING_CANCEL_STATUS
        order["panic_reversal_unbacked"] = True
        order["panic_reversal_cancel_error"] = str(exc)
        order["pending_cancel_at"] = now
        return False
    return cancelled == 1


def resize_active_panic_reversal(
    exchange: Any,
    coin: str,
    order: dict[str, Any],
    size: Decimal,
    now: int,
    cache: dict[str, Any] | None,
) -> bool:
    plan = order.get("plan")
    if not isinstance(plan, dict) or str(order.get("status")) != "active" or not order.get("oid"):
        set_grid_order_size_exact(order, size)
        return False
    reserve_grid_exchange_actions(cache)
    result = exchange.modify_order(
        int(order["oid"]),
        coin,
        bool(plan["is_buy"]),
        float(size),
        float(plan["limit_px"]),
        {"limit": {"tif": "Gtc"}},
        reduce_only=bool(plan.get("reduce_only", False)),
    )
    audit_grid_action(
        "panic_reversal_resize",
        coin=coin,
        oid=order.get("oid"),
        size=decimal_to_plain(size),
        result=result,
    )
    statuses = result.get("response", {}).get("data", {}).get("statuses", []) if isinstance(result, dict) else []
    status = statuses[0] if result.get("status") == "ok" and statuses else None
    if status is not None and not (isinstance(status, dict) and status.get("error")):
        set_grid_order_size_exact(order, size)
        order["panic_reversal_resized_at"] = now
        return apply_panic_reversal_submit_status(order, status, now)
    order["panic_reversal_resize_error"] = str(result)
    old_oid = order.get("oid")
    if cancel_unbacked_panic_reversal(exchange, coin, order, now, cache):
        order["replace_never_cancel"] = True
        order["status"] = GRID_REPLACEMENT_PAUSE_STATUS
        order["oid"] = None
        order["paused_at"] = now
        order["panic_reversal_resubmit_after_resize_oid"] = old_oid
        set_grid_order_size_exact(order, size)
    return False


def submit_grid_panic_pair(
    exchange: Any,
    coin: str,
    panic_order: dict[str, Any],
    reversal_order: dict[str, Any],
    now: int,
    row: dict[str, Any],
    cache: dict[str, Any] | None = None,
    open_orders: list[dict[str, Any]] | None = None,
) -> tuple[bool, bool]:
    panic_plan = panic_order.get("plan")
    reversal_plan = reversal_order.get("plan")
    if not isinstance(panic_plan, dict) or not isinstance(reversal_plan, dict):
        return False, False
    set_grid_order_tif(reversal_order, "Gtc")
    reserve_grid_exchange_actions(cache, 2)
    requests = [order_plan_request(panic_plan), order_plan_request(reversal_plan)]
    result = exchange.bulk_orders(requests, grouping="na")
    audit_grid_action(
        "panic_pair_submit",
        coin=coin,
        panic_price=panic_order.get("price"),
        reversal_price=reversal_order.get("price"),
        size=panic_order.get("size"),
        result=result,
    )
    log_event("grid_panic_pair_orders", {"coin": coin, "requests": requests, "result": result})
    if result.get("status") != "ok":
        row["panic_reduce_error"] = str(result)
        row["panic_reduce_error_at"] = now
        return False, False
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    if not isinstance(statuses, list) or len(statuses) != 2:
        row["panic_reduce_error"] = f"panic pair response statuses mismatch: {result}"
        row["panic_reduce_error_at"] = now
        return False, False

    reversal_submitted = apply_panic_reversal_submit_status(reversal_order, statuses[1], now)
    panic_status = statuses[0]
    if not isinstance(panic_status, dict) or panic_status.get("error") or not isinstance(panic_status.get("filled"), dict):
        row["panic_reduce_error"] = str(
            panic_status.get("error") if isinstance(panic_status, dict) and panic_status.get("error") else panic_status
        )
        row["panic_reduce_error_at"] = now
        if reversal_submitted and str(reversal_order.get("status")) == "active":
            cancelled = cancel_unbacked_panic_reversal(exchange, coin, reversal_order, now, cache)
            return False, not cancelled
        if reversal_submitted and str(reversal_order.get("status")) == "filled":
            reversal_order["panic_reversal_panic_retry_at"] = now
            if submit_grid_panic_reduce(exchange, coin, panic_order, now, row, cache):
                reversal_order["panic_reversal_panic_retry_succeeded"] = True
                return True, True
            reversal_order.pop("replace_never_cancel", None)
            reversal_order["panic_reversal_unbacked_filled"] = True
            return False, True
        return False, False

    filled = panic_status["filled"]
    panic_order["oid"] = int(filled.get("oid", 0))
    panic_order["status"] = "filled"
    panic_order["filled_at"] = now
    panic_order["last_submit_status"] = panic_status
    filled_size = decimal_or_none(filled.get("totalSz", filled.get("sz"))) or decimal_or_none(panic_order.get("size"))
    if filled_size is None or filled_size <= 0:
        row["panic_reduce_error"] = f"panic pair fill did not include a positive size: {panic_status}"
        row["panic_reduce_error_at"] = now
        return False, reversal_submitted
    panic_order["filled_size"] = decimal_to_plain(filled_size)
    requested_size = decimal_or_none(reversal_order.get("size"))
    if requested_size != filled_size:
        if reversal_submitted and str(reversal_order.get("status")) == "active":
            reversal_submitted = resize_active_panic_reversal(
                exchange,
                coin,
                reversal_order,
                filled_size,
                now,
                cache,
            )
        elif reversal_submitted and str(reversal_order.get("status")) == "filled":
            reversal_order["panic_reversal_partial_fill_mismatch"] = {
                "panic_filled_size": decimal_to_plain(filled_size),
                "reversal_size": decimal_to_plain(requested_size or Decimal("0")),
            }
        else:
            set_grid_order_size_exact(reversal_order, filled_size)
    if reversal_submitted and str(reversal_order.get("status")) == "active":
        record_submitted_open_grid_order(
            open_orders,
            coin,
            reversal_order,
            int(reversal_order.get("oid") or 0),
            now,
        )
    return True, True


def prepare_grid_cancel_entries(
    row: dict[str, Any] | None,
    entries: list[dict[str, Any]],
    now: int,
    note: str,
    current_mid: Decimal | None,
    cache: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], int]:
    if row is None or current_mid is None:
        return entries, 0
    rate = pending_cancel_rate(action_limit_deficit(cache))
    if rate is None:
        return entries, 0
    immediate: list[dict[str, Any]] = []
    deferred = 0
    for entry in entries:
        if not isinstance(entry, dict) or str(entry.get("status")) == GRID_PENDING_CANCEL_STATUS:
            continue
        if not grid_order_far_from_mid(entry, current_mid, rate):
            immediate.append(entry)
            continue
        entry["status"] = GRID_PENDING_CANCEL_STATUS
        entry["pending_cancel_reason"] = note
        entry["pending_cancel_at"] = now
        entry["pending_cancel_mid"] = decimal_to_plain(current_mid)
        entry["pending_cancel_rate"] = decimal_to_plain(rate)
        audit_grid_action(
            "pending_cancel_defer",
            coin=row.get("coin"),
            side=entry.get("side"),
            oid=entry.get("oid"),
            price=entry.get("price", entry.get("limit_px")),
            reason=note,
            deficit=action_limit_deficit(cache),
            pending_rate=decimal_to_plain(rate),
            phase=cache.get("grid_action_phase") if isinstance(cache, dict) else None,
        )
        deferred += 1
    return immediate, deferred


def preserve_grid_active_floor(
    row: dict[str, Any] | None,
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Never let worker maintenance cancel the last active order on a side."""
    if row is None:
        return entries
    allowed_ids: set[int] = {
        id(entry)
        for entry in entries
        if str(entry.get("side") or "") not in {"buy", "sell"}
    }
    for side in ("buy", "sell"):
        active = active_grid_entries(row, side)
        candidates = [
            entry
            for entry in entries
            if str(entry.get("side") or "") == side and id(entry) in {id(item) for item in active}
        ]
        allowed_count = max(0, len(active) - GRID_SURVIVAL_ACTIVE_ORDERS_PER_SIDE)
        if len(candidates) <= allowed_count:
            allowed_ids.update(id(entry) for entry in candidates)
            continue
        # When a bulk rule targets the whole side, retain the nearest candidate.
        candidates.sort(key=lambda entry: grid_entry_near_to_far_key(entry, side))
        allowed_ids.update(id(entry) for entry in candidates[len(candidates) - allowed_count :])
    return [entry for entry in entries if id(entry) in allowed_ids]


def cancel_grid_entries(
    exchange: Any,
    coin: str,
    entries: list[dict[str, Any]],
    now: int,
    note: str,
    row: dict[str, Any] | None = None,
    current_mid: Decimal | None = None,
    cache: dict[str, Any] | None = None,
    raise_on_unconfirmed: bool = False,
    consume_p1_budget: bool = False,
    preserve_active_floor: bool = True,
) -> int:
    entries = [entry for entry in entries if not grid_order_is_never_cancel(entry)]
    if preserve_active_floor:
        entries = preserve_grid_active_floor(row, entries)
    entries, deferred = prepare_grid_cancel_entries(row, entries, now, note, current_mid, cache)
    if deferred and isinstance(cache, dict):
        cache["pending_cancel_changed"] = True
    requests = []
    for entry in entries:
        try:
            requests.append({"coin": coin, "oid": int(entry["oid"])})
        except (KeyError, TypeError, ValueError):
            continue
    if not requests:
        return 0
    reserve_grid_exchange_actions(
        cache,
        len(requests),
        consume_p1_budget=consume_p1_budget,
    )
    result = exchange.bulk_cancel(requests)
    audit_grid_action(
        "grid_cancel",
        coin=coin,
        note=note,
        requests=requests,
        result=result,
        deficit=action_limit_deficit(cache),
        phase=cache.get("grid_action_phase") if isinstance(cache, dict) else None,
    )
    log_event("grid_cancel_entries", {"note": note, "requests": requests, "result": result})
    if result.get("status") != "ok":
        raise RuntimeError(f"Failed to cancel grid orders: {result}")
    cancelled, cancel_errors = successful_cancel_oids(result, requests)
    if cancel_errors:
        log_event("grid_cancel_entry_errors", {"note": note, "errors": cancel_errors})
        if raise_on_unconfirmed:
            raise RuntimeError("; ".join(cancel_errors))
    for entry in entries:
        try:
            oid = int(entry["oid"])
        except (KeyError, TypeError, ValueError):
            continue
        if oid in cancelled:
            entry["status"] = note
            entry["cancelled_at"] = now
            entry.pop("pending_cancel_reason", None)
            entry.pop("pending_cancel_at", None)
            entry.pop("pending_cancel_mid", None)
            entry.pop("pending_cancel_rate", None)
    return len(cancelled)


def cancel_grid_entries_with_p1_budget(
    exchange: Any,
    coin: str,
    entries: list[dict[str, Any]],
    now: int,
    note: str,
    cache: dict[str, Any] | None,
    row: dict[str, Any] | None = None,
    current_mid: Decimal | None = None,
) -> int:
    if isinstance(cache, dict) and cache.get("grid_action_phase") not in (
        None,
        GRID_ACTION_PHASE_P1_CANCELS,
        GRID_ACTION_PHASE_P1_WITHDRAWABLE,
    ):
        return 0
    entries = [entry for entry in entries if not grid_order_is_never_cancel(entry)]
    entries, deferred = prepare_grid_cancel_entries(row, entries, now, note, current_mid, cache)
    if deferred and isinstance(cache, dict):
        cache["pending_cancel_changed"] = True
    budget_tracked = p1_budget_tracked(cache)
    if budget_tracked:
        if not action_limit_p1_enabled(cache):
            return 0
        remaining = action_limit_p1_budget_remaining(cache) or 0
        if remaining <= 0:
            return 0
        entries = entries[:remaining]
    cancelled = cancel_grid_entries(
        exchange,
        coin,
        entries,
        now,
        note,
        row=row,
        current_mid=current_mid,
        cache=cache,
        consume_p1_budget=budget_tracked,
    )
    return cancelled


def grid_fill_time(entry: dict[str, Any]) -> int:
    fill = entry.get("fill")
    if isinstance(fill, dict):
        try:
            return int(fill.get("time") or 0)
        except (TypeError, ValueError):
            pass
    try:
        return int(entry.get("filled_at") or 0) * 1000
    except (TypeError, ValueError):
        return 0


def grid_fill_adds_risk(entry: dict[str, Any], position_size: Decimal) -> bool:
    fill = entry.get("fill")
    if isinstance(fill, dict):
        direction = str(fill.get("dir") or "").lower()
        if "open" in direction:
            return True
        if "close" in direction:
            return False
    return grid_order_would_add_risk(position_size, bool(entry.get("is_buy")))


def grid_entry_oid_key(entry: dict[str, Any]) -> str:
    fill = entry.get("fill")
    if isinstance(fill, dict) and fill.get("oid") is not None:
        return str(fill.get("oid"))
    for key in ("confirmed_filled_oid", "oid"):
        if entry.get(key) is not None:
            return str(entry.get(key))
    return ""


def recent_grid_filled_entries(row: dict[str, Any]) -> list[dict[str, Any]]:
    entries = [
        entry
        for entry in row.get("levels") or []
        if isinstance(entry, dict)
        and entry.get("side")
        and str(entry.get("status")) == "filled"
        and grid_entry_oid_key(entry)
    ]
    return sorted(entries, key=grid_fill_time)


def nearest_add_risk_grid_entry(row: dict[str, Any], side: str, position_size: Decimal) -> dict[str, Any] | None:
    entries = [
        entry
        for entry in active_grid_entries(row, side)
        if grid_order_would_add_risk(position_size, bool(entry.get("is_buy")))
        and not grid_order_is_never_cancel(entry)
    ]
    if not entries:
        return None
    return sorted(
        entries,
        key=lambda entry: decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0"),
        reverse=side == "buy",
    )[0]


def append_add_risk_brake_history(row: dict[str, Any], event: dict[str, Any]) -> None:
    history = row.setdefault("add_risk_brakes", [])
    if not isinstance(history, list):
        history = []
        row["add_risk_brakes"] = history
    history.append(event)
    del history[:-GRID_LEVEL_HISTORY_MAX]


def prune_add_risk_brake_state(row: dict[str, Any], now: int) -> bool:
    changed = False
    if "add_risk_streak" in row:
        row.pop("add_risk_streak", None)
        changed = True

    try:
        last_at = int(row.get("last_add_risk_brake_at") or 0)
    except (TypeError, ValueError):
        last_at = 0
    if last_at and now - last_at > GRID_ADD_RISK_BRAKE_PAIR_RETENTION_SECONDS:
        row.pop("last_add_risk_brake_pair", None)
        row.pop("last_add_risk_brake_at", None)
        changed = True

    history = row.get("add_risk_brakes")
    if history is None:
        return changed
    if not isinstance(history, list):
        row.pop("add_risk_brakes", None)
        return True

    cutoff = now - GRID_ADD_RISK_BRAKE_HISTORY_RETENTION_SECONDS
    kept: list[dict[str, Any]] = []
    for event in history:
        if not isinstance(event, dict):
            changed = True
            continue
        try:
            event_at = int(event.get("at") or 0)
        except (TypeError, ValueError):
            event_at = 0
        if event_at >= cutoff:
            kept.append(event)
        else:
            changed = True
    kept = kept[-GRID_LEVEL_HISTORY_MAX:]
    if len(kept) != len(history):
        changed = True
    if kept:
        row["add_risk_brakes"] = kept
    else:
        row.pop("add_risk_brakes", None)
    return changed


def apply_grid_add_risk_brake(
    exchange: Any,
    coin: str,
    row: dict[str, Any],
    newly_filled: list[dict[str, Any]],
    position_size: Decimal,
    now: int,
) -> int:
    recent_filled = recent_grid_filled_entries(row)
    if len(recent_filled) < GRID_ADD_RISK_BRAKE_STREAK:
        return 0

    latest_pair = recent_filled[-GRID_ADD_RISK_BRAKE_STREAK:]
    latest_keys = [grid_entry_oid_key(entry) for entry in latest_pair]
    new_keys = {
        grid_entry_oid_key(entry)
        for entry in newly_filled
        if isinstance(entry, dict) and grid_entry_oid_key(entry)
    }
    if not any(key in new_keys for key in latest_keys):
        return 0

    pair_key = ":".join(latest_keys)
    if pair_key == str(row.get("last_add_risk_brake_pair") or ""):
        return 0

    sides = [str(entry.get("side") or "") for entry in latest_pair]
    adds_risk = [grid_fill_adds_risk(entry, position_size) for entry in latest_pair]
    if len(set(sides)) != 1 or sides[0] not in {"buy", "sell"} or not all(adds_risk):
        return 0

    side = sides[0]
    target = nearest_add_risk_grid_entry(row, side, position_size)
    event = {
        "at": now,
        "side": side,
        "trigger_oids": latest_keys,
        "threshold": GRID_ADD_RISK_BRAKE_STREAK,
        "mode": "latest_pair",
    }
    cancelled = 0
    if target is None:
        event["status"] = "skipped_no_active_add_risk_order"
    else:
        try:
            cancelled = cancel_grid_entries(exchange, coin, [target], now, "brake_near_add_risk", row=row)
        except RuntimeError as exc:
            target["brake_cancel_failed_at"] = now
            target["brake_cancel_error"] = str(exc)
            event.update(
                {
                    "status": "cancel_failed",
                    "cancelled_oid": target.get("oid"),
                    "cancelled_price": target.get("price", target.get("limit_px")),
                    "error": str(exc),
                }
            )
            row["last_add_risk_brake_pair"] = pair_key
            row["last_add_risk_brake_at"] = now
            append_add_risk_brake_history(row, event)
            return 0
        event.update(
            {
                "status": "cancelled" if cancelled else "cancel_failed",
                "cancelled_oid": target.get("oid"),
                "cancelled_price": target.get("price", target.get("limit_px")),
            }
        )
    row["last_add_risk_brake_pair"] = pair_key
    row["last_add_risk_brake_at"] = now
    append_add_risk_brake_history(row, event)
    return cancelled


def trim_excess_grid_entries(exchange: Any, coin: str, row: dict[str, Any], target_per_side: int, now: int) -> int:
    trimmed = 0
    for side in ("buy", "sell"):
        entries = active_grid_entries(row, side)
        unique_entries: list[dict[str, Any]] = []
        entries_by_oid: dict[int, list[dict[str, Any]]] = {}
        for entry in entries:
            try:
                oid = int(entry["oid"])
            except (KeyError, TypeError, ValueError):
                entry["status"] = "invalid_active_oid"
                entry["cancelled_at"] = now
                trimmed += 1
                continue
            if oid not in entries_by_oid:
                unique_entries.append(entry)
                entries_by_oid[oid] = []
            entries_by_oid[oid].append(entry)

        for oid_entries in entries_by_oid.values():
            for duplicate in oid_entries[1:]:
                duplicate["status"] = "duplicate_active_oid"
                duplicate["cancelled_at"] = now
                trimmed += 1

    return trimmed


def prioritize_grid_entry_for_restore(levels: list[dict[str, Any]], entry: dict[str, Any]) -> bool:
    for index, candidate in enumerate(levels):
        if id(candidate) != id(entry):
            continue
        if index == 0:
            return False
        levels.insert(0, levels.pop(index))
        return True
    return False


def paused_replacement_restore_entries_near_first(
    levels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Scan restorable replacements near-to-far without changing side fairness."""
    ordered = list(levels)
    for side in ("buy", "sell"):
        indexes = [
            index
            for index, entry in enumerate(ordered)
            if isinstance(entry, dict)
            and str(entry.get("side") or "") == side
            and bool(entry.get("replacement_order"))
            and str(entry.get("status") or "") in GRID_PAUSED_STATUSES
        ]
        side_entries = sorted(
            (ordered[index] for index in indexes),
            key=lambda entry: grid_entry_near_to_far_key(entry, side),
        )
        for index, entry in zip(indexes, side_entries):
            ordered[index] = entry
    return ordered


def reduce_side_for_position(position_size: Decimal) -> str | None:
    if position_size > 0:
        return "sell"
    if position_size < 0:
        return "buy"
    return None


def grid_effectively_at_limit(
    row: dict[str, Any],
    asset: dict[str, Any],
    side: str,
    reference_px: Decimal,
    position_size: Decimal,
    position_value: Decimal,
    max_position_value: Decimal,
    policy: str,
) -> bool:
    gap_key = "topup_buy_gap" if side == "buy" else "topup_sell_gap"
    gap = Decimal(str(row.get(gap_key) or row["gap_rate"]))
    is_buy = side == "buy"
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    price = rounded_perp_price(reference_px * (Decimal("1") - gap if is_buy else Decimal("1") + gap), sz_decimals)
    if price <= 0:
        return position_value >= max_position_value
    size_key = "topup_buy_size" if is_buy else "topup_sell_size"
    size = Decimal(str(row.get(size_key) or row.get("base_buy_size" if is_buy else "base_sell_size") or "0"))
    if size <= 0:
        return position_value >= max_position_value
    min_notional = Decimal(str(row.get("min_order_value") or "10"))
    size = grid_size_for_min_notional(size, price, sz_decimals, min_notional)
    order_notional = size * price
    min_position_value = Decimal(str(row.get("min_position_value") or "0"))
    return not grid_order_allowed_by_max(
        position_size,
        position_value,
        is_buy,
        order_notional,
        max_position_value,
        policy,
        min_position_value,
    )


def near_grid_orders_if_stale(
    row: dict[str, Any],
    coin: str,
    asset: dict[str, Any],
    side: str,
    reference_px: Decimal,
    position_size: Decimal,
    policy: str,
) -> list[dict[str, Any]]:
    gap = Decimal(str(row["gap_rate"]))
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    is_buy = side == "buy"
    if grid_order_would_add_risk(position_size, is_buy):
        return []

    nearest_px = nearest_active_price(row, side)
    if nearest_px is None:
        return []
    stale_threshold = (
        Decimal("1") - gap * GRID_NEAR_REGRID_STALE_GAP_MULTIPLE
        if is_buy
        else Decimal("1") + gap * GRID_NEAR_REGRID_STALE_GAP_MULTIPLE
    )
    if is_buy and nearest_px >= reference_px * stale_threshold:
        return []
    if not is_buy and nearest_px <= reference_px * stale_threshold:
        return []

    reduce_only = grid_order_should_reduce_only(position_size, is_buy, policy)
    entries: list[dict[str, Any]] = []
    for gap_multiple in (GRID_NEAR_REGRID_TARGET_GAP_MULTIPLE,):
        multiplier = Decimal("1") - gap * gap_multiple if is_buy else Decimal("1") + gap * gap_multiple
        target_px = rounded_perp_price(reference_px * multiplier, sz_decimals)
        if target_px > 0:
            entries.append(grid_order_entry(row, coin, asset, is_buy, target_px, reduce_only))
    return entries


def maintain_grid_legacy(row: dict[str, Any], cache: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
    phase_started_at = time.monotonic()
    last_phase_at = phase_started_at
    phase_timings: list[tuple[str, float]] = []

    def mark_phase(name: str) -> None:
        nonlocal last_phase_at
        now_phase = time.monotonic()
        phase_timings.append((name, now_phase - last_phase_at))
        last_phase_at = now_phase

    cache = cache if cache is not None else {}
    grid_action_phase = cache.get("grid_action_phase")
    phase_limited = grid_action_phase is not None
    allow_p0 = not phase_limited or grid_action_phase == GRID_ACTION_PHASE_P0
    allow_latest_replacement = not phase_limited or grid_action_phase == GRID_ACTION_PHASE_P1_LATEST_REPLACEMENT
    allow_p1_topup = not phase_limited or grid_action_phase == GRID_ACTION_PHASE_P1_TOPUP
    allow_p1_restore = not phase_limited or grid_action_phase == GRID_ACTION_PHASE_P1_RESTORE
    allow_p1_paused_replacement = not phase_limited or grid_action_phase == GRID_ACTION_PHASE_P1_PAUSED_REPLACEMENT
    allow_p2 = not phase_limited or grid_action_phase == GRID_ACTION_PHASE_P2
    changed = ensure_grid_base_sizes(row)
    network = str(row.get("network") or "mainnet")
    timeout = float(row.get("timeout") or 20)
    raw_coin = batch_row_raw_coin(row)
    dex = str(row.get("dex") or "")
    client_cache = cache.setdefault("clients", {})
    client_key = (network, timeout, dex)
    if client_key not in client_cache:
        client_cache[client_key] = build_worker_clients(cache, network, timeout, raw_coin)
    info, exchange, account, _signer, _role = client_cache[client_key]
    mark_phase("clients")
    coin, asset = resolve_perp_asset(info, batch_row_raw_coin(row))
    mark_phase("asset")
    now = int(cache.setdefault("now", int(time.time())))
    precheck_action_limit(info, account, cache, network, now)
    now_ms = now * 1000
    stale_margin_pauses_cleared = clear_stale_grid_margin_pauses(row, now)
    start_ms = int(row.get("last_fill_check_ms") or (now - 24 * 60 * 60) * 1000)
    start_ms = max(0, min(start_ms - 5 * 60 * 1000, (now - GRID_FILL_LOOKBACK_SECONDS) * 1000))
    max_position_value = Decimal(str(row["max_position_value"]))
    min_position_value = Decimal(str(row.get("min_position_value") or "0"))
    policy = grid_limit_policy_from_row(row)
    mids_cache = cache.setdefault("mids", {})
    mids_key = (network, dex)
    if mids_key not in mids_cache:
        mids_cache[mids_key] = info.all_mids(dex)
    mids = mids_cache[mids_key]
    current_mid = Decimal(str(mids[coin]))
    row["lifecycle_mid"] = decimal_to_plain(current_mid)
    mark_phase("mids")
    # The worker already uses one mid snapshot for the whole run. Reuse one
    # order-book snapshot per coin as well; repeatedly refreshing it in every
    # global action phase was a large source of REST wait time. The phase
    # ordering still prevents lower-priority actions from running early.
    books_cache = cache.setdefault("books", {})
    books_key = (network, coin)
    if books_key not in books_cache:
        books_cache[books_key] = best_bid_ask(info, coin)
    best_bid, best_ask = books_cache[books_key]
    mark_phase("book")
    pending_restored = 0
    user_state_cache = cache.setdefault("user_states", {})
    user_state_key = (network, account, dex)
    if user_state_key not in user_state_cache:
        user_state_cache[user_state_key] = info.user_state(account, dex=dex)
        log_event(f"worker_user_state:{dex or 'default'}", user_state_cache[user_state_key])
    current_position = find_current_position_from_state(user_state_cache[user_state_key], coin)
    mark_phase("position")
    if current_position is None:
        position_size = Decimal("0")
        position_value = Decimal("0")
        liquidation_px = None
        position_roe = None
    else:
        position_size = Decimal(str(current_position.get("szi", "0")))
        position_value = decimal_or_none(current_position.get("positionValue"))
        if position_value is None:
            position_value = abs(position_size * current_mid)
        else:
            position_value = abs(position_value)
        liquidation_px = decimal_or_none(current_position.get("liquidationPx"))
        position_roe = decimal_or_none(current_position.get("returnOnEquity"))
    if grid_action_phase == GRID_ACTION_PHASE_P0:
        record_grid_limit_chase_candidate(cache, row, position_size, position_value)
    position_roe_for_controls = grid_roe_for_position_value(position_value, position_roe)
    previous_avg_state = (
        row.get("topup_buy_size"),
        row.get("topup_sell_size"),
        row.get("topup_buy_gap"),
        row.get("topup_sell_gap"),
        row.get("avg_multiplier"),
        row.get("avg_favored_side"),
        row.get("avg_current_value"),
        row.get("effective_gap_rate"),
    )
    avg_value = decimal_or_none(row.get("avg"))
    if avg_value is not None:
        base_buy_size = Decimal(str(row.get("base_buy_size") or row.get("buy_size") or "0"))
        base_sell_size = Decimal(str(row.get("base_sell_size") or row.get("sell_size") or "0"))
        avg_multiplier, avg_favored_side, avg_current_value = grid_avg_multiplier(
            policy,
            min_position_value,
            max_position_value,
            avg_value,
            position_size,
            position_value,
        )
        topup_buy_size, topup_sell_size, topup_buy_gap, topup_sell_gap = grid_avg_topup_params(
            Decimal(str(row["gap_rate"])),
            base_buy_size,
            base_sell_size,
            avg_multiplier,
            avg_favored_side,
            int(row.get("sz_decimals") or asset["szDecimals"]),
        )
        row["base_buy_size"] = decimal_to_plain(base_buy_size)
        row["base_sell_size"] = decimal_to_plain(base_sell_size)
        row["buy_size"] = decimal_to_plain(base_buy_size)
        row["sell_size"] = decimal_to_plain(base_sell_size)
        row["topup_buy_size"] = decimal_to_plain(topup_buy_size)
        row["topup_sell_size"] = decimal_to_plain(topup_sell_size)
        row["topup_buy_gap"] = decimal_to_plain(topup_buy_gap)
        row["topup_sell_gap"] = decimal_to_plain(topup_sell_gap)
        if topup_buy_size > topup_sell_size:
            row["actual_trend"] = format_signed_percent(topup_buy_size / topup_sell_size - Decimal("1"))
        elif topup_sell_size > topup_buy_size:
            row["actual_trend"] = format_signed_percent(-(topup_sell_size / topup_buy_size - Decimal("1")))
        else:
            row["actual_trend"] = "0%"
        row["avg_multiplier"] = decimal_to_plain(avg_multiplier)
        row["avg_favored_side"] = avg_favored_side
        row["avg_current_value"] = decimal_to_plain(avg_current_value)
        row["effective_gap_rate"] = decimal_to_plain(Decimal(str(row["gap_rate"])) * avg_multiplier)
    else:
        row["topup_buy_size"] = decimal_to_plain(Decimal(str(row.get("base_buy_size") or row.get("buy_size") or "0")))
        row["topup_sell_size"] = decimal_to_plain(Decimal(str(row.get("base_sell_size") or row.get("sell_size") or "0")))
        row["topup_buy_gap"] = decimal_to_plain(Decimal(str(row["gap_rate"])))
        row["topup_sell_gap"] = decimal_to_plain(Decimal(str(row["gap_rate"])))
        row["effective_gap_rate"] = decimal_to_plain(Decimal(str(row["gap_rate"])))
    mark_phase("avg")
    avg_state_changed = previous_avg_state != (
        row.get("topup_buy_size"),
        row.get("topup_sell_size"),
        row.get("topup_buy_gap"),
        row.get("topup_sell_gap"),
        row.get("avg_multiplier"),
        row.get("avg_favored_side"),
        row.get("avg_current_value"),
        row.get("effective_gap_rate"),
    )
    spot_withdrawable_state = account_spot_withdrawable(info, account, network, cache)
    if spot_withdrawable_state is None:
        withdrawable = None
        total_usdc = None
        withdrawable_ratio = None
    else:
        withdrawable, total_usdc = spot_withdrawable_state
        withdrawable_ratio = withdrawable / total_usdc if total_usdc > 0 else Decimal("0")
    withdrawable_reduce_only = account_withdrawable_reduce_only(withdrawable)
    withdrawable_pause_active = account_withdrawable_pause_active(withdrawable)
    withdrawable_pause_phase = account_withdrawable_pause_phase(withdrawable)
    account_margin_protected = withdrawable_reduce_only
    margin_gap_multiplier = Decimal("1")
    retired_margin_keys = (
        "margin_gap_multiplier",
        "margin_gap_soft_threshold",
        "account_margin_ratio",
        "account_margin_hard_stop",
        "account_margin_protected",
    )
    margin_state_changed = any(key in row for key in retired_margin_keys)
    for key in retired_margin_keys:
        row.pop(key, None)
    row["account_usdc_total"] = decimal_to_plain(total_usdc) if total_usdc is not None else None
    row["account_usdc_withdrawable"] = decimal_to_plain(withdrawable) if withdrawable is not None else None
    row["account_usdc_withdrawable_ratio"] = (
        decimal_to_plain(withdrawable_ratio) if withdrawable_ratio is not None else None
    )
    row["account_usdc_reduce_only_only"] = withdrawable_reduce_only
    row["account_usdc_withdrawable_pause_active"] = withdrawable_pause_active
    row["account_usdc_withdrawable_pause_phase"] = withdrawable_pause_phase
    brake_state_pruned = prune_add_risk_brake_state(row, now)
    mark_phase("margin")

    open_orders_cache = cache.setdefault("open_orders", {})
    open_orders_key = (network, account, dex)
    if open_orders_key not in open_orders_cache:
        open_orders_cache[open_orders_key] = collect_frontend_open_orders(info, account, dex)
    current_open_orders = open_orders_cache[open_orders_key]
    open_oids = open_order_oids(info, account, dex, coin, current_open_orders)
    mark_phase("open_orders")

    fills_cache = cache.setdefault("fills", {})
    common_start_ms = (now - GRID_FILL_LOOKBACK_SECONDS) * 1000
    fills_key = (network, account, common_start_ms, now_ms)
    if fills_key not in fills_cache:
        fills_cache[fills_key] = info.user_fills_by_time(account, common_start_ms, now_ms)
        log_event("grid_user_fills_by_time", {"start_ms": common_start_ms, "end_ms": now_ms, "count": len(fills_cache[fills_key])})
    fills_by_oid = recent_fills_by_oid(info, account, coin, start_ms, now_ms, fills_cache[fills_key])
    mark_phase("fills")
    changed = (
        avg_state_changed
        or margin_state_changed
        or brake_state_pruned
        or stale_margin_pauses_cleared
        or pending_restored > 0
        or bool(cache.pop("pending_cancel_changed", False))
    )
    missing_without_fill: list[int] = []
    recovered_missing = 0
    newly_filled: list[dict[str, Any]] = [
        entry
        for entry in row.get("levels") or []
        if isinstance(entry, dict) and entry.get("side") and bool(entry.get("replacement_pending"))
    ]

    levels = row.setdefault("levels", [])
    submissions_by_side = {"buy": 0, "sell": 0}
    filled_submission_sides: set[str] = set()
    replacement_quota_sides: set[str] = set()
    isolated_leverage_ready: set[str] = set()
    margin_blocked_sides: set[tuple[str, str]] = set()

    def side_submission_allowed(side: str) -> bool:
        return (
            side not in replacement_quota_sides
            and side not in filled_submission_sides
            and submissions_by_side.get(side, 0) < GRID_MAX_SUBMISSIONS_PER_SIDE_PER_RUN
        )

    def submit_tracked(order: dict[str, Any]) -> bool:
        if phase_limited and grid_action_phase not in (
            GRID_ACTION_PHASE_P1_TOPUP,
            GRID_ACTION_PHASE_P1_RESTORE,
        ):
            return False
        side = str(order.get("side") or "")
        if not side_submission_allowed(side):
            return False
        existing_action_limit = action_limit_error(cache)
        budget_tracked = p1_budget_tracked(cache)
        if action_limit_p1_enabled(cache) and budget_tracked and not p1_budget_available(cache):
            if existing_action_limit:
                pause_grid_order_for_action_limit(order, now, existing_action_limit)
            return False
        if existing_action_limit and not action_limit_p1_enabled(cache):
            pause_grid_order_for_action_limit(order, now, existing_action_limit)
            return False
        try:
            order["audit_phase"] = grid_action_phase
            order["audit_deficit"] = action_limit_deficit(cache)
            submitted = submit_grid_order_entry(
                exchange,
                coin,
                order,
                now,
                row,
                asset,
                position_size,
                position_value,
                policy,
                account_margin_protected,
                isolated_leverage_ready,
                margin_blocked_sides=margin_blocked_sides,
                open_orders=current_open_orders,
                cache=cache,
                consume_p1_budget=True,
            )
        except RuntimeError as exc:
            error_text = str(exc)
            if not is_cumulative_action_limit_text(error_text):
                raise
            mark_action_limit_hit(cache, error_text, now)
            pause_grid_order_for_action_limit(order, now, error_text)
            return False
        if submitted:
            submissions_by_side[side] = submissions_by_side.get(side, 0) + 1
            if str(order.get("status")) == "filled":
                filled_submission_sides.add(side)
        return submitted

    def submit_replacement(order: dict[str, Any], *, bypass_current_controls: bool = False) -> bool:
        side = str(order.get("side") or "")
        if (
            not bypass_current_controls
            and not grid_order_is_never_cancel(order)
            and not replacement_active_cap_submit_allowed(row, side)
        ):
            return False
        if not bypass_current_controls:
            existing_action_limit = action_limit_error(cache)
            budget_tracked = p1_budget_tracked(cache)
            if action_limit_p1_enabled(cache) and budget_tracked and not p1_budget_available(cache):
                if existing_action_limit:
                    pause_grid_order_for_action_limit(order, now, existing_action_limit)
                return False
            if existing_action_limit and not action_limit_p1_enabled(cache):
                pause_grid_order_for_action_limit(order, now, existing_action_limit)
                return False
        try:
            order["audit_phase"] = grid_action_phase
            order["audit_deficit"] = action_limit_deficit(cache)
            submitted = submit_grid_order_entry(
                exchange,
                coin,
                order,
                now,
                row,
                asset,
                position_size,
                position_value,
                policy,
                account_margin_protected,
                isolated_leverage_ready,
                True,
                margin_blocked_sides=margin_blocked_sides,
                open_orders=current_open_orders,
                cache=cache,
                consume_p1_budget=not bypass_current_controls,
                bypass_margin_controls=bypass_current_controls,
            )
        except RuntimeError as exc:
            error_text = str(exc)
            if not is_cumulative_action_limit_text(error_text):
                raise
            mark_action_limit_hit(cache, error_text, now)
            pause_grid_order_for_action_limit(order, now, error_text)
            return False
        if submitted:
            submissions_by_side[side] = submissions_by_side.get(side, 0) + 1
            replacement_quota_sides.add(side)
        return submitted

    def submit_panic_reversal(order: dict[str, Any]) -> bool:
        def submit_once() -> bool:
            return submit_grid_order_entry(
                exchange,
                coin,
                order,
                now,
                row,
                asset,
                position_size,
                position_value,
                policy,
                False,
                isolated_leverage_ready,
                True,
                margin_blocked_sides=None,
                open_orders=current_open_orders,
                cache=cache,
            )

        def record_wait(wait_count: int, error_text: str) -> None:
            order["panic_reversal_action_limit_wait_seconds"] = GRID_EMERGENCY_ACTION_LIMIT_WAIT_SECONDS
            order["panic_reversal_action_limit_wait_at"] = now
            order["panic_reversal_action_limit_wait_count"] = wait_count
            order["panic_reversal_action_limit_wait_error"] = error_text

        try:
            submitted = submit_with_cumulative_action_limit_wait(
                submit_once,
                cache=cache,
                on_wait=record_wait,
            )
        except RuntimeError as exc:
            order["panic_reversal_retry_error"] = str(exc)
            order["panic_reversal_retry_at"] = now
            return False
        if submitted:
            side = str(order.get("side") or "")
            submissions_by_side[side] = submissions_by_side.get(side, 0) + 1
        return submitted

    recovery_scan_entries = grid_entries_near_first_per_side(
        [entry for entry in levels if isinstance(entry, dict)]
    )
    for entry in (recovery_scan_entries if allow_latest_replacement else []):
        if not isinstance(entry, dict) or not entry.get("side"):
            continue
        if str(entry.get("status", "active")) not in {
            "active",
            "recovery_deferred",
            GRID_PENDING_CANCEL_STATUS,
        }:
            continue
        try:
            oid = int(entry["oid"])
        except (KeyError, TypeError, ValueError):
            continue
        if oid in open_oids:
            continue
        fill = fills_by_oid.get(oid)
        if fill is None:
            old_oid = oid
            order_status = info.query_order_by_oid(account, old_oid)
            status_name = grid_order_status_name(order_status)
            if mark_missing_order_confirmed_open(
                entry,
                old_oid,
                now,
                order_status,
                current_open_orders,
                coin,
            ):
                open_oids.add(old_oid)
                changed = True
                continue
            if status_name == "filled":
                entry["status"] = "filled"
                entry["filled_at"] = now
                entry["confirmed_filled_oid"] = old_oid
                entry["replacement_pending"] = True
                newly_filled.append(entry)
                changed = True
                continue
            if status_name == "reduceOnlyCanceled":
                pause_reduce_only_canceled_entry(entry, old_oid, now)
                changed = True
                continue
            if mark_pending_cancel_confirmed_cancelled(entry, old_oid, now, order_status):
                changed = True
                continue
            protected_replacement = grid_order_is_never_cancel(entry)
            if not protected_replacement and skip_unknown_oid_grid_recovery(entry, old_oid, now, order_status):
                changed = True
                continue
            side = str(entry.get("side"))
            if not protected_replacement and not grid_missing_recovery_allowed(row, side, open_oids):
                entry["status"] = "recovery_deferred"
                entry["oid"] = old_oid
                entry["recovery_deferred_status"] = GRID_ACTIVE_CAP_PAUSE_STATUS
                entry["recovery_deferred_reason"] = "active_cap"
                entry["recovery_deferred_at"] = now
                entry["recovery_active_cap_allowed"] = GRID_MAX_ACTIVE_ORDERS_PER_SIDE
                changed = True
                continue
            if grid_margin_pause_active(row, side, now, position_value, position_size):
                changed = True
                continue
            if not protected_replacement and skip_stale_grid_recovery(
                entry, old_oid, now, current_mid, best_bid, best_ask
            ):
                changed = True
                continue
            submitted_missing = (
                submit_panic_reversal(entry)
                if protected_replacement
                else submit_tracked(entry)
            )
            if submitted_missing:
                entry["recovered_missing_oid"] = old_oid
                entry["recovered_missing_at"] = now
                missing_without_fill.append(oid)
                recovered_missing += 1
                if entry.get("replacement_pending"):
                    newly_filled.append(entry)
            else:
                if protected_replacement:
                    entry["panic_reversal_missing_oid"] = old_oid
                    preserve_replacement_order(levels, entry, now, "missing_submit_retry")
                    changed = True
                    continue
                deferred_status = str(entry.get("status") or "recovery_deferred")
                if entry.get("action_limit_deferred_at") == now:
                    entry["action_limit_deferred_oid"] = old_oid
                else:
                    entry["status"] = "recovery_deferred"
                    entry["oid"] = old_oid
                    entry["recovery_deferred_status"] = deferred_status
                    entry["recovery_deferred_at"] = now
            changed = True
            continue
        entry["status"] = "filled"
        entry["filled_at"] = int(fill.get("time") or now_ms) // 1000
        entry["fill"] = fill
        entry["replacement_pending"] = True
        newly_filled.append(entry)
        changed = True
    pending_restored = restore_pending_cancel_entries(row, current_mid, cache, now)
    if pending_restored:
        changed = True
    mark_phase("missing_scan")

    pending_replacement_sides = {
        "sell" if bool(entry.get("is_buy")) else "buy"
        for entry in newly_filled
    }
    replacement_quota_sides.update(pending_replacement_sides)
    saved_target_per_side = int(row.get("target_orders_per_side") or GRID_TARGET_ORDERS_PER_SIDE)
    target_per_side = grid_target_orders_per_side(row)
    if target_per_side != saved_target_per_side:
        row["target_orders_per_side"] = target_per_side
        changed = True

    add_risk_braked = 0
    paused = 0

    to_pause_bypassed_margin = (
        grid_bypassed_replacement_margin_pause_candidates(
            row,
            position_size,
            account_margin_protected,
            now,
        )
        if allow_p0
        else []
    )
    if to_pause_bypassed_margin:
        paused_bypassed_margin = cancel_grid_entries(
            exchange,
            coin,
            to_pause_bypassed_margin,
            now,
            "paused_account_margin",
            row=row,
            current_mid=current_mid,
            cache=cache,
        )
        paused += paused_bypassed_margin
        if paused_bypassed_margin:
            changed = True
    paused_bypassed_margin_ids = {id(entry) for entry in to_pause_bypassed_margin}

    to_pause_density: list[dict[str, Any]] = []
    risk_density_allowed: dict[str, int] = {}
    risk_density_multiplier: dict[str, Decimal] = {}
    for side in ("buy", "sell"):
        candidates, allowed, multiplier = grid_risk_density_pause_candidates(
            row,
            side,
            position_size,
            target_per_side,
            margin_gap_multiplier,
        )
        risk_density_allowed[side] = allowed
        risk_density_multiplier[side] = multiplier
        to_pause_density.extend(candidates)
    if to_pause_density and allow_p0:
        paused_density = cancel_grid_entries(
            exchange, coin, to_pause_density, now, GRID_RISK_DENSITY_PAUSE_STATUS,
            row=row, current_mid=current_mid, cache=cache,
        )
        paused += paused_density
        for entry in to_pause_density:
            entry["risk_density_allowed"] = risk_density_allowed.get(str(entry.get("side") or ""), target_per_side)
            entry["risk_density_multiplier"] = decimal_to_plain(
                risk_density_multiplier.get(str(entry.get("side") or ""), Decimal("1"))
            )
            entry["risk_density_paused_at"] = now
    paused_density_ids = {id(entry) for entry in to_pause_density}
    to_pause_roe: list[dict[str, Any]] = []
    roe_allowed: dict[str, int] = {}
    for side in ("buy", "sell"):
        candidates, allowed = grid_roe_pause_candidates(
            row,
            side,
            position_size,
            target_per_side,
            position_roe_for_controls,
        )
        roe_allowed[side] = allowed
        to_pause_roe.extend(
            entry
            for entry in candidates
            if id(entry) not in paused_density_ids
        )
    if to_pause_roe and allow_p0:
        paused_roe = cancel_grid_entries(
            exchange, coin, to_pause_roe, now, GRID_ROE_PAUSE_STATUS,
            row=row, current_mid=current_mid, cache=cache,
        )
        paused += paused_roe
        for entry in to_pause_roe:
            side = str(entry.get("side") or "")
            entry["roe_allowed"] = roe_allowed.get(side, target_per_side)
            entry["roe_paused_at"] = now
    paused_roe_ids = {id(entry) for entry in to_pause_roe}
    if paused:
        changed = True
    mark_phase("pre_replacement_risk_pauses")

    dense_regridded = 0
    near_regrids = 0

    projected_position_values: dict[str, Decimal] = {}
    for side in ("buy", "sell"):
        projected = signed_position_value(position_size, position_value) if policy == "limit" else position_value
        for entry in active_grid_entries(row, side):
            order_notional = Decimal(str(entry.get("size"))) * Decimal(str(entry.get("price", entry.get("limit_px"))))
            if policy == "limit":
                projected += order_notional if bool(entry.get("is_buy")) else -order_notional
            elif grid_order_would_add_risk(position_size, bool(entry.get("is_buy"))):
                projected += order_notional
            else:
                projected = max(Decimal("0"), projected - order_notional)
        projected_position_values[side] = projected
    previous_panic_state = (
        row.get("panic_ratio"),
        row.get("panic_ratio_threshold"),
        row.get("panic_liquidation_px"),
    )
    previous_roe_state = (
        row.get("roe"),
        row.get("roe_density_threshold"),
        row.get("roe_stop_threshold"),
        row.get("roe_add_risk_allowed_buy"),
        row.get("roe_add_risk_allowed_sell"),
        row.get("roe_min_position_value"),
    )
    panic_ratio = grid_panic_ratio(row, position_size, current_mid, liquidation_px)
    panic_threshold = grid_panic_ratio_threshold(row)
    panic_reduced = 0
    row["panic_ratio_threshold"] = decimal_to_plain(panic_threshold)
    row["panic_ratio"] = decimal_to_plain(panic_ratio) if panic_ratio is not None else None
    row["panic_liquidation_px"] = decimal_to_plain(liquidation_px) if liquidation_px is not None else None
    row["roe"] = decimal_to_plain(position_roe) if position_roe is not None else None
    row["roe_density_threshold"] = decimal_to_plain(GRID_ROE_DENSITY_THRESHOLD)
    row["roe_stop_threshold"] = decimal_to_plain(GRID_ROE_STOP_THRESHOLD)
    row["roe_add_risk_allowed_buy"] = roe_allowed.get("buy", target_per_side)
    row["roe_add_risk_allowed_sell"] = roe_allowed.get("sell", target_per_side)
    row["roe_min_position_value"] = decimal_to_plain(GRID_ROE_MIN_POSITION_VALUE)
    if previous_panic_state != (
        row.get("panic_ratio"),
        row.get("panic_ratio_threshold"),
        row.get("panic_liquidation_px"),
    ):
        changed = True
    if previous_roe_state != (
        row.get("roe"),
        row.get("roe_density_threshold"),
        row.get("roe_stop_threshold"),
        row.get("roe_add_risk_allowed_buy"),
        row.get("roe_add_risk_allowed_sell"),
        row.get("roe_min_position_value"),
    ):
        changed = True
    if allow_p0 and panic_ratio is not None and panic_ratio < panic_threshold:
        panic_order = build_grid_panic_reduce_order(exchange, row, coin, asset, current_mid, position_size)
        if panic_order is not None:
            panic_order["panic_ratio"] = decimal_to_plain(panic_ratio)
            panic_order["panic_ratio_threshold"] = decimal_to_plain(panic_threshold)
            panic_reversal = None
            # The reversal price depends on the IOC fill avgPx, so these two
            # exchange actions must be submitted sequentially.
            pair_submitted = False
            keep_paired_reversal = False
            submitted = submit_grid_panic_reduce(exchange, coin, panic_order, now, row, cache)
            if submitted:
                panic_reduced = 1
                row["panic_reduce_at"] = now
                row["panic_reduce_count"] = int(row.get("panic_reduce_count") or 0) + 1
                row["panic_reduce_side"] = panic_order.get("side")
                row["panic_reduce_size"] = panic_order.get("filled_size") or panic_order.get("size")
                row["panic_reduce_price"] = panic_order.get("price")
                row["panic_reduce_ratio"] = decimal_to_plain(panic_ratio)
                row.pop("panic_reduce_error", None)
                row.pop("panic_reduce_error_at", None)
                changed = True
                if pair_submitted and panic_reversal is not None:
                    if keep_paired_reversal:
                        levels.append(panic_reversal)
                    changed = True
                else:
                    panic_filled_avg_px = decimal_or_none(panic_order.get("filled_avg_px"))
                    panic_reversal_anchor_price = panic_filled_avg_px or current_mid
                    panic_reversal_anchor_source = (
                        "market_fill" if panic_filled_avg_px is not None else "mid_fallback"
                    )
                    row["panic_reduce_fill_price"] = decimal_to_plain(panic_reversal_anchor_price)
                    row["panic_reversal_anchor_source"] = panic_reversal_anchor_source
                    panic_reversal = panic_reversal_order_from_reduce(
                        row,
                        coin,
                        asset,
                        panic_reversal_anchor_price,
                        bool(panic_order.get("is_buy")),
                        Decimal(str(panic_order.get("filled_size") or panic_order["size"])),
                        position_size,
                        policy,
                        panic_reversal_anchor_source,
                    )
                if panic_reversal is not None and not pair_submitted:
                    reversal_side = str(panic_reversal.get("side") or "")
                    order_notional = Decimal(str(panic_reversal["size"])) * Decimal(str(panic_reversal["price"]))
                    if not submit_panic_reversal(panic_reversal):
                        preserve_replacement_order(levels, panic_reversal, now)
                    else:
                        levels.append(panic_reversal)
                        projected_position_value = projected_position_values.get(reversal_side, Decimal("0"))
                        if policy == "limit":
                            projected_position_values[reversal_side] += (
                                order_notional if bool(panic_reversal["is_buy"]) else -order_notional
                            )
                        elif grid_order_would_add_risk(position_size, bool(panic_reversal["is_buy"])):
                            projected_position_values[reversal_side] = projected_position_value + order_notional
                        else:
                            projected_position_values[reversal_side] = max(
                                Decimal("0"),
                                projected_position_value - order_notional,
                            )
                    changed = True
                elif panic_reversal is not None and pair_submitted and str(panic_reversal.get("status")) in {"active", "filled"}:
                    reversal_side = str(panic_reversal.get("side") or "")
                    order_notional = Decimal(str(panic_reversal["size"])) * Decimal(str(panic_reversal["price"]))
                    projected_position_value = projected_position_values.get(reversal_side, Decimal("0"))
                    if policy == "limit":
                        projected_position_values[reversal_side] += (
                            order_notional if bool(panic_reversal["is_buy"]) else -order_notional
                        )
                    elif grid_order_would_add_risk(position_size, bool(panic_reversal["is_buy"])):
                        projected_position_values[reversal_side] = projected_position_value + order_notional
                    else:
                        projected_position_values[reversal_side] = max(
                            Decimal("0"),
                            projected_position_value - order_notional,
                        )
            elif pair_submitted and keep_paired_reversal and panic_reversal is not None:
                levels.append(panic_reversal)
                changed = True
    mark_phase("panic")
    enable_action_limit_p1_budget(cache)
    replacements = 0
    for entry in (newly_filled if allow_latest_replacement else []):
        submitted_limit_px = decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0")
        replacement = replacement_order_from_fill(
            row,
            coin,
            asset,
            submitted_limit_px,
            bool(entry.get("is_buy")),
            position_size,
            position_value,
            max_position_value,
            policy,
        )
        if replacement is None:
            continue
        replacement_side = str(replacement["side"])
        entry["replacement_pending"] = False
        entry["replacement_processed_at"] = now
        projected_position_value = projected_position_values[replacement_side]
        order_notional = Decimal(str(replacement["size"])) * Decimal(str(replacement["price"]))
        replacement["immediate_control_bypass_at"] = now
        submitted = submit_replacement(replacement, bypass_current_controls=True)
        if not submitted:
            preserve_replacement_order(levels, replacement, now)
            changed = True
            continue
        levels.append(replacement)
        cache.setdefault("immediate_control_bypass_entry_ids", set()).add(id(replacement))
        order_notional = Decimal(str(replacement["size"])) * Decimal(str(replacement["price"]))
        if policy == "limit":
            projected_position_values[replacement_side] += order_notional if bool(replacement["is_buy"]) else -order_notional
        elif grid_order_would_add_risk(position_size, bool(replacement["is_buy"])):
            projected_position_values[replacement_side] += order_notional
        else:
            projected_position_values[replacement_side] = max(Decimal("0"), projected_position_value - order_notional)
        replacements += 1
        changed = True
    mark_phase("replacements")

    to_pause_limit: list[dict[str, Any]] = []
    high_priority_pause_ids = paused_bypassed_margin_ids | paused_density_ids | paused_roe_ids
    for side in ("buy", "sell"):
        entries = active_grid_entries(row, side)
        entries.sort(
            key=lambda entry: decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0"),
            reverse=side == "buy",
        )
        projected_side_value = signed_position_value(position_size, position_value) if policy == "limit" else position_value
        limit_survival_kept = False
        for entry in entries:
            if id(entry) in high_priority_pause_ids:
                continue
            if grid_order_is_never_cancel(entry):
                continue
            is_buy = bool(entry.get("is_buy"))
            order_notional = Decimal(str(entry.get("size"))) * Decimal(str(entry.get("price", entry.get("limit_px"))))
            bypassed_this_run = id(entry) in cache.get("immediate_control_bypass_entry_ids", set())
            if not bypassed_this_run and not grid_order_allowed_by_max(
                position_size,
                projected_side_value,
                is_buy,
                order_notional,
                max_position_value,
                policy,
                min_position_value,
                position_value_is_signed=policy == "limit",
            ):
                if not limit_survival_kept:
                    entry["limit_survival_slot"] = True
                    limit_survival_kept = True
                else:
                    entry.pop("limit_survival_slot", None)
                    to_pause_limit.append(entry)
                    continue
            else:
                entry.pop("limit_survival_slot", None)
            if policy == "limit":
                projected_side_value += order_notional if is_buy else -order_notional
            elif grid_order_would_add_risk(position_size, is_buy):
                projected_side_value += order_notional
            else:
                projected_side_value = max(Decimal("0"), projected_side_value - order_notional)
    if to_pause_limit:
        limit_paused = cancel_grid_entries_with_p1_budget(
            exchange, coin, to_pause_limit, now, "paused_limit", cache,
            row=row, current_mid=current_mid,
        )
        paused += limit_paused
        if limit_paused:
            changed = True
    paused_limit_ids = {id(entry) for entry in to_pause_limit}
    pending_overflow = pending_cancel_overflow_candidates(row, open_oids)
    if pending_overflow:
        overflow_cancelled = cancel_grid_entries_with_p1_budget(
            exchange,
            coin,
            pending_overflow,
            now,
            GRID_ACTIVE_CAP_PAUSE_STATUS,
            cache,
        )
        paused += overflow_cancelled
        for entry in pending_overflow:
            if str(entry.get("status")) != GRID_ACTIVE_CAP_PAUSE_STATUS or entry.get("cancelled_at") != now:
                continue
            entry["active_cap_allowed"] = GRID_REPLACEMENT_ACTIVE_CAP_SUBMIT_THRESHOLD
            entry["active_cap_paused_at"] = now
            entry["active_cap_pending_overflow"] = True
        if overflow_cancelled:
            changed = True
    to_pause_post_replacement_cap: list[dict[str, Any]] = []
    for side in ("buy", "sell"):
        candidates, _allowed = grid_active_cap_pause_candidates(row, side)
        to_pause_post_replacement_cap.extend(
            entry
            for entry in candidates
            if id(entry) not in high_priority_pause_ids and id(entry) not in paused_limit_ids
        )
    if to_pause_post_replacement_cap:
        active_cap_paused = cancel_grid_entries_with_p1_budget(
            exchange,
            coin,
            to_pause_post_replacement_cap,
            now,
            GRID_ACTIVE_CAP_PAUSE_STATUS,
            cache,
            row=row,
            current_mid=current_mid,
        )
        paused += active_cap_paused
        for entry in to_pause_post_replacement_cap:
            if str(entry.get("status") or "") != GRID_ACTIVE_CAP_PAUSE_STATUS or entry.get("cancelled_at") != now:
                continue
            entry["active_cap_allowed"] = GRID_MAX_ACTIVE_ORDERS_PER_SIDE
            entry["active_cap_paused_at"] = now
        if active_cap_paused:
            changed = True
    mark_phase("post_replacement_limit_cap")

    if allow_p2 and noncritical_grid_work_allowed(cache):
        dense_regridded = regrid_dense_entries(
            exchange,
            coin,
            row,
            asset,
            now,
            position_size,
            position_value,
            policy,
            account_margin_protected,
            isolated_leverage_ready,
            margin_blocked_sides,
            current_mid=current_mid,
            cache=cache,
        )
    if dense_regridded:
        changed = True
    mark_phase("dense")

    replacement_rebalanced = 0
    near_far_rebalanced = 0
    if allow_p2 and isinstance(levels, list) and noncritical_grid_work_allowed(cache):
        nearest_paused_by_side = grid_nearest_non_crossing_paused_entries(
            levels,
            current_mid,
            best_bid,
            best_ask,
        )
        for side in ("buy", "sell"):
            if not side_submission_allowed(side):
                continue
            if grid_margin_pause_active(row, side, now, position_value, position_size):
                continue
            nearest_paused = nearest_paused_by_side.get(side)
            paused_candidates = [
                entry
                for entry in ([nearest_paused] if nearest_paused is not None else [])
                if not (
                    withdrawable_pause_active
                    and str(entry.get("status") or "") == GRID_WITHDRAWABLE_PAUSE_STATUS
                )
                and not grid_recovery_price_would_cross_market(entry, current_mid, best_bid, best_ask)
                and (
                    not grid_order_would_add_risk(position_size, bool(entry.get("is_buy")))
                    or grid_roe_add_risk_allowed(target_per_side, position_roe_for_controls) > 0
                )
                and grid_reduce_only_capacity_available(row, entry, position_size, position_value)
            ]
            pause_entry, restore_entry = grid_near_far_rebalance_pair(
                row,
                side,
                position_size,
                position_value,
                max_position_value,
                policy,
                min_position_value,
                paused_candidates,
            )
            if pause_entry is None or restore_entry is None:
                continue
            prospective_active = [
                entry for entry in active_grid_entries(row, side) if id(entry) != id(pause_entry)
            ] + [restore_entry]
            current_add_risk = sum(
                1
                for entry in active_grid_entries(row, side)
                if grid_order_would_add_risk(position_size, bool(entry.get("is_buy")))
            )
            prospective_add_risk = sum(
                1
                for entry in prospective_active
                if grid_order_would_add_risk(position_size, bool(entry.get("is_buy")))
            )
            prospective_add_risk_allowed = min(
                grid_risk_density_allowed(
                    target_per_side,
                    grid_risk_density_multiplier(row, side, margin_gap_multiplier),
                ),
                grid_roe_add_risk_allowed(target_per_side, position_roe_for_controls),
            )
            if not grid_near_far_add_risk_allowed(
                current_add_risk,
                prospective_add_risk,
                prospective_add_risk_allowed,
            ):
                continue
            pause_status = (
                GRID_REPLACEMENT_PAUSE_STATUS
                if bool(pause_entry.get("replacement_order"))
                else GRID_ACTIVE_CAP_PAUSE_STATUS
            )
            cancelled = cancel_grid_entries(
                exchange, coin, [pause_entry], now, pause_status,
                row=row, current_mid=current_mid, cache=cache,
            )
            if not cancelled:
                continue
            pause_entry["paused_at"] = now
            pause_entry["near_far_rebalanced_at"] = now
            restore_entry["near_far_rebalance_target_at"] = now
            if bool(pause_entry.get("replacement_order")):
                pause_entry["replacement_rebalanced_at"] = now
            if bool(restore_entry.get("replacement_order")):
                restore_entry["replacement_rebalance_target_at"] = now
            prioritize_grid_entry_for_restore(levels, restore_entry)
            paused += cancelled
            near_far_rebalanced += 1
            if bool(pause_entry.get("replacement_order")) or bool(restore_entry.get("replacement_order")):
                replacement_rebalanced += 1
            changed = True
    mark_phase("replacement_rebalance")

    withdrawable_pause_due = withdrawable_pause_phase is not None and (
        not phase_limited or grid_action_phase == withdrawable_pause_phase
    )
    withdrawable_pause_headroom_available = (
        withdrawable_pause_phase != GRID_ACTION_PHASE_P2 or noncritical_grid_work_allowed(cache)
    )
    if withdrawable_pause_due and withdrawable_pause_headroom_available:
        withdrawable_pause_entry = claim_withdrawable_pause_entry(
            row,
            cache.get("grid_rows") if isinstance(cache.get("grid_rows"), list) else [row],
            network,
            account,
            cache,
        )
        if withdrawable_pause_entry is not None:
            if withdrawable_pause_phase == GRID_ACTION_PHASE_P1_WITHDRAWABLE:
                withdrawable_paused = cancel_grid_entries_with_p1_budget(
                    exchange,
                    coin,
                    [withdrawable_pause_entry],
                    now,
                    GRID_WITHDRAWABLE_PAUSE_STATUS,
                    cache,
                    row=row,
                    current_mid=current_mid,
                )
            else:
                withdrawable_paused = cancel_grid_entries(
                    exchange,
                    coin,
                    [withdrawable_pause_entry],
                    now,
                    GRID_WITHDRAWABLE_PAUSE_STATUS,
                    row=row,
                    current_mid=current_mid,
                    cache=cache,
                )
            if withdrawable_paused:
                withdrawable_pause_entry["paused_at"] = now
                withdrawable_pause_entry["withdrawable_paused_at"] = now
                withdrawable_pause_entry["withdrawable_paused_oid"] = withdrawable_pause_entry.get("oid")
                paused += withdrawable_paused
                changed = True
    mark_phase("withdrawable_pause")

    restored = 0
    nearest_paused_by_side = grid_nearest_non_crossing_paused_entries(
        levels,
        current_mid,
        best_bid,
        best_ask,
    )
    for entry in (grid_entries_near_first_per_side(levels) if allow_p1_restore else []):
        if (
            not isinstance(entry, dict)
            or str(entry.get("status")) != GRID_RISK_DENSITY_PAUSE_STATUS
            or bool(entry.get("replacement_order"))
            or entry.get("side") is None
        ):
            continue
        side = str(entry["side"])
        if nearest_paused_by_side.get(side) is not entry:
            continue
        survival_needed = grid_survival_slot_available(row, side)
        if panic_reduced and grid_order_would_add_risk(position_size, side == "buy") and not survival_needed:
            continue
        if not side_submission_allowed(side):
            continue
        if len(active_grid_oids(row, side)) >= GRID_MAX_ACTIVE_ORDERS_PER_SIDE:
            continue
        if not survival_needed and not grid_risk_density_restore_allowed(
            row,
            entry,
            side,
            position_size,
            target_per_side,
            margin_gap_multiplier,
        ):
            continue
        if not survival_needed and not grid_roe_restore_allowed(
            row,
            entry,
            side,
            position_size,
            target_per_side,
            position_roe_for_controls,
        ):
            continue
        if grid_margin_pause_active(row, side, now, position_value, position_size):
            continue
        if defer_paused_grid_restore_if_crossing(entry, now, current_mid, best_bid, best_ask):
            continue
        projected_position_value = projected_position_values[side]
        order_notional = Decimal(str(entry.get("size"))) * Decimal(str(entry.get("price", entry.get("limit_px"))))
        if not grid_order_allowed_by_max_or_survival(
            row,
            entry,
            side,
            position_size,
            projected_position_value,
            order_notional,
            max_position_value,
            policy,
            min_position_value,
        ):
            continue
        if not submit_tracked(entry):
            changed = True
            continue
        if policy == "limit":
            projected_position_values[side] += order_notional if bool(entry.get("is_buy")) else -order_notional
        elif grid_order_would_add_risk(position_size, bool(entry.get("is_buy"))):
            projected_position_values[side] += order_notional
        else:
            projected_position_values[side] = max(Decimal("0"), projected_position_value - order_notional)
        restored += 1
        changed = True
    mark_phase("risk_restore")

    topped_up = 0
    for side in (("buy", "sell") if allow_p1_topup else ()):
        if (
            panic_reduced
            and grid_order_would_add_risk(position_size, side == "buy")
            and not grid_survival_slot_available(row, side)
        ):
            continue
        if not side_submission_allowed(side):
            continue
        if grid_margin_pause_active(row, side, now, position_value, position_size):
            continue
        if grid_order_would_add_risk(position_size, side == "buy"):
            allowed = grid_roe_add_risk_allowed(target_per_side, position_roe_for_controls)
            active_add_risk = [
                active
                for active in active_grid_entries(row, side)
                if grid_order_would_add_risk(position_size, bool(active.get("is_buy")))
            ]
            if len(active_add_risk) >= allowed:
                continue
        remaining_topups = max(0, target_per_side - len(active_grid_entries(row, side)))
        while remaining_topups > 0:
            if not side_submission_allowed(side):
                break
            projected_position_value = projected_position_values[side]
            reference_px = grid_reference_price(side, current_mid, best_bid, best_ask)
            topup = next_depth_order(
                row,
                coin,
                asset,
                side,
                current_mid,
                position_size,
                position_value,
                max_position_value,
                policy,
                reference_px,
                best_bid,
                best_ask,
            )
            if topup is None:
                break
            order_notional = Decimal(str(topup["size"])) * Decimal(str(topup["price"]))
            if not grid_order_allowed_by_max_or_survival(
                row,
                topup,
                side,
                position_size,
                projected_position_value,
                order_notional,
                max_position_value,
                policy,
                min_position_value,
            ):
                topup["status"] = "paused_limit"
                topup["paused_at"] = now
                levels.append(topup)
                changed = True
                break
            submitted = submit_tracked(topup)
            if not submitted:
                if str(topup.get("status")) in GRID_PAUSED_STATUSES:
                    levels.append(topup)
                changed = True
                break
            levels.append(topup)
            order_notional = Decimal(str(topup["size"])) * Decimal(str(topup["price"]))
            remaining_topups -= 1
            if policy == "limit":
                projected_position_values[side] += order_notional if bool(topup["is_buy"]) else -order_notional
            elif grid_order_would_add_risk(position_size, bool(topup["is_buy"])):
                projected_position_values[side] += order_notional
            else:
                projected_position_values[side] = max(Decimal("0"), projected_position_value - order_notional)
            topped_up += 1
            changed = True
    mark_phase("topups")

    restore_scan_entries = grid_entries_near_first_per_side(levels)
    nearest_paused_by_side = grid_nearest_non_crossing_paused_entries(
        levels,
        current_mid,
        best_bid,
        best_ask,
    )
    for entry in (restore_scan_entries if (allow_p1_restore or allow_p1_paused_replacement) else []):
        if isinstance(entry, dict) and pause_refresh_reduce_only_replacement(entry, now):
            changed = True
            continue
        if isinstance(entry, dict) and pause_skipped_account_margin_replacement(levels, entry, now):
            changed = True
            continue
        protected_reduce_only_restore = (
            isinstance(entry, dict)
            and withdrawable_protected_paused_restore(
                entry,
                position_size,
                account_margin_protected,
            )
        )
        if (
            isinstance(entry, dict)
            and str(entry.get("status")) == "paused_account_margin"
        ):
            if protected_reduce_only_restore:
                entry["status"] = (
                    GRID_REPLACEMENT_PAUSE_STATUS
                    if entry.get("replacement_order")
                    else "paused_margin"
                )
                entry["withdrawable_reduce_only_restore_migrated_at"] = now
                changed = True
            elif entry.get("replacement_order"):
                preserve_replacement_order(levels, entry, now, "paused_account_margin")
                changed = True
                continue
            else:
                # Migrate levels saved by older workers so they are not restored at
                # stale prices after account-margin protection ends.
                entry["status"] = "skipped_account_margin"
                entry["oid"] = None
                entry["skipped_at"] = now
                changed = True
                continue
        if not isinstance(entry, dict) or entry.get("side") is None or str(entry.get("status")) not in GRID_PAUSED_STATUSES:
            continue
        side = str(entry["side"])
        if nearest_paused_by_side.get(side) is not entry:
            continue
        survival_needed = grid_survival_slot_available(row, side)
        is_replacement_order = bool(entry.get("replacement_order"))
        protected_replacement = grid_order_is_never_cancel(entry)
        if allow_p1_paused_replacement and not is_replacement_order:
            continue
        if allow_p1_restore and is_replacement_order:
            continue
        if protected_reduce_only_restore and not withdrawable_protected_restore_submission_available(
            cache,
            network,
            account,
            coin,
            side,
        ):
            continue
        status = str(entry.get("status"))
        if (
            status == GRID_WITHDRAWABLE_PAUSE_STATUS
            and withdrawable_pause_active
            and not protected_reduce_only_restore
        ):
            continue
        if normalize_margin_paused_replacement(entry, now):
            status = str(entry.get("status"))
            changed = True
        if (
            panic_reduced
            and not is_replacement_order
            and grid_order_would_add_risk(position_size, side == "buy")
            and not survival_needed
        ):
            continue
        if not side_submission_allowed(side):
            continue
        if (
            not protected_replacement
            and status == GRID_ACTIVE_CAP_PAUSE_STATUS
            and not grid_active_cap_restore_allowed(row, entry, side)
        ):
            continue
        if not protected_replacement and not survival_needed and not grid_roe_restore_allowed(
            row,
            entry,
            side,
            position_size,
            target_per_side,
            position_roe_for_controls,
        ):
            continue
        if not is_replacement_order and not survival_needed:
            if status == GRID_RISK_DENSITY_PAUSE_STATUS and grid_order_would_add_risk(position_size, bool(entry.get("is_buy"))):
                if not grid_risk_density_restore_allowed(
                    row,
                    entry,
                    side,
                    position_size,
                    target_per_side,
                    margin_gap_multiplier,
                ):
                    continue
            elif status == GRID_ACTIVE_CAP_PAUSE_STATUS:
                if grid_order_would_add_risk(position_size, bool(entry.get("is_buy"))):
                    if not grid_risk_density_restore_allowed(
                        row,
                        entry,
                        side,
                        position_size,
                        target_per_side,
                        margin_gap_multiplier,
                    ):
                        continue
            elif len(active_grid_oids(row, side)) >= target_per_side:
                continue
        if (
            not protected_reduce_only_restore
            and grid_margin_pause_active(row, side, now, position_value, position_size)
        ):
            continue
        if not protected_replacement and defer_paused_grid_restore_if_crossing(
            entry, now, current_mid, best_bid, best_ask
        ):
            continue
        projected_position_value = projected_position_values[side]
        order_notional = Decimal(str(entry.get("size"))) * Decimal(str(entry.get("price", entry.get("limit_px"))))
        if (
            not protected_replacement
            and not protected_reduce_only_restore
            and not grid_order_allowed_by_max_or_survival(
                row,
                entry,
                side,
                position_size,
                projected_position_value,
                order_notional,
                max_position_value,
                policy,
                min_position_value,
            )
        ):
            if is_replacement_order:
                preserve_replacement_order(levels, entry, now, "limit_still_blocked", normalize_margin=True)
            continue
        submitted = submit_replacement(entry) if is_replacement_order else submit_tracked(entry)
        if not submitted:
            if is_replacement_order:
                preserve_replacement_order(levels, entry, now, normalize_margin=True)
            changed = True
            continue
        if protected_reduce_only_restore:
            mark_withdrawable_protected_restore_submitted(
                cache,
                network,
                account,
                coin,
                side,
            )
        order_notional = Decimal(str(entry.get("size"))) * Decimal(str(entry.get("price", entry.get("limit_px"))))
        if policy == "limit":
            projected_position_values[side] += order_notional if bool(entry.get("is_buy")) else -order_notional
        elif grid_order_would_add_risk(position_size, bool(entry.get("is_buy"))):
            projected_position_values[side] += order_notional
        else:
            projected_position_values[side] = max(Decimal("0"), projected_position_value - order_notional)
        restored += 1
        changed = True
    mark_phase("paused_restore")

    trimmed = 0 if phase_limited else trim_excess_grid_entries(exchange, coin, row, target_per_side, now)
    if trimmed:
        changed = True
    side_cap_cleared = 0 if phase_limited else clear_grid_side_cap_entries(exchange, coin, row, now)
    if side_cap_cleared:
        changed = True
    changed = changed or bool(cache.pop("pending_cancel_changed", False))
    mark_phase("trim")

    row["status"] = "active"
    row.pop("error", None)
    row.pop("last_error", None)
    row["updated_at"] = now
    row["last_fill_check_ms"] = now_ms
    row["position_value"] = decimal_to_plain(position_value)
    row["position_size"] = decimal_to_plain(position_size)
    row["add_risk_protected"] = account_margin_protected
    row["open_oids"] = sorted(grid_batch_open_oids(row))
    note_values = {
        "replacements": replacements,
        "replacement_rebalanced": replacement_rebalanced,
        "near_far_rebalanced": near_far_rebalanced,
        "topped_up": topped_up,
        "paused": paused,
        "dense_regridded": dense_regridded,
        "restored": restored,
        "trimmed": trimmed,
        "near_regrids": near_regrids,
        "add_risk_braked": add_risk_braked,
        "side_cap_cleared": side_cap_cleared,
        "recovered_missing": recovered_missing,
        "panic_reduced": panic_reduced,
        "submissions_buy": submissions_by_side["buy"],
        "submissions_sell": submissions_by_side["sell"],
    }
    if phase_limited:
        run_counters = cache.setdefault("grid_run_counters", {}).setdefault(
            id(row),
            {key: 0 for key in note_values},
        )
        for key, value in note_values.items():
            run_counters[key] = int(run_counters.get(key, 0) or 0) + int(value or 0)
        note_values = dict(run_counters)
    margin_cooldowns = ",".join(sorted((row.get("margin_pauses") or {}).keys())) or "-"
    action_limit_label = "1" if action_limit_error(cache) else "0"
    action_limit_budget = action_limit_p1_budget_remaining(cache)
    action_limit_budget_label = "-" if action_limit_budget is None else str(action_limit_budget)
    row["note"] = (
        f"grid maintained; replacements={note_values['replacements']}; replacement_rebalanced={note_values['replacement_rebalanced']}; near_far_rebalanced={note_values['near_far_rebalanced']}; topped_up={note_values['topped_up']}; "
        f"paused={note_values['paused']}; dense_regridded={note_values['dense_regridded']}; restored={note_values['restored']}; trimmed={note_values['trimmed']}; near_regrids={note_values['near_regrids']}; "
        f"add_risk_braked={note_values['add_risk_braked']}; side_cap_cleared={note_values['side_cap_cleared']}; "
        f"recovered_missing={note_values['recovered_missing']}; margin_cooldown={margin_cooldowns}; "
        f"submissions=buy:{note_values['submissions_buy']},sell:{note_values['submissions_sell']}; "
        f"action_limit={action_limit_label}; action_limit_p1_budget={action_limit_budget_label}; "
        f"filled_stop={','.join(sorted(filled_submission_sides)) or '-'}; "
        f"avg={row.get('avg') if row.get('avg') is not None else '-'}; avg_multiplier={row.get('avg_multiplier', '1')}; "
        f"withdrawable={row.get('account_usdc_withdrawable') or '-'}; add_risk_protected={int(account_margin_protected)}; "
        f"roe={row.get('roe') or '-'}; roe_allowed=buy:{row.get('roe_add_risk_allowed_buy')},sell:{row.get('roe_add_risk_allowed_sell')}; "
        f"panic_ratio={row.get('panic_ratio') or '-'}; panic_reduced={note_values['panic_reduced']}"
    )
    total_elapsed = time.monotonic() - phase_started_at
    if total_elapsed >= 30:
        phase_text = ",".join(f"{name}:{elapsed:.2f}s" for name, elapsed in phase_timings if elapsed >= 0.01)
        print(f"trail_worker: grid phases {network}:{coin} total={total_elapsed:.2f}s {phase_text}")
    return (
        row,
        changed
        or replacements > 0
        or topped_up > 0
        or paused > 0
        or dense_regridded > 0
        or restored > 0
        or trimmed > 0
        or side_cap_cleared > 0
        or near_regrids > 0
        or add_risk_braked > 0
        or recovered_missing > 0
        or panic_reduced > 0,
    )


def prune_done_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    cutoff = int(time.time()) - DONE_RETENTION_DAYS * 24 * 60 * 60
    kept: list[dict[str, Any]] = []
    recent_done: list[dict[str, Any]] = []

    for row in rows:
        if row.get("status") != "done":
            kept.append(row)
            continue
        done_at = int(row.get("done_at") or row.get("updated_at") or 0)
        if done_at >= cutoff:
            recent_done.append(row)

    if len(recent_done) > DONE_RETENTION_MAX:
        recent_done = sorted(
            recent_done,
            key=lambda item: int(item.get("done_at") or item.get("updated_at") or 0),
        )[-DONE_RETENTION_MAX:]

    pruned = kept + recent_done
    return pruned, len(pruned) != len(rows)


def prune_cancelled_grid_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    pruned = [
        row
        for row in rows
        if not (
            isinstance(row, dict)
            and row.get("type") == "grid"
            and row.get("status") == "cancelled"
        )
    ]
    return pruned, len(pruned) != len(rows)


def grid_level_updated_at(entry: dict[str, Any]) -> int:
    for key in (
        "near_far_rebalance_target_at",
        "submitted_at",
        "recovered_at",
        "filled_at",
        "cancelled_at",
        "skipped_at",
        "paused_at",
        "resized_min_notional_at",
        "resized_min_retry_at",
    ):
        value = entry.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def grid_paused_dedupe_key(entry: dict[str, Any]) -> tuple[str, str, str, bool]:
    return (
        str(entry.get("side")),
        str(entry.get("price", entry.get("limit_px", ""))),
        str(entry.get("size", "")),
        bool(entry.get("reduce_only", False)),
    )


def replacement_pause_keep_score(entry: dict[str, Any]) -> tuple[int, int]:
    status = str(entry.get("status", ""))
    status_priority = {
        GRID_REPLACEMENT_PAUSE_STATUS: 4,
        "paused_margin": 3,
        "skipped_account_margin": 3,
        "paused_limit": 2,
        GRID_ACTIVE_CAP_PAUSE_STATUS: 2,
        "refresh_reduce_only": 2,
        GRID_ACTION_LIMIT_PAUSE_STATUS: 1,
        "paused_action_rate_limit": 1,
    }.get(status, 0)
    return status_priority, grid_level_updated_at(entry)


def dedupe_paused_replacement_orders(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept_by_key: dict[tuple[str, str, str, bool], dict[str, Any]] = {}
    for entry in entries:
        if grid_order_is_never_cancel(entry):
            continue
        key = grid_paused_dedupe_key(entry)
        current = kept_by_key.get(key)
        if current is None or replacement_pause_keep_score(entry) > replacement_pause_keep_score(current):
            kept_by_key[key] = entry
    return [
        entry
        for entry in entries
        if grid_order_is_never_cancel(entry)
        or kept_by_key.get(grid_paused_dedupe_key(entry)) is entry
    ]


def prune_grid_levels(row: dict[str, Any]) -> bool:
    if row.get("type") != "grid":
        return False
    levels = row.get("levels")
    if not isinstance(levels, list):
        return False
    if int(row.get("grid_lifecycle_version") or 0) >= GRID_LIFECYCLE_VERSION:
        kept = [
            entry
            for entry in levels
            if not isinstance(entry, dict)
            or (
                str(entry.get("status") or "") not in {
                    "cancelled",
                    "discarded",
                    "filled_replaced",
                    "skipped_account_margin",
                    "skipped_post_only",
                    "skipped_exchange_reject",
                }
                and not (
                    str(entry.get("status") or "") == "filled"
                    and not bool(entry.get("replacement_pending"))
                )
            )
        ]
        if len(kept) == len(levels):
            return False
        row["levels"] = kept
        row["history_pruned_at"] = int(time.time())
        return True

    live_statuses = {
        "active",
        "pending",
        "recovery_deferred",
        GRID_PENDING_CANCEL_STATUS,
    }
    live_levels: list[dict[str, Any]] = []
    paused_levels: list[dict[str, Any]] = []
    history_levels: list[dict[str, Any]] = []
    passthrough: list[Any] = []

    for entry in levels:
        if not isinstance(entry, dict) or not entry.get("side"):
            passthrough.append(entry)
            continue
        status = str(entry.get("status", "active"))
        if status == "cancelled":
            continue
        if status in live_statuses:
            live_levels.append(entry)
        elif status in GRID_PAUSED_STATUSES:
            paused_levels.append(entry)
        else:
            history_levels.append(entry)

    target_per_side = int(row.get("target_orders_per_side") or GRID_TARGET_ORDERS_PER_SIDE)
    active_counts = {
        side: len(active_grid_oids(row, side))
        for side in ("buy", "sell")
    }
    kept_paused: list[dict[str, Any]] = dedupe_paused_replacement_orders(
        [entry for entry in paused_levels if bool(entry.get("replacement_order"))]
    )
    for side in ("buy", "sell"):
        keep_count = max(0, target_per_side - active_counts[side])
        if keep_count == 0:
            continue
        side_paused = sorted(
            (
                entry
                for entry in paused_levels
                if str(entry.get("side")) == side and not bool(entry.get("replacement_order"))
            ),
            key=grid_level_updated_at,
            reverse=True,
        )
        seen: set[tuple[str, str, str, bool]] = set()
        side_kept = 0
        for entry in side_paused:
            key = grid_paused_dedupe_key(entry)
            if key in seen:
                continue
            seen.add(key)
            kept_paused.append(entry)
            side_kept += 1
            if side_kept >= keep_count:
                break

    history_levels = sorted(history_levels, key=grid_level_updated_at)[-GRID_LEVEL_HISTORY_MAX:]
    pruned_levels = passthrough + live_levels + kept_paused + history_levels
    if len(pruned_levels) == len(levels):
        return False
    row["levels"] = pruned_levels
    row["history_pruned_at"] = int(time.time())
    return True


def prune_grid_level_history(rows: list[dict[str, Any]]) -> bool:
    changed = False
    for row in rows:
        changed = prune_grid_levels(row) or changed
    return changed


def save_worker_progress(rows: list[dict[str, Any]], changed: bool) -> bool:
    if changed:
        save_server_batch(rows)
    return False


def reconcile_cached_grid_open_orders(
    rows: list[dict[str, Any]],
    active_grid_indexes: list[int],
    grid_cache: dict[str, Any],
) -> None:
    """Keep the worker-level open-order snapshot consistent with our own actions.

    The exchange snapshot is intentionally fetched once per worker run. Orders
    that this worker cancelled are removed here, while newly submitted orders
    are already appended by ``record_submitted_open_grid_order``.
    """
    current_tracked_oids: set[int] = set()
    for index in active_grid_indexes:
        current_tracked_oids.update(grid_batch_open_oids(rows[index]))
    previous_tracked_oids = grid_cache.setdefault("tracked_grid_open_oids", current_tracked_oids.copy())
    removed_oids = previous_tracked_oids - current_tracked_oids
    if removed_oids:
        for open_orders in grid_cache.get("open_orders", {}).values():
            if isinstance(open_orders, list):
                retained: list[dict[str, Any]] = []
                for order in open_orders:
                    if not isinstance(order, dict):
                        continue
                    try:
                        oid = int(order.get("oid", -1))
                    except (TypeError, ValueError):
                        oid = -1
                    if oid not in removed_oids:
                        retained.append(order)
                open_orders[:] = retained
    grid_cache["tracked_grid_open_oids"] = current_tracked_oids


def submit_grid_limit_chase_market(
    exchange: Any,
    coin: str,
    order: dict[str, Any],
    now: int,
    row: dict[str, Any],
    cache: dict[str, Any] | None = None,
) -> bool:
    plan = order.get("plan")
    if not isinstance(plan, dict):
        return False
    def submit_once() -> Any:
        reserve_grid_exchange_actions(cache)
        return exchange.order(
            coin,
            bool(plan["is_buy"]),
            float(plan["size"]),
            float(plan["limit_px"]),
            plan["order_type"],
            reduce_only=bool(plan.get("reduce_only")),
        )

    def record_wait(wait_count: int, error_text: str) -> None:
        row["limit_chase_market_action_limit_wait_seconds"] = GRID_EMERGENCY_ACTION_LIMIT_WAIT_SECONDS
        row["limit_chase_market_action_limit_wait_at"] = now
        row["limit_chase_market_action_limit_wait_count"] = wait_count
        row["limit_chase_market_action_limit_wait_error"] = error_text

    result = submit_with_cumulative_action_limit_wait(
        submit_once,
        cache=cache,
        on_wait=record_wait,
    )
    audit_grid_action(
        "limit_chase_market_submit",
        coin=coin,
        side=order.get("side"),
        price=order.get("price"),
        size=order.get("size"),
        reduce_only=bool(plan.get("reduce_only")),
        result=result,
    )
    if not isinstance(result, dict) or result.get("status") != "ok":
        row["limit_chase_error"] = str(result)
        row["limit_chase_error_at"] = now
        return False
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    for status in statuses:
        if not isinstance(status, dict):
            continue
        if status.get("error"):
            row["limit_chase_error"] = str(status["error"])
            row["limit_chase_error_at"] = now
            return False
        filled = status.get("filled")
        if not isinstance(filled, dict):
            continue
        order["oid"] = int(filled.get("oid", 0))
        order["status"] = "filled"
        order["filled_at"] = now
        order["last_submit_status"] = status
        filled_size = decimal_or_none(filled.get("totalSz", filled.get("sz")))
        if filled_size is not None and filled_size > 0:
            order["filled_size"] = decimal_to_plain(filled_size)
        filled_avg_px = decimal_or_none(filled.get("avgPx"))
        if filled_avg_px is not None and filled_avg_px > 0:
            order["filled_avg_px"] = decimal_to_plain(filled_avg_px)
        row.pop("limit_chase_error", None)
        row.pop("limit_chase_error_at", None)
        return True
    row["limit_chase_error"] = f"limit chase market response did not include a fill: {result}"
    row["limit_chase_error_at"] = now
    return False


def run_grid_limit_chase_p3(cache: dict[str, Any]) -> bool:
    candidates = cache.get("limit_chase_candidates")
    if (
        not cache.get("limit_chase_p1_completed")
        or not isinstance(candidates, list)
        or not candidates
    ):
        return False
    candidate = random.choice(candidates)
    row = candidate.get("row") if isinstance(candidate, dict) else None
    if not isinstance(row, dict) or not grid_row_recoverable_from_error(row):
        return False
    cache["api_stat_context"] = str(row.get("coin") or "-")

    network = str(row.get("network") or "mainnet")
    timeout = float(row.get("timeout") or 20)
    raw_coin = batch_row_raw_coin(row)
    dex = str(row.get("dex") or "")
    client_key = (network, timeout, dex)
    client_cache = cache.setdefault("clients", {})
    if client_key not in client_cache:
        client_cache[client_key] = build_worker_clients(cache, network, timeout, raw_coin)
    info, exchange, account, _signer, _role = client_cache[client_key]
    clear_info_cache(info)
    coin, asset = resolve_perp_asset(info, raw_coin)
    mids = info.all_mids(dex)
    current_mid = Decimal(str(mids[coin]))

    user_state = info.user_state(account, dex=dex)
    cache.setdefault("user_states", {})[(network, account, dex)] = user_state
    current_position = find_current_position_from_state(user_state, coin)
    if current_position is None:
        position_size = Decimal("0")
        position_value = Decimal("0")
    else:
        position_size = Decimal(str(current_position.get("szi", "0")))
        position_value = decimal_or_none(current_position.get("positionValue"))
        if position_value is None:
            position_value = abs(position_size * current_mid)
        else:
            position_value = abs(position_value)

    spot_states = cache.setdefault("spot_user_states", {})
    spot_states.pop((network, account), None)
    spot_withdrawable_state = account_spot_withdrawable(info, account, network, cache)
    withdrawable = spot_withdrawable_state[0] if spot_withdrawable_state is not None else None
    row["limit_chase_checked_at"] = int(time.time())
    row["limit_chase_startup_position_value"] = candidate.get("startup_position_value")
    row["limit_chase_position_value"] = decimal_to_plain(
        signed_position_value(position_size, position_value)
    )
    row["limit_chase_withdrawable"] = decimal_to_plain(withdrawable) if withdrawable is not None else None

    is_buy = grid_limit_chase_direction(row, position_size, position_value)
    if is_buy is None:
        row["limit_chase_status"] = "skipped_back_within_limit"
        return True
    reduction_mode = (
        (is_buy and position_size < 0)
        or (not is_buy and position_size > 0)
    )
    if not reduction_mode:
        return False
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    reduction_target = grid_limit_chase_reduction_target_size(
        row,
        position_size,
        position_value,
        current_mid,
        is_buy,
        sz_decimals,
    )
    market_order = build_grid_limit_chase_market_order(
        exchange,
        row,
        coin,
        asset,
        current_mid,
        is_buy,
        max_size=abs(position_size),
        target_size=reduction_target,
    )
    if market_order is None:
        row["limit_chase_status"] = "skipped_invalid_market_order"
        return True
    isolated_leverage_ready: set[str] = set()

    now = int(row["limit_chase_checked_at"])
    if not submit_grid_limit_chase_market(exchange, coin, market_order, now, row, cache):
        row["limit_chase_status"] = "failed_market"
        return True

    filled_size = decimal_or_none(market_order.get("filled_size")) or decimal_or_none(market_order.get("size"))
    filled_avg_px = decimal_or_none(market_order.get("filled_avg_px"))
    replacement_anchor_price = filled_avg_px or current_mid
    replacement_anchor_source = "market_fill" if filled_avg_px is not None else "mid_fallback"
    row["limit_chase_fill_price"] = decimal_to_plain(replacement_anchor_price)
    row["limit_chase_replacement_anchor_source"] = replacement_anchor_source
    replacement = limit_chase_replacement_order_from_market(
        row,
        coin,
        asset,
        replacement_anchor_price,
        is_buy,
        filled_size or Decimal("0"),
        replacement_anchor_source,
    )
    if replacement is None:
        row["limit_chase_status"] = "market_filled_replacement_invalid"
        return True

    levels = row.setdefault("levels", [])
    open_orders = cache.setdefault("open_orders", {}).setdefault((network, account, dex), [])
    def submit_replacement_once() -> bool:
        return submit_grid_order_entry(
            exchange,
            coin,
            replacement,
            now,
            row,
            asset,
            position_size,
            position_value,
            "limit",
            False,
            isolated_leverage_ready,
            True,
            margin_blocked_sides=None,
            open_orders=open_orders,
            cache=cache,
            bypass_margin_controls=True,
        )

    def record_replacement_wait(wait_count: int, error_text: str) -> None:
        replacement["limit_chase_replacement_action_limit_wait_seconds"] = (
            GRID_EMERGENCY_ACTION_LIMIT_WAIT_SECONDS
        )
        replacement["limit_chase_replacement_action_limit_wait_at"] = now
        replacement["limit_chase_replacement_action_limit_wait_count"] = wait_count
        replacement["limit_chase_replacement_action_limit_wait_error"] = error_text

    try:
        replacement_submitted = submit_with_cumulative_action_limit_wait(
            submit_replacement_once,
            cache=cache,
            on_wait=record_replacement_wait,
        )
    except Exception as exc:
        replacement["last_error"] = str(exc)
        replacement["limit_chase_replacement_error_at"] = now
        replacement_submitted = False
    if not replacement_submitted:
        preserve_replacement_order(levels, replacement, now, "limit_chase_submit_failed")
    elif replacement not in levels:
        levels.append(replacement)

    row["limit_chase_status"] = "submitted" if replacement_submitted else "market_filled_replacement_paused"
    row["limit_chase_side"] = market_order["side"]
    row["limit_chase_size"] = market_order["size"]
    row["limit_chase_price"] = market_order["price"]
    row["limit_chase_replacement_price"] = replacement["price"]
    row["limit_chase_at"] = now
    row["updated_at"] = now
    row["note"] = f"{row.get('note') or 'grid maintained'}; limit_chase={row['limit_chase_status']}"
    return True


def migrate_legacy_grid_position_limit_mode(row: dict[str, Any]) -> bool:
    """Canonicalize legacy abs/long/short bounds as an equivalent signed limit."""
    policy = grid_limit_policy_from_row(row)
    if policy == "limit":
        return False
    minimum = Decimal(str(row.get("min_position_value") or "0"))
    maximum = Decimal(str(row.get("max_position_value") or "0"))
    lower_bound, upper_bound = grid_position_bounds(policy, minimum, maximum)
    row["position_limit_mode"] = "limit"
    row["min_position_value"] = decimal_to_plain(lower_bound)
    row["max_position_value"] = decimal_to_plain(upper_bound)
    row.pop("max_position_mode", None)
    return True


def migrate_grid_lifecycle(row: dict[str, Any], now: int) -> bool:
    """Move a saved grid into the finite-chain lifecycle without reviving old controls."""
    changed = migrate_legacy_grid_position_limit_mode(row)
    if int(row.get("grid_lifecycle_version") or 0) >= GRID_LIFECYCLE_VERSION:
        return changed
    levels = row.setdefault("levels", [])
    migrated_levels: list[Any] = []
    for entry in levels:
        if not isinstance(entry, dict) or not entry.get("side"):
            migrated_levels.append(entry)
            continue
        status = str(entry.get("status") or "active")
        entry.pop("replace_never_cancel", None)
        entry.pop("panic_reversal_order", None)
        entry.pop("limit_chase_replacement", None)
        if status.startswith("paused_") or status in GRID_PAUSED_STATUSES:
            entry["legacy_pause_status"] = status
            entry["status"] = GRID_LEGACY_PAUSE_STATUS
            entry["oid"] = None
            entry["grid_leg"] = 1
            entry["legacy_pause_migrated_at"] = now
            migrated_levels.append(entry)
            changed = True
            continue
        if status in {"active", "pending", "recovery_deferred", GRID_PENDING_CANCEL_STATUS}:
            entry["status"] = "active"
            entry["grid_leg"] = 0
            migrated_levels.append(entry)
            changed = True
            continue
        if status == "filled" and not bool(entry.get("replacement_pending")):
            # Persisted filled rows are history, not fresh chain debt.  Only an
            # explicit replacement_pending marker may carry a fill into P2.
            changed = True
            continue
        if bool(entry.get("replacement_pending")):
            entry.setdefault("grid_leg", 0)
            changed = True
        migrated_levels.append(entry)
    row["levels"] = migrated_levels
    row["grid_lifecycle_version"] = GRID_LIFECYCLE_VERSION
    row["grid_lifecycle_migrated_at"] = now
    row.pop("target_orders_per_side", None)
    row.pop("margin_pauses", None)
    initialize_lifecycle_iterations(row)
    return True


def invalidate_unqueued_legacy_pauses(row: dict[str, Any], now: int) -> int:
    """Discard legacy pauses that never made it into the P3 debt queue."""
    levels = row.get("levels")
    if not isinstance(levels, list):
        return 0
    kept = [
        entry
        for entry in levels
        if not (
            isinstance(entry, dict)
            and str(entry.get("status") or "") == GRID_LEGACY_PAUSE_STATUS
        )
    ]
    invalidated = len(levels) - len(kept)
    if not invalidated:
        return 0
    row["levels"] = kept
    row["legacy_pause_invalidated"] = int(row.get("legacy_pause_invalidated") or 0) + invalidated
    row["legacy_pause_invalidated_at"] = now
    return invalidated


def lifecycle_leg(entry: dict[str, Any]) -> int:
    try:
        return 1 if int(entry.get("grid_leg") or 0) == 1 else 0
    except (TypeError, ValueError):
        return 0


def lifecycle_iteration(entry: dict[str, Any]) -> int:
    try:
        return max(0, int(entry.get("iteration") or 0))
    except (TypeError, ValueError):
        return 0


def initialize_lifecycle_iterations(row: dict[str, Any]) -> bool:
    """Persist iteration=0 for pre-counter grid entries."""
    changed = False
    for entry in row.get("levels") or []:
        if not isinstance(entry, dict) or not entry.get("side"):
            continue
        iteration = lifecycle_iteration(entry)
        if entry.get("iteration") != iteration:
            entry["iteration"] = iteration
            changed = True
    return changed


def lifecycle_active_price_too_close(
    row: dict[str, Any],
    side: str,
    price: Decimal,
    *,
    exclude: dict[str, Any] | None = None,
) -> bool:
    gap = Decimal(str(row["gap_rate"]))
    threshold = price * gap * GRID_ALO_SPACING_MULTIPLIER
    for entry in row.get("levels") or []:
        if not isinstance(entry, dict) or entry is exclude:
            continue
        if str(entry.get("status") or "") not in {"active", "pending"}:
            continue
        if str(entry.get("side") or "") != side:
            continue
        existing = decimal_or_none(entry.get("price", entry.get("limit_px")))
        if existing is not None and existing > 0 and abs(existing - price) <= threshold:
            return True
    return False


def lifecycle_was_confirmed_resting(entry: dict[str, Any]) -> bool:
    """A fully filled limit order that previously rested filled at its saved limit."""
    submit_status = entry.get("last_submit_status")
    resting = submit_status.get("resting") if isinstance(submit_status, dict) else None
    if not isinstance(resting, dict):
        return False
    resting_oid = resting.get("oid")
    entry_oid = entry.get("oid")
    if resting_oid is None or entry_oid is None:
        return True
    try:
        return int(resting_oid) == int(entry_oid)
    except (TypeError, ValueError):
        return False


def lifecycle_fill_price_size(entry: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    fill = entry.get("fill")
    if isinstance(fill, dict):
        price = decimal_or_none(fill.get("avgPx", fill.get("px")))
        size = decimal_or_none(fill.get("totalSz", fill.get("sz")))
    else:
        price = None
        size = None
    price = price or decimal_or_none(entry.get("filled_avg_px"))
    # Once an order is confirmed resting, a later full fill executes at that
    # resting limit.  This fallback is deliberately unavailable to an IOC or
    # a GTC that crossed immediately, whose avgPx must come from fill details.
    if price is None and lifecycle_was_confirmed_resting(entry):
        price = decimal_or_none(entry.get("price", entry.get("limit_px")))
    size = size or decimal_or_none(entry.get("filled_size")) or decimal_or_none(entry.get("size"))
    return price, size


def lifecycle_replacement_from_fill(
    row: dict[str, Any],
    coin: str,
    asset: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any] | None:
    fill_price, fill_size = lifecycle_fill_price_size(source)
    gap = Decimal(str(row["gap_rate"]))
    if fill_price is None or fill_price <= 0 or fill_size is None or fill_size <= 0 or gap <= 0:
        return None
    is_buy = not bool(source.get("is_buy"))
    multiplier = Decimal("1") - gap if is_buy else Decimal("1") + gap
    price = rounded_perp_price(
        fill_price * multiplier,
        int(row.get("sz_decimals") or asset["szDecimals"]),
    )
    if price <= 0:
        return None
    order = grid_order_entry(
        row,
        coin,
        asset,
        is_buy,
        price,
        False,
        size=fill_size,
        gap=gap,
        preserve_size=True,
    )
    order["replacement_order"] = True
    order["grid_leg"] = 1 - lifecycle_leg(source)
    order["source_grid_leg"] = lifecycle_leg(source)
    order["source_oid"] = source.get("oid")
    order["grid_id"] = row.get("id")
    order["economic_chain_id"] = economic_chain_id(row, source)
    order["iteration"] = lifecycle_iteration(source)
    order["replacement_anchor_price"] = decimal_to_plain(fill_price)
    order["replacement_anchor_source"] = "market_fill" if isinstance(source.get("fill"), dict) else "saved_fill"
    order["preserve_fill_size"] = True
    original_notional = fill_size * price
    if original_notional < MIN_NOTIONAL:
        expanded_size = grid_size_for_min_notional(
            fill_size,
            price,
            int(row.get("sz_decimals") or asset["szDecimals"]),
            MIN_NOTIONAL,
        )
        if expanded_size <= 0:
            return None
        order["size"] = decimal_to_plain(expanded_size)
        plan = order.get("plan")
        if isinstance(plan, dict):
            plan["size"] = expanded_size
            expanded_notional = expanded_size * price
            plan["notional"] = expanded_notional
            plan["target_notional"] = expanded_notional
            plan["worst_notional"] = expanded_notional
        order["replacement_expanded_min_notional_from"] = decimal_to_plain(fill_size)
        order["replacement_expanded_min_notional_at"] = decimal_to_plain(original_notional)
        order["replacement_min_notional"] = decimal_to_plain(MIN_NOTIONAL)
    return order


def lifecycle_mark_deferred_or_discarded(
    order: dict[str, Any],
    now: int,
    error_text: str,
) -> str:
    """Preserve unfinished legs and P9 restores; only margin failures use margin status."""
    order["oid"] = None
    order["last_error"] = error_text
    if lifecycle_leg(order) == 1 or bool(order.get("p9_restore")) or bool(order.get("p10_restore")):
        if is_insufficient_margin_text(error_text):
            order["status"] = GRID_MARGIN_STATUS
            order["margin_at"] = now
            order.pop("chain_debt_at", None)
            return GRID_MARGIN_STATUS
        order["status"] = GRID_CHAIN_DEBT_STATUS
        order["chain_debt_at"] = now
        order.pop("margin_at", None)
        return GRID_CHAIN_DEBT_STATUS
    order["status"] = "discarded"
    order["discarded_at"] = now
    return "discarded"


def lifecycle_submit_order(
    exchange: Any,
    coin: str,
    order: dict[str, Any],
    now: int,
    row: dict[str, Any],
    asset: dict[str, Any],
    position_size: Decimal,
    current_mid: Decimal,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
    isolated_leverage_ready: set[str],
    open_orders: list[dict[str, Any]] | None,
    cache: dict[str, Any],
    *,
    search_outward: bool,
    force_gtc: bool = False,
    force_non_reduce_only: bool = False,
    allow_non_reduce_only_fallback: bool = False,
) -> str:
    """Submit one finite-chain order; chain debt defers, completed legs may end."""
    order["grid_id"] = row.get("id")
    economic_chain_id(row, order)
    order["audit_phase"] = cache.get("grid_action_phase")
    order["audit_deficit"] = raw_action_limit_deficit(cache)

    def defer(error_text: str) -> str:
        if lifecycle_leg(order) == 1 and is_min_order_value_error_text(error_text):
            size = decimal_or_none(order.get("size")) or Decimal("0")
            price = decimal_or_none(order.get("price", order.get("limit_px"))) or Decimal("0")
            if size > 0 and price > 0:
                order["oid"] = None
                order["status"] = GRID_P8_REACCUMULATE_STATUS
                order["last_error"] = error_text
                order["p8_reaccumulate_at"] = now
                order["p8_reaccumulate_price"] = decimal_to_plain(price)
                order["p8_reaccumulate_value"] = decimal_to_plain(size * price)
                order["p8_reaccumulate_reason"] = "minimum_order_value_rejected"
                order.pop("p3_queue_seq", None)
                return GRID_P8_REACCUMULATE_STATUS
        result = lifecycle_mark_deferred_or_discarded(order, now, error_text)
        lifecycle_assign_p3_queue_seq(order, cache)
        return result

    order.pop("replace_never_cancel", None)
    order["iteration"] = lifecycle_iteration(order)
    reduce_only = False if force_non_reduce_only else grid_order_reduces_position(order, position_size)
    set_grid_order_reduce_only(order, reduce_only)
    set_grid_order_tif(order, "Gtc" if force_gtc else "Alo")
    plan = order.get("plan")
    if not isinstance(plan, dict):
        return defer("grid order is missing its saved plan")
    if position_size == 0 and not reduce_only and asset_requires_isolated_margin(asset) and coin not in isolated_leverage_ready:
        try:
            reserve_grid_exchange_actions(cache)
            leverage, leverage_result = update_isolated_opening_leverage(exchange, int(asset["maxLeverage"]), coin)
        except Exception as exc:
            return defer(str(exc))
        if leverage_result.get("status") != "ok":
            error_text = f"Failed to set isolated opening leverage to {leverage}x: {leverage_result}"
            return defer(error_text)
        isolated_leverage_ready.add(coin)
    if not bool(order.get("preserve_fill_size")):
        ensure_grid_order_min_notional(row, asset, order)
    if adopt_matching_open_grid_order(open_orders, coin, order, now, row):
        return "submitted"

    def retry_without_reduce_only(error_text: str) -> str | None:
        if not (
            allow_non_reduce_only_fallback
            and reduce_only
            and is_reduce_only_would_increase_text(error_text)
        ):
            return None
        order["p3_non_reduce_only_fallback_at"] = now
        order["p3_non_reduce_only_fallback_error"] = error_text
        return lifecycle_submit_order(
            exchange,
            coin,
            order,
            now,
            row,
            asset,
            position_size,
            current_mid,
            best_bid,
            best_ask,
            isolated_leverage_ready,
            open_orders,
            cache,
            search_outward=search_outward,
            force_gtc=force_gtc,
            force_non_reduce_only=True,
            allow_non_reduce_only_fallback=False,
        )

    for attempt in range(GRID_ALO_PRICE_ATTEMPT_LIMIT):
        price = decimal_or_none(order.get("price", order.get("limit_px")))
        side = str(order.get("side") or "")
        if price is None or price <= 0 or side not in {"buy", "sell"}:
            return lifecycle_mark_deferred_or_discarded(order, now, "invalid lifecycle order price")
        if search_outward:
            while (
                grid_price_would_cross_market(side, price, current_mid, best_bid, best_ask)
                or lifecycle_active_price_too_close(row, side, price, exclude=order)
            ):
                next_price = next_outward_grid_price(row, asset, order)
                if next_price is None or next_price <= 0 or next_price == price:
                    return defer("unable to move lifecycle order outward")
                set_grid_order_price(order, next_price)
                price = next_price
        if str(order.get("birth_source") or "") == "p8_partial_debt":
            size = decimal_or_none(order.get("size")) or Decimal("0")
            if size <= 0 or size * price <= MIN_NOTIONAL:
                order["status"] = GRID_P8_REACCUMULATE_STATUS
                order["p8_reaccumulate_at"] = now
                order["p8_reaccumulate_price"] = decimal_to_plain(price)
                order["p8_reaccumulate_value"] = decimal_to_plain(size * price)
                return GRID_P8_REACCUMULATE_STATUS
        try:
            oid, state, submit_status = submit_grid_child_order(
                exchange,
                coin,
                order,
                cache,
                increment_iteration=not force_gtc,
            )
        except GridPostOnlyRejected as exc:
            if force_gtc or not search_outward:
                return defer(str(exc))
            next_price = next_outward_grid_price(row, asset, order)
            if next_price is None or next_price <= 0 or next_price == price:
                return defer(str(exc))
            set_grid_order_price(order, next_price)
            order["alo_rejects"] = int(order.get("alo_rejects") or 0) + 1
            continue
        except GridActionBudgetUnavailable as exc:
            return defer(str(exc))
        except RuntimeError as exc:
            fallback_result = retry_without_reduce_only(str(exc))
            if fallback_result is not None:
                return fallback_result
            return defer(str(exc))
        except Exception as exc:
            fallback_result = retry_without_reduce_only(str(exc))
            if fallback_result is not None:
                return fallback_result
            return defer(str(exc))
        order["oid"] = oid
        order["status"] = state
        order["submitted_at"] = now
        order["last_submit_status"] = submit_status
        if state == "active":
            record_submitted_open_grid_order(open_orders, coin, order, oid, now)
        elif state == "filled":
            order["filled_at"] = now
            order["replacement_pending"] = True
            filled = submit_status.get("filled") if isinstance(submit_status, dict) else None
            if isinstance(filled, dict):
                order["fill"] = {
                    "oid": oid,
                    "px": filled.get("avgPx", order.get("price")),
                    "sz": filled.get("totalSz", filled.get("sz", order.get("size"))),
                    "time": now * 1000,
                }
        return "submitted"
    return defer("ALO outward search exhausted")


def lifecycle_resolve_perp_asset(
    row: dict[str, Any],
    cache: dict[str, Any],
    info: Any,
    network: str,
    dex: str,
) -> tuple[str, dict[str, Any]]:
    """Resolve immutable market metadata once per Grid for the worker run."""
    raw_coin = batch_row_raw_coin(row)
    key = (network, dex, raw_coin)
    assets = cache.setdefault("perp_assets", {})
    if key not in assets:
        assets[key] = resolve_perp_asset(info, raw_coin)
    return assets[key]


def lifecycle_context(row: dict[str, Any], cache: dict[str, Any]) -> dict[str, Any]:
    phase = str(cache.get("grid_action_phase") or GRID_LIFECYCLE_PHASE_P0)
    network = str(row.get("network") or "mainnet")
    timeout = float(row.get("timeout") or 20)
    raw_coin = batch_row_raw_coin(row)
    dex = str(row.get("dex") or "")
    client_key = (network, timeout, dex)
    clients = cache.setdefault("clients", {})
    if client_key not in clients:
        clients[client_key] = build_worker_clients(cache, network, timeout, raw_coin)
    info, exchange, account, _signer, _role = clients[client_key]
    coin, asset = lifecycle_resolve_perp_asset(row, cache, info, network, dex)
    now = int(cache.setdefault("now", int(time.time())))
    precheck_action_limit(info, account, cache, network, now)
    needs_mid = phase in {
        GRID_LIFECYCLE_PHASE_P0,
        GRID_LIFECYCLE_PHASE_P2,
        GRID_LIFECYCLE_PHASE_P3,
        GRID_LIFECYCLE_PHASE_P4,
        GRID_LIFECYCLE_PHASE_P6,
        GRID_LIFECYCLE_PHASE_P10,
    }
    # P0/P4/P10 submit IOC actions from the fresh mid, then fixed-price GTC
    # children without outward ALO search. P2 needs the book only after it has
    # confirmed a fill and is actually about to place a replacement. P3 is the
    # only phase that always needs a book up front.
    needs_book = phase == GRID_LIFECYCLE_PHASE_P3
    needs_position = phase in {
        GRID_LIFECYCLE_PHASE_P0,
        GRID_LIFECYCLE_PHASE_P2,
        GRID_LIFECYCLE_PHASE_P3,
        GRID_LIFECYCLE_PHASE_P4,
        GRID_LIFECYCLE_PHASE_P10,
    }
    needs_withdrawable = phase not in {
        GRID_LIFECYCLE_PHASE_P4,
        GRID_LIFECYCLE_PHASE_P5,
    }
    needs_open_orders = phase in {
        GRID_LIFECYCLE_PHASE_P0,
        GRID_LIFECYCLE_PHASE_P2,
        GRID_LIFECYCLE_PHASE_P3,
        GRID_LIFECYCLE_PHASE_P4,
        GRID_LIFECYCLE_PHASE_P5,
        GRID_LIFECYCLE_PHASE_P7,
        GRID_LIFECYCLE_PHASE_P9,
        GRID_LIFECYCLE_PHASE_P10,
    }
    needs_fills = phase in {
        GRID_LIFECYCLE_PHASE_P0,
        GRID_LIFECYCLE_PHASE_P2,
        GRID_LIFECYCLE_PHASE_P5,
        GRID_LIFECYCLE_PHASE_P10,
    }

    mids: dict[str, Any] = {}
    current_mid: Decimal | None = None
    if needs_mid:
        mids_key = (network, dex)
        mids_cache = cache.setdefault("mids", {})
        if mids_key not in mids_cache:
            mids_cache[mids_key] = info.all_mids(dex)
        mids = mids_cache[mids_key]
        current_mid = Decimal(str(mids[coin]))

    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    if needs_book:
        books_key = (network, coin)
        books = cache.setdefault("books", {})
        if books_key not in books:
            books[books_key] = best_bid_ask(info, coin)
        best_bid, best_ask = books[books_key]

    position: dict[str, Any] | None = None
    position_size = Decimal("0")
    position_value = Decimal("0")
    liquidation_px: Decimal | None = None
    if needs_position:
        state_key = (network, account, dex)
        states = cache.setdefault("user_states", {})
        if state_key not in states:
            states[state_key] = info.user_state(account, dex=dex)
        position = find_current_position_from_state(states[state_key], coin)
        if position is not None:
            position_size = Decimal(str(position.get("szi") or "0"))
            position_value = decimal_or_none(position.get("positionValue")) or abs(
                position_size * (current_mid or Decimal("0"))
            )
            position_value = abs(position_value)
            liquidation_px = decimal_or_none(position.get("liquidationPx"))

    withdrawable: Decimal | None = None
    if needs_withdrawable:
        withdrawable_state = account_spot_withdrawable(info, account, network, cache)
        withdrawable = withdrawable_state[0] if withdrawable_state is not None else None

    open_orders: list[dict[str, Any]] = []
    open_oids: set[int] = set()
    if needs_open_orders:
        open_key = (network, account, dex)
        open_cache = cache.setdefault("open_orders", {})
        if open_key not in open_cache:
            open_cache[open_key] = collect_frontend_open_orders(info, account, dex)
        open_orders = open_cache[open_key]
        open_oids = open_order_oids(info, account, dex, coin, open_orders)

    now_ms = now * 1000
    common_start_ms = (now - GRID_FILL_LOOKBACK_SECONDS) * 1000
    fills_by_oid: dict[int, dict[str, Any]] = {}
    if needs_fills:
        fills_key = (network, account, common_start_ms, now_ms)
        fills_cache = cache.setdefault("fills", {})
        if fills_key not in fills_cache:
            fills_cache[fills_key] = info.user_fills_by_time(account, common_start_ms, now_ms)
        fills_by_oid = recent_fills_by_oid(
            info, account, coin, common_start_ms, now_ms, fills_cache[fills_key]
        )
    position_leverage = decimal_or_none(
        (position.get("leverage") or {}).get("value")
    ) if isinstance(position, dict) else None
    max_leverage = decimal_or_none(asset.get("maxLeverage"))
    lifecycle_leverage = (
        position_leverage
        if position_leverage is not None and position_leverage > 0
        else max_leverage
    )
    if needs_withdrawable:
        row["account_usdc_withdrawable"] = (
            decimal_to_plain(withdrawable) if withdrawable is not None else None
        )
    if needs_position:
        row["position_size"] = decimal_to_plain(position_size)
        row["position_value"] = decimal_to_plain(position_value)
    row["lifecycle_max_leverage"] = decimal_to_plain(asset.get("maxLeverage"))
    row["lifecycle_leverage"] = (
        decimal_to_plain(lifecycle_leverage) if lifecycle_leverage is not None else None
    )
    row["raw_deficit"] = raw_action_limit_deficit(cache)
    return {
        "network": network,
        "dex": dex,
        "info": info,
        "exchange": exchange,
        "account": account,
        "coin": coin,
        "asset": asset,
        "now": now,
        "now_ms": now_ms,
        "mids": mids,
        "current_mid": current_mid,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "position_size": position_size,
        "position_value": position_value,
        "position_leverage": position_leverage,
        "liquidation_px": liquidation_px,
        "withdrawable": withdrawable,
        "open_orders": open_orders,
        "open_oids": open_oids,
        "fills_by_oid": fills_by_oid,
        "p10_chain_realized_surplus": cache.get("p10_chain_realized_surplus"),
    }


def lifecycle_ensure_context_book(ctx: dict[str, Any], cache: dict[str, Any]) -> None:
    """Load one fresh book only when an action needs outward price protection."""
    if ctx.get("best_bid") is not None or ctx.get("best_ask") is not None:
        return
    network = str(ctx.get("network") or "mainnet")
    coin = str(ctx.get("coin") or "")
    if not coin:
        return
    books_key = (network, coin)
    books = cache.setdefault("books", {})
    if books_key not in books:
        books[books_key] = best_bid_ask(ctx["info"], coin)
    ctx["best_bid"], ctx["best_ask"] = books[books_key]


def lifecycle_row_account_key(row: dict[str, Any], network: str, account: str) -> tuple[str, str]:
    return network, str(row.get("account") or account).lower()


def lifecycle_terminal_candidate(
    rows: list[dict[str, Any]],
    network: str,
    account: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    account_key = account.lower()
    candidates: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
    for candidate_row in rows:
        if not isinstance(candidate_row, dict) or candidate_row.get("type") != "grid":
            continue
        if str(candidate_row.get("network") or "mainnet") != network:
            continue
        if str(candidate_row.get("account") or account).lower() != account_key:
            continue
        for entry in candidate_row.get("levels") or []:
            if not isinstance(entry, dict) or str(entry.get("status") or "") != "active":
                continue
            if lifecycle_leg(entry) != 0 or entry.get("oid") is None or bool(entry.get("reduce_only")):
                continue
            timestamp = grid_entry_timestamp_ms(entry) or 0
            try:
                oid = int(entry.get("oid") or 0)
            except (TypeError, ValueError):
                oid = 0
            candidates.append((timestamp, oid, candidate_row, entry))
    if not candidates:
        return None
    _timestamp, _oid, candidate_row, entry = min(candidates, key=lambda item: item[:2])
    return candidate_row, entry


def lifecycle_cancel_terminal_entry(
    exchange: Any,
    coin: str,
    row: dict[str, Any],
    entry: dict[str, Any],
    now: int,
    cache: dict[str, Any],
) -> bool:
    try:
        oid = int(entry["oid"])
    except (KeyError, TypeError, ValueError):
        return False
    reserve_grid_exchange_actions(cache)
    request = {"coin": coin, "oid": oid}
    result = exchange.bulk_cancel([request])
    audit_grid_action("grid_leg_terminal_cancel", coin=coin, oid=oid, grid_leg=0, result=result)
    if not isinstance(result, dict) or result.get("status") != "ok":
        entry["last_error"] = str(result)
        entry["terminal_cancel_attempted_at"] = now
        return False
    cancelled, errors = successful_cancel_oids(result, [request])
    if oid not in cancelled:
        entry["last_error"] = "; ".join(errors) or str(result)
        entry["terminal_cancel_attempted_at"] = now
        return False
    levels = row.get("levels")
    if isinstance(levels, list) and entry in levels:
        levels.remove(entry)
    return True


def lifecycle_p9_order_score(
    row: dict[str, Any],
    entry: dict[str, Any],
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Return margin-times-deviation score, margin and deviation for one order."""
    mid = decimal_or_none(row.get("lifecycle_mid"))
    price = decimal_or_none(entry.get("price", entry.get("limit_px")))
    size = decimal_or_none(entry.get("size"))
    leverage = (
        decimal_or_none(row.get("lifecycle_leverage"))
        or decimal_or_none(row.get("lifecycle_max_leverage"))
    )
    if (
        mid is None
        or mid <= 0
        or price is None
        or price <= 0
        or size is None
        or size <= 0
        or leverage is None
        or leverage <= 0
    ):
        return None
    estimated_margin = price * size / leverage
    deviation_rate = abs(mid - price) / mid
    return estimated_margin * deviation_rate, estimated_margin, deviation_rate


def lifecycle_p9_candidate(
    rows: list[dict[str, Any]],
    network: str,
    account: str,
) -> tuple[dict[str, Any], dict[str, Any], Decimal, Decimal, Decimal] | None:
    """Choose the account-wide active non-reduce-only order with the highest P9 score."""
    account_key = account.lower()
    candidates: list[
        tuple[Decimal, Decimal, Decimal, int, dict[str, Any], dict[str, Any]]
    ] = []
    for candidate_row in rows:
        if not isinstance(candidate_row, dict) or not grid_row_recoverable_from_error(candidate_row):
            continue
        if str(candidate_row.get("network") or "mainnet") != network:
            continue
        if str(candidate_row.get("account") or account).lower() != account_key:
            continue
        for entry in candidate_row.get("levels") or []:
            if (
                not isinstance(entry, dict)
                or str(entry.get("status") or "") != "active"
                or bool(entry.get("reduce_only"))
            ):
                continue
            try:
                oid = int(entry["oid"])
            except (KeyError, TypeError, ValueError):
                continue
            score_parts = lifecycle_p9_order_score(candidate_row, entry)
            if score_parts is None:
                continue
            score, estimated_margin, deviation_rate = score_parts
            candidates.append(
                (score, estimated_margin, deviation_rate, -oid, candidate_row, entry)
            )
    if not candidates:
        return None
    score, estimated_margin, deviation_rate, _oid_order, candidate_row, entry = max(
        candidates, key=lambda item: item[:4]
    )
    return candidate_row, entry, score, estimated_margin, deviation_rate


def lifecycle_cancel_p9_candidate(
    exchange: Any,
    coin: str,
    row: dict[str, Any],
    entry: dict[str, Any],
    score: Decimal,
    estimated_margin: Decimal,
    deviation_rate: Decimal,
    now: int,
    cache: dict[str, Any],
) -> bool:
    """Cancel one confirmed P9 candidate and append it to the P3 FIFO tail."""
    try:
        oid = int(entry["oid"])
    except (KeyError, TypeError, ValueError):
        return False
    request = {"coin": coin, "oid": oid}
    try:
        reserve_grid_exchange_actions(cache)
        result = exchange.bulk_cancel([request])
    except Exception as exc:
        entry["last_error"] = str(exc)
        entry["p9_cancel_attempted_at"] = now
        return False
    audit_grid_action(
        "grid_p9_withdrawable_cancel",
        coin=coin,
        oid=oid,
        grid_id=row.get("id"),
        economic_chain_id=economic_chain_id(row, entry),
        grid_leg=lifecycle_leg(entry),
        score=decimal_to_plain(score),
        estimated_margin=decimal_to_plain(estimated_margin),
        deviation_rate=decimal_to_plain(deviation_rate),
        result=result,
    )
    if not isinstance(result, dict) or result.get("status") != "ok":
        entry["last_error"] = str(result)
        entry["p9_cancel_attempted_at"] = now
        return False
    cancelled, errors = successful_cancel_oids(result, [request])
    if oid not in cancelled:
        entry["last_error"] = "; ".join(errors) or str(result)
        entry["p9_cancel_attempted_at"] = now
        return False
    entry["p9_source_oid"] = oid
    entry["p9_cancelled_at"] = now
    entry["p9_score"] = decimal_to_plain(score)
    entry["p9_estimated_margin"] = decimal_to_plain(estimated_margin)
    entry["p9_deviation_rate"] = decimal_to_plain(deviation_rate)
    entry["p9_restore"] = True
    entry["oid"] = None
    entry["status"] = GRID_CHAIN_DEBT_STATUS
    entry["chain_debt_at"] = now
    entry.pop("submitted_at", None)
    entry.pop("p3_queue_seq", None)
    lifecycle_assign_p3_queue_seq(entry, cache)
    levels = row.get("levels")
    if isinstance(levels, list) and entry in levels:
        levels.remove(entry)
        levels.append(entry)
    return True


def build_grid_p10_market_order(
    exchange: Any,
    row: dict[str, Any],
    coin: str,
    asset: dict[str, Any],
    current_mid: Decimal,
    is_buy: bool,
    size: Decimal,
) -> dict[str, Any] | None:
    """Build an exact-size IOC used to promote one existing active Grid order."""
    if current_mid <= 0 or size <= 0:
        return None
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    step = Decimal(1).scaleb(-sz_decimals)
    size = (size / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if size <= 0:
        return None
    slippage = Decimal(str(row.get("slippage") or DEFAULT_SLIPPAGE))
    limit_px = Decimal(
        str(exchange._slippage_price(coin, is_buy, float(slippage), float(current_mid)))
    )
    limit_px = rounded_perp_price(limit_px, sz_decimals)
    min_notional = max(MIN_NOTIONAL, decimal_or_none(row.get("min_order_value")) or MIN_NOTIONAL)
    if limit_px <= 0 or size * limit_px < min_notional:
        return None
    notional = size * limit_px
    return {
        "side": "buy" if is_buy else "sell",
        "is_buy": is_buy,
        "size": decimal_to_plain(size),
        "price": decimal_to_plain(limit_px),
        "limit_px": decimal_to_plain(limit_px),
        "reduce_only": False,
        "p10_market_order": True,
        "plan": {
            "label": "grid-p10-promotion",
            "coin": coin,
            "is_buy": is_buy,
            "size": size,
            "limit_px": limit_px,
            "order_type": {"limit": {"tif": "Ioc"}},
            "reduce_only": False,
            "mode": "market",
            "notional": notional,
            "target_notional": notional,
            "worst_notional": notional,
            "reference_price": current_mid,
            "reference_notional": size * current_mid,
            "price_source": f"mid with {slippage} slippage protection",
        },
    }


def lifecycle_p10_candidate(
    row: dict[str, Any],
    ctx: dict[str, Any],
) -> tuple[
    dict[str, Any], Decimal, Decimal, bool, Decimal, Decimal, Decimal, Decimal
] | None:
    """Choose the nearest active add-risk order in the current limit-breach direction."""
    is_buy = grid_limit_chase_direction(row, ctx["position_size"], ctx["position_value"])
    if is_buy is None:
        return None
    side = "buy" if is_buy else "sell"
    open_by_oid: dict[int, dict[str, Any]] = {}
    for open_order in ctx.get("open_orders") or []:
        if not isinstance(open_order, dict) or not fill_matches_coin(
            str(open_order.get("coin", "")), ctx["coin"]
        ):
            continue
        try:
            open_by_oid[int(open_order["oid"])] = open_order
        except (KeyError, TypeError, ValueError):
            continue
    candidates: list[
        tuple[
            Decimal, Decimal, int, dict[str, Any], Decimal,
            Decimal, Decimal, Decimal, Decimal,
        ]
    ] = []
    profit_blocked: list[
        tuple[Decimal, Decimal, int, dict[str, Any], Decimal, Decimal, Decimal, Decimal]
    ] = []
    surplus_map = ctx.get("p10_chain_realized_surplus")
    if not isinstance(surplus_map, dict):
        row["p10_status"] = "skipped_chain_profit_unavailable"
        return None
    gap_rate = decimal_or_none(row.get("gap_rate")) or Decimal("0")
    for entry in row.get("levels") or []:
        if (
            not isinstance(entry, dict)
            or str(entry.get("status") or "") != "active"
            or str(entry.get("side") or "") != side
            or bool(entry.get("reduce_only"))
            or grid_order_reduces_position(entry, ctx["position_size"])
        ):
            continue
        try:
            oid = int(entry["oid"])
        except (KeyError, TypeError, ValueError):
            continue
        open_order = open_by_oid.get(oid)
        if open_order is None:
            continue
        price = decimal_or_none(open_order.get("limitPx")) or decimal_or_none(
            entry.get("price", entry.get("limit_px"))
        )
        remaining_size = decimal_or_none(open_order.get("sz")) or decimal_or_none(entry.get("size"))
        if price is None or price <= 0 or remaining_size is None or remaining_size <= 0:
            continue
        # The promoted order must still be on the passive side of the market;
        # otherwise the mirrored target could land on the wrong side of the
        # eventual IOC fill and no longer represent a profitable closure.
        if (is_buy and price >= ctx["current_mid"]) or (
            not is_buy and price <= ctx["current_mid"]
        ):
            continue
        distance = abs(ctx["current_mid"] - price)
        chain_id = str(entry.get("economic_chain_id") or "").strip()
        realized_surplus = (
            decimal_or_none(surplus_map.get(chain_id)) or Decimal("0")
            if current_economic_chain_id(chain_id)
            and not entry.get("economic_chain_id_backfilled_at")
            else Decimal("0")
        )
        chase_cost = distance * remaining_size
        execution_buffer = ctx["current_mid"] * remaining_size * gap_rate
        required_surplus = chase_cost + execution_buffer
        candidate = (
            distance, price, oid, entry, remaining_size, realized_surplus,
            required_surplus, chase_cost, execution_buffer,
        )
        if realized_surplus <= required_surplus:
            profit_blocked.append(candidate[:-1])
            continue
        candidates.append(candidate)
    if not candidates:
        if profit_blocked:
            (
                distance, source_price, source_oid, source, source_size,
                realized_surplus, required_surplus, chase_cost,
            ) = min(profit_blocked, key=lambda item: (item[0], item[2]))
            execution_buffer = required_surplus - chase_cost
            row["p10_status"] = "skipped_chain_profit"
            row["p10_chain_realized_surplus"] = decimal_to_plain(realized_surplus)
            row["p10_required_surplus"] = decimal_to_plain(required_surplus)
            audit_grid_action(
                "grid_p10_chain_profit_skipped",
                coin=ctx["coin"],
                grid_id=row.get("id"),
                economic_chain_id=source.get("economic_chain_id"),
                source_oid=source_oid,
                source_price=decimal_to_plain(source_price),
                source_size=decimal_to_plain(source_size),
                current_mid=decimal_to_plain(ctx["current_mid"]),
                source_distance=decimal_to_plain(distance),
                chain_realized_surplus=decimal_to_plain(realized_surplus),
                chase_cost=decimal_to_plain(chase_cost),
                execution_buffer=decimal_to_plain(execution_buffer),
                required_surplus=decimal_to_plain(required_surplus),
            )
        return None
    (
        _distance, source_price, _oid, entry, remaining_size,
        realized_surplus, required_surplus, chase_cost, execution_buffer,
    ) = min(
        candidates, key=lambda item: (item[0], item[2])
    )
    return (
        entry, remaining_size, source_price, is_buy, realized_surplus,
        required_surplus, chase_cost, execution_buffer,
    )


def lifecycle_p10_margin_adequacy(
    market: dict[str, Any],
    withdrawable: Decimal | None,
    leverage: Decimal | None,
) -> tuple[Decimal | None, Decimal | None]:
    """Estimate P10 IOC margin and its withdrawable coverage before cancellation."""
    plan = market.get("plan")
    worst_notional = (
        decimal_or_none(plan.get("worst_notional")) if isinstance(plan, dict) else None
    )
    if (
        withdrawable is None
        or withdrawable < 0
        or leverage is None
        or leverage <= 0
        or worst_notional is None
        or worst_notional <= 0
    ):
        return None, None
    estimated_margin = worst_notional / leverage
    if estimated_margin <= 0:
        return None, None
    return estimated_margin, withdrawable / estimated_margin


def lifecycle_p10_source_distance_rate(
    current_mid: Decimal,
    source_price: Decimal,
) -> Decimal | None:
    if current_mid <= 0 or source_price <= 0:
        return None
    return abs(current_mid - source_price) / current_mid


def lifecycle_p10_projected_within_max(
    row: dict[str, Any],
    position_size: Decimal,
    position_value: Decimal,
    market: dict[str, Any],
) -> bool:
    plan = market.get("plan")
    if not isinstance(plan, dict):
        return False
    notional = decimal_or_none(plan.get("worst_notional"))
    if notional is None or notional <= 0:
        return False
    minimum = Decimal(str(row.get("min_position_value") or "0"))
    maximum = Decimal(str(row.get("max_position_value") or "0"))
    lower_bound, upper_bound = grid_position_bounds("limit", minimum, maximum)
    current = signed_position_value(position_size, position_value)
    if bool(plan.get("is_buy")):
        return current + notional <= upper_bound
    return current - notional >= lower_bound


def lifecycle_p10_intent(row: dict[str, Any]) -> dict[str, Any] | None:
    intent = row.get("p10_promotion_intent")
    return intent if isinstance(intent, dict) else None


def lifecycle_p10_source(row: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any] | None:
    cloid = str(intent.get("cloid") or "")
    for entry in row.get("levels") or []:
        if isinstance(entry, dict) and str(entry.get("p10_intent_cloid") or "") == cloid:
            return entry
    return None


def lifecycle_remove_p10_intent(
    row: dict[str, Any],
    source: dict[str, Any] | None,
    cache: dict[str, Any],
) -> None:
    if isinstance(source, dict):
        source.pop("p10_intent_cloid", None)
    row.pop("p10_promotion_intent", None)
    persist_lifecycle_intent(cache)


def lifecycle_restore_p10_source(
    row: dict[str, Any],
    ctx: dict[str, Any],
    cache: dict[str, Any],
    intent: dict[str, Any],
    reason: str,
) -> bool:
    """Restore the cancelled source as P3 debt when P10 definitely did not fill."""
    source = lifecycle_p10_source(row, intent)
    if source is None:
        price = decimal_or_none(intent.get("source_price"))
        size = decimal_or_none(intent.get("source_size"))
        if price is None or price <= 0 or size is None or size <= 0:
            intent["status"] = "restore_blocked"
            intent["last_error"] = reason
            persist_lifecycle_intent(cache)
            return False
        source = grid_order_entry(
            row,
            ctx["coin"],
            ctx["asset"],
            bool(intent.get("market_is_buy")),
            price,
            False,
            size=size,
            gap=Decimal(str(row["gap_rate"])),
            preserve_size=True,
        )
        source["grid_leg"] = int(intent.get("source_grid_leg") or 0)
        source["iteration"] = int(intent.get("source_iteration") or 0)
        source["economic_chain_id"] = intent.get("economic_chain_id")
        row.setdefault("levels", []).append(source)
    source["oid"] = None
    source["status"] = GRID_CHAIN_DEBT_STATUS
    source["chain_debt_at"] = ctx["now"]
    source["p10_restore"] = True
    source["p10_restore_reason"] = reason
    source["p10_source_oid"] = intent.get("source_oid")
    source.pop("submitted_at", None)
    source.pop("p3_queue_seq", None)
    lifecycle_assign_p3_queue_seq(source, cache)
    levels = row.get("levels")
    if isinstance(levels, list) and source in levels:
        levels.remove(source)
        levels.append(source)
    lifecycle_remove_p10_intent(row, source, cache)
    return True


def lifecycle_p10_replacement_from_fill(
    row: dict[str, Any],
    ctx: dict[str, Any],
    intent: dict[str, Any],
    fill_price: Decimal,
    fill_size: Decimal,
) -> dict[str, Any] | None:
    source_price = decimal_or_none(intent.get("source_price"))
    if source_price is None or source_price <= 0 or fill_price <= 0 or fill_size <= 0:
        return None
    market_is_buy = bool(intent.get("market_is_buy"))
    gap_rate = decimal_or_none(row.get("gap_rate")) or Decimal("0")
    sz_decimals = int(row.get("sz_decimals") or ctx["asset"]["szDecimals"])
    reflected_price = fill_price * Decimal("2") - source_price
    target_price = (
        rounded_perp_price(reflected_price, sz_decimals)
        if reflected_price > 0
        else Decimal("0")
    )
    # The IOC can occasionally improve through the old passive price while the
    # cancel/fill race is in flight.  In that case the reflected target points
    # to the wrong side of the fill.  Keep at least one configured gap between
    # the actual fill and its reverse order so fees, slippage, and the normal
    # profit buffer remain covered.
    if gap_rate > 0:
        gap_target = rounded_perp_price(
            fill_price
            * (Decimal("1") + gap_rate if market_is_buy else Decimal("1") - gap_rate),
            sz_decimals,
        )
        target_price = (
            max(target_price, gap_target)
            if market_is_buy
            else min(target_price, gap_target)
        )
    if target_price <= 0 or (
        market_is_buy and target_price <= fill_price
    ) or (
        not market_is_buy and target_price >= fill_price
    ):
        return None
    child = grid_order_entry(
        row,
        ctx["coin"],
        ctx["asset"],
        not market_is_buy,
        target_price,
        True,
        size=fill_size,
        gap=Decimal(str(row["gap_rate"])),
        preserve_size=True,
    )
    child.update(
        {
            "replacement_order": True,
            "p10_replacement": True,
            "p10_restore": True,
            "grid_leg": 1 - int(intent.get("source_grid_leg") or 0),
            "source_grid_leg": int(intent.get("source_grid_leg") or 0),
            "source_oid": intent.get("source_oid"),
            "p10_source_oid": intent.get("source_oid"),
            "p10_market_cloid": intent.get("cloid"),
            "grid_id": row.get("id"),
            "economic_chain_id": intent.get("economic_chain_id"),
            "iteration": int(intent.get("source_iteration") or 0),
            "replacement_anchor_price": decimal_to_plain(fill_price),
            "replacement_anchor_source": "p10_market_fill",
            "p10_original_order_price": decimal_to_plain(source_price),
            "preserve_fill_size": True,
        }
    )
    return child


def lifecycle_materialize_p10_fill(
    row: dict[str, Any],
    ctx: dict[str, Any],
    cache: dict[str, Any],
    intent: dict[str, Any],
    fill_price: Decimal,
    fill_size: Decimal,
    market_oid: int | None,
) -> bool:
    child = lifecycle_p10_replacement_from_fill(row, ctx, intent, fill_price, fill_size)
    if child is None:
        intent["status"] = "filled_waiting_for_replacement"
        intent["market_fill_price"] = decimal_to_plain(fill_price)
        intent["market_fill_size"] = decimal_to_plain(fill_size)
        intent["market_oid"] = market_oid
        intent["last_error"] = "P10 fill could not build its mirrored replacement"
        persist_lifecycle_intent(cache)
        return False
    source = lifecycle_p10_source(row, intent)
    levels = row.setdefault("levels", [])
    if source in levels:
        levels.remove(source)
    child["status"] = GRID_CHAIN_DEBT_STATUS
    child["oid"] = None
    child["chain_debt_at"] = ctx["now"]
    lifecycle_assign_p3_queue_seq(child, cache)
    levels.append(child)
    lifecycle_remove_p10_intent(row, source, cache)

    synthetic_position = ctx["position_size"] + (fill_size if bool(intent.get("market_is_buy")) else -fill_size)
    result = lifecycle_submit_order(
        ctx["exchange"], ctx["coin"], child, ctx["now"], row, ctx["asset"],
        synthetic_position, ctx["current_mid"], ctx["best_bid"], ctx["best_ask"],
        cache.setdefault("lifecycle_isolated_ready", set()), ctx["open_orders"], cache,
        search_outward=False, force_gtc=True,
    )
    if result == GRID_P8_REACCUMULATE_STATUS and lifecycle_accumulate_p8_partial_debt(
        row,
        child,
        decimal_or_none(child.get("size")) or Decimal("0"),
        decimal_or_none(child.get("price", child.get("limit_px"))) or Decimal("0"),
        ctx["now"],
    ):
        if child in levels:
            levels.remove(child)
    row["p10_status"] = "submitted" if result == "submitted" else result
    row["p10_fill_price"] = decimal_to_plain(fill_price)
    row["p10_fill_size"] = decimal_to_plain(fill_size)
    row["p10_replacement_price"] = child.get("price")
    row["p10_at"] = ctx["now"]
    audit_grid_action(
        "grid_p10_materialized",
        coin=ctx["coin"],
        grid_id=row.get("id"),
        economic_chain_id=intent.get("economic_chain_id"),
        source_oid=intent.get("source_oid"),
        market_oid=market_oid,
        market_side="buy" if bool(intent.get("market_is_buy")) else "sell",
        market_fill_price=decimal_to_plain(fill_price),
        market_fill_size=decimal_to_plain(fill_size),
        original_order_price=intent.get("source_price"),
        replacement_price=child.get("price"),
        replacement_oid=child.get("oid"),
        replacement_status=child.get("status"),
    )
    return True


def lifecycle_submit_p10_market(
    row: dict[str, Any],
    ctx: dict[str, Any],
    cache: dict[str, Any],
    intent: dict[str, Any],
    market: dict[str, Any],
) -> bool:
    plan = market.get("plan")
    if not isinstance(plan, dict):
        return False
    intent["status"] = "submitting"
    persist_lifecycle_intent(cache)
    try:
        reserve_grid_exchange_actions(cache)
        result = ctx["exchange"].order(
            ctx["coin"], bool(plan["is_buy"]), float(plan["size"]), float(plan["limit_px"]),
            plan["order_type"], reduce_only=False, cloid=Cloid.from_str(str(intent["cloid"])),
        )
    except Exception as exc:
        intent["status"] = "awaiting_reconcile"
        intent["last_error"] = str(exc)
        intent["last_checked_at"] = ctx["now"]
        persist_lifecycle_intent(cache)
        return True
    audit_grid_action(
        "p10_market_submit",
        coin=ctx["coin"],
        grid_id=row.get("id"),
        economic_chain_id=intent.get("economic_chain_id"),
        source_oid=intent.get("source_oid"),
        side=market.get("side"),
        price=market.get("price"),
        size=market.get("size"),
        reduce_only=False,
        cloid=intent.get("cloid"),
        result=result,
    )
    if not isinstance(result, dict) or result.get("status") != "ok":
        return lifecycle_restore_p10_source(row, ctx, cache, intent, str(result))
    filled: dict[str, Any] | None = None
    for status in result.get("response", {}).get("data", {}).get("statuses", []):
        if isinstance(status, dict) and isinstance(status.get("filled"), dict):
            filled = status["filled"]
            break
        if isinstance(status, dict) and status.get("error"):
            return lifecycle_restore_p10_source(row, ctx, cache, intent, str(status["error"]))
    if filled is None:
        intent["status"] = "awaiting_reconcile"
        intent["last_error"] = str(result)
        persist_lifecycle_intent(cache)
        return True
    fill_price = decimal_or_none(filled.get("avgPx")) or ctx["current_mid"]
    fill_size = decimal_or_none(filled.get("totalSz", filled.get("sz"))) or decimal_or_none(
        market.get("size")
    )
    market_oid = int(filled["oid"]) if filled.get("oid") is not None else None
    return lifecycle_materialize_p10_fill(
        row, ctx, cache, intent, fill_price, fill_size or Decimal("0"), market_oid
    )


def lifecycle_reconcile_p10_intent(
    row: dict[str, Any],
    ctx: dict[str, Any],
    cache: dict[str, Any],
    intent: dict[str, Any],
) -> tuple[bool, bool]:
    """Return (changed, stop); stop means P10 must not start/resume another action."""
    status = str(intent.get("status") or "")
    source = lifecycle_p10_source(row, intent)
    if status in {"prepared", "cancelled"}:
        return False, False
    if status == "filled_waiting_for_replacement":
        fill_price = decimal_or_none(intent.get("market_fill_price"))
        fill_size = decimal_or_none(intent.get("market_fill_size"))
        market_oid = (
            int(intent["market_oid"])
            if intent.get("market_oid") is not None
            else None
        )
        if fill_price is not None and fill_size is not None:
            return lifecycle_materialize_p10_fill(
                row, ctx, cache, intent, fill_price, fill_size, market_oid
            ), True
        # Older persisted intents did not retain the fill details.  Fall
        # through to the CLOID/fill reconciliation below so deployment can
        # repair them without a manual state edit.
    try:
        order_status = ctx["info"].query_order_by_cloid(
            ctx["account"], Cloid.from_str(str(intent["cloid"]))
        )
    except Exception as exc:
        intent["last_error"] = str(exc)
        intent["last_checked_at"] = ctx["now"]
        persist_lifecycle_intent(cache)
        return True, True
    status_name = grid_order_status_name(order_status).strip()
    intent["exchange_status"] = status_name
    intent["last_checked_at"] = ctx["now"]
    order_wrapper = order_status.get("order") if isinstance(order_status, dict) else None
    order_data = order_wrapper.get("order") if isinstance(order_wrapper, dict) else None
    market_oid = None
    if isinstance(order_data, dict) and order_data.get("oid") is not None:
        market_oid = int(order_data["oid"])
    if status_name == "filled" and market_oid is not None:
        fill = ctx["fills_by_oid"].get(market_oid)
        fill_price = decimal_or_none(fill.get("px")) if isinstance(fill, dict) else None
        fill_size = decimal_or_none(fill.get("sz")) if isinstance(fill, dict) else None
        if fill_price is not None and fill_size is not None:
            return lifecycle_materialize_p10_fill(
                row, ctx, cache, intent, fill_price, fill_size, market_oid
            ), True
        intent["status"] = "awaiting_reconcile"
        intent["last_error"] = "P10 fill details are not available yet"
        persist_lifecycle_intent(cache)
        return True, True
    lowered = status_name.lower()
    if grid_order_status_is_cancelled(order_status) or lowered in {"rejected", "margincanceled"}:
        return lifecycle_restore_p10_source(row, ctx, cache, intent, status_name), True
    if (
        lowered == "unknownoid"
        and ctx["now"] - int(intent.get("created_at") or 0) >= GRID_BIRTH_INTENT_UNKNOWN_GRACE_SECONDS
    ):
        return lifecycle_restore_p10_source(row, ctx, cache, intent, status_name), True
    persist_lifecycle_intent(cache)
    return True, True


def lifecycle_process_p10(row: dict[str, Any], ctx: dict[str, Any], cache: dict[str, Any]) -> bool:
    """Promote one existing add-risk order after all pre-cancel safety checks pass."""
    intent = lifecycle_p10_intent(row)
    if intent is not None:
        changed, stop = lifecycle_reconcile_p10_intent(row, ctx, cache, intent)
        if stop:
            return changed
        source = lifecycle_p10_source(row, intent)
        if source is None:
            return lifecycle_restore_p10_source(
                row, ctx, cache, intent, "P10 source order is missing"
            )
        source_size = decimal_or_none(intent.get("source_size")) or Decimal("0")
        is_buy = bool(intent.get("market_is_buy"))
        source_price = decimal_or_none(intent.get("source_price")) or Decimal("0")
    else:
        candidate = lifecycle_p10_candidate(row, ctx)
        if candidate is None:
            return False
        (
            source,
            source_size,
            source_price,
            is_buy,
            chain_realized_surplus,
            required_surplus,
            chase_cost,
            execution_buffer,
        ) = candidate
        row["p10_chain_realized_surplus"] = decimal_to_plain(chain_realized_surplus)
        row["p10_required_surplus"] = decimal_to_plain(required_surplus)
        row["p10_estimated_chase_cost"] = decimal_to_plain(chase_cost)
        row["p10_execution_buffer"] = decimal_to_plain(execution_buffer)
        distance_rate = lifecycle_p10_source_distance_rate(
            ctx["current_mid"], source_price
        )
        gap_rate = decimal_or_none(row.get("gap_rate")) or Decimal("0")
        row["p10_source_distance_rate"] = (
            decimal_to_plain(distance_rate) if distance_rate is not None else None
        )
        row["p10_gap_rate"] = decimal_to_plain(gap_rate)
        if distance_rate is not None and gap_rate > 0 and distance_rate < gap_rate:
            row["p10_status"] = "skipped_source_within_gap"
            audit_grid_action(
                "grid_p10_source_within_gap_skipped",
                coin=ctx["coin"],
                grid_id=row.get("id"),
                source_oid=source.get("oid"),
                source_price=decimal_to_plain(source_price),
                current_mid=decimal_to_plain(ctx["current_mid"]),
                source_distance_rate=decimal_to_plain(distance_rate),
                gap_rate=decimal_to_plain(gap_rate),
            )
            return False

    market = build_grid_p10_market_order(
        ctx["exchange"], row, ctx["coin"], ctx["asset"], ctx["current_mid"], is_buy, source_size
    )
    leverage = decimal_or_none(ctx.get("position_leverage")) or decimal_or_none(
        ctx["asset"].get("maxLeverage")
    )
    estimated_margin, adequacy = lifecycle_p10_margin_adequacy(
        market or {}, ctx.get("withdrawable"), leverage
    )
    row["p10_estimated_margin"] = (
        decimal_to_plain(estimated_margin) if estimated_margin is not None else None
    )
    row["p10_margin_adequacy"] = decimal_to_plain(adequacy) if adequacy is not None else None
    if (
        market is None
        or adequacy is None
        or adequacy <= GRID_P10_MARGIN_ADEQUACY_THRESHOLD
        or not lifecycle_p10_projected_within_max(
            row, ctx["position_size"], ctx["position_value"], market
        )
        or raw_action_limit_deficit(cache) > -GRID_P10_REQUIRED_ACTION_HEADROOM
    ):
        row["p10_status"] = "skipped_pre_cancel_safety"
        audit_grid_action(
            "grid_p10_pre_cancel_skipped",
            coin=ctx["coin"],
            grid_id=row.get("id"),
            source_oid=source.get("oid"),
            source_price=decimal_to_plain(source_price),
            source_size=decimal_to_plain(source_size),
            estimated_margin=(decimal_to_plain(estimated_margin) if estimated_margin is not None else None),
            margin_adequacy=(decimal_to_plain(adequacy) if adequacy is not None else None),
            margin_threshold=decimal_to_plain(GRID_P10_MARGIN_ADEQUACY_THRESHOLD),
            projected_within_max=(
                lifecycle_p10_projected_within_max(
                    row, ctx["position_size"], ctx["position_value"], market
                )
                if market is not None
                else False
            ),
            raw_deficit=raw_action_limit_deficit(cache),
        )
        if intent is not None and str(intent.get("status") or "") == "cancelled":
            return lifecycle_restore_p10_source(
                row, ctx, cache, intent, "P10 resume failed pre-cancel safety recheck"
            )
        return False

    if intent is None:
        source_oid = int(source["oid"])
        chain_id = economic_chain_id(row, source)
        intent = {
            "cloid": str(Cloid.from_int(uuid.uuid4().int)),
            "status": "prepared",
            "created_at": ctx["now"],
            "source_oid": source_oid,
            "source_price": decimal_to_plain(source_price),
            "source_size": decimal_to_plain(source_size),
            "source_grid_leg": lifecycle_leg(source),
            "source_iteration": lifecycle_iteration(source),
            "market_is_buy": is_buy,
            "economic_chain_id": chain_id,
            "estimated_margin": decimal_to_plain(estimated_margin or Decimal("0")),
            "margin_adequacy": decimal_to_plain(adequacy or Decimal("0")),
            "chain_realized_surplus": decimal_to_plain(chain_realized_surplus),
            "required_surplus": decimal_to_plain(required_surplus),
            "estimated_chase_cost": decimal_to_plain(chase_cost),
            "execution_buffer": decimal_to_plain(execution_buffer),
        }
        row["p10_promotion_intent"] = intent
        source["p10_intent_cloid"] = intent["cloid"]
        set_grid_order_size_exact(source, source_size)
        persist_lifecycle_intent(cache)
    else:
        source_oid = int(intent["source_oid"])

    if str(intent.get("status")) == "prepared":
        if source_oid not in ctx["open_oids"]:
            try:
                source_status = ctx["info"].query_order_by_oid(ctx["account"], source_oid)
            except Exception as exc:
                intent["last_error"] = str(exc)
                persist_lifecycle_intent(cache)
                return True
            if grid_order_status_name(source_status) == "filled":
                lifecycle_remove_p10_intent(row, source, cache)
                return True
            if not grid_order_status_is_cancelled(source_status):
                intent["last_error"] = f"source order is neither open nor cancelled: {source_status}"
                persist_lifecycle_intent(cache)
                return True
        else:
            request = {"coin": ctx["coin"], "oid": source_oid}
            try:
                reserve_grid_exchange_actions(cache)
                cancel_result = ctx["exchange"].bulk_cancel([request])
            except Exception as exc:
                intent["last_error"] = str(exc)
                persist_lifecycle_intent(cache)
                return True
            audit_grid_action(
                "grid_p10_source_cancel",
                coin=ctx["coin"], grid_id=row.get("id"), source_oid=source_oid,
                economic_chain_id=intent.get("economic_chain_id"), result=cancel_result,
            )
            cancelled, errors = successful_cancel_oids(cancel_result, [request])
            if source_oid not in cancelled:
                intent["last_error"] = "; ".join(errors) or str(cancel_result)
                lifecycle_remove_p10_intent(row, source, cache)
                return True
        source["oid"] = None
        source["status"] = "p10_cancelled"
        source["p10_cancelled_at"] = ctx["now"]
        intent["status"] = "cancelled"
        intent["cancelled_at"] = ctx["now"]
        persist_lifecycle_intent(cache)

    return lifecycle_submit_p10_market(row, ctx, cache, intent, market)


def lifecycle_legacy_pause_candidate(
    rows: list[dict[str, Any]],
    network: str,
    account: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    candidates: list[tuple[Decimal, int, dict[str, Any], dict[str, Any]]] = []
    for candidate_row in rows:
        if not isinstance(candidate_row, dict) or candidate_row.get("type") != "grid":
            continue
        if str(candidate_row.get("network") or "mainnet") != network:
            continue
        if str(candidate_row.get("account") or account).lower() != account.lower():
            continue
        mid = decimal_or_none(candidate_row.get("lifecycle_mid"))
        if mid is None or mid <= 0:
            continue
        for entry in candidate_row.get("levels") or []:
            if not isinstance(entry, dict) or str(entry.get("status") or "") != GRID_LEGACY_PAUSE_STATUS:
                continue
            price = decimal_or_none(entry.get("price", entry.get("limit_px")))
            if price is None or price <= 0:
                continue
            distance = abs(price - mid) / mid
            candidates.append((distance, grid_entry_timestamp_ms(entry) or 0, candidate_row, entry))
    if not candidates:
        return None
    _distance, _timestamp, candidate_row, entry = min(candidates, key=lambda item: item[:2])
    return candidate_row, entry


def lifecycle_p8_buckets(row: dict[str, Any]) -> list[dict[str, Any]]:
    buckets = row.setdefault("p8_partial_debts", [])
    if not isinstance(buckets, list):
        buckets = []
        row["p8_partial_debts"] = buckets
    return buckets


def lifecycle_refresh_p8_summary(row: dict[str, Any]) -> None:
    buckets = [bucket for bucket in lifecycle_p8_buckets(row) if isinstance(bucket, dict)]
    total_value = sum(
        (decimal_or_none(bucket.get("weighted_notional")) or Decimal("0"))
        for bucket in buckets
    )
    row["p8_partial_debt_count"] = len(buckets)
    row["p8_partial_debt_value"] = decimal_to_plain(total_value)
    if not buckets:
        row.pop("p8_partial_debts", None)


def lifecycle_p8_batch_sizes(
    total_size: Decimal,
    price: Decimal,
    sz_decimals: int,
) -> tuple[list[Decimal], Decimal]:
    """Split one P8 bucket into exchange-valid batches just above $10.

    P8 keeps a weighted-average price and an aggregate size, so batches use
    that price while preserving the complete aggregate size.  The strict
    ``> MIN_NOTIONAL`` check is intentional: an order at exactly $10 can be
    rejected by the exchange, and a size step is added when necessary.  Any
    quantity below one valid batch remains in P8 for the next accumulation.
    """
    if total_size <= 0 or price <= 0:
        return [], Decimal("0")
    step = Decimal(1).scaleb(-sz_decimals)
    minimum_size = grid_size_for_min_notional(
        Decimal("0"), price, sz_decimals, MIN_NOTIONAL
    )
    while minimum_size * price <= MIN_NOTIONAL:
        minimum_size += step
    total_units = int((total_size / step).to_integral_value(rounding=ROUND_FLOOR))
    minimum_units = int((minimum_size / step).to_integral_value(rounding=ROUND_FLOOR))
    if minimum_units <= 0 or total_units < minimum_units:
        return [], total_size

    batch_count, remainder_units = divmod(total_units, minimum_units)
    batch_units = [minimum_units] * batch_count
    remainder_size = Decimal(remainder_units) * step
    return [Decimal(units) * step for units in batch_units], remainder_size


def lifecycle_accumulate_p8_partial_debt(
    row: dict[str, Any],
    source: dict[str, Any],
    remaining_size: Decimal,
    price: Decimal,
    now: int,
) -> bool:
    """Accumulate one unfilled leg-1 remainder in its Grid/side P8 bucket."""
    side = str(source.get("side") or "")
    if side not in {"buy", "sell"} or remaining_size <= 0 or price <= 0:
        return False
    source_oids: list[int] = []
    for value in source.get("p8_source_oids") or [source.get("oid")]:
        try:
            source_oids.append(int(value))
        except (TypeError, ValueError):
            continue
    buckets = lifecycle_p8_buckets(row)
    bucket = next(
        (
            candidate
            for candidate in buckets
            if isinstance(candidate, dict) and str(candidate.get("side") or "") == side
        ),
        None,
    )
    if bucket is None:
        bucket = {
            "side": side,
            "is_buy": side == "buy",
            "size": "0",
            "weighted_notional": "0",
            "weighted_avg_price": "0",
            "source_count": 0,
            "source_oids": [],
            "created_at": now,
        }
        buckets.append(bucket)
    known_oids = {
        int(value)
        for value in bucket.get("source_oids") or []
        if isinstance(value, int) or str(value).isdigit()
    }
    if source_oids and all(oid in known_oids for oid in source_oids):
        return False
    old_size = decimal_or_none(bucket.get("size")) or Decimal("0")
    old_notional = decimal_or_none(bucket.get("weighted_notional")) or Decimal("0")
    total_size = old_size + remaining_size
    total_notional = old_notional + remaining_size * price
    bucket["size"] = decimal_to_plain(total_size)
    bucket["weighted_notional"] = decimal_to_plain(total_notional)
    bucket["weighted_avg_price"] = decimal_to_plain(total_notional / total_size)
    bucket["source_count"] = int(bucket.get("source_count") or 0) + int(
        source.get("p8_source_count") or 1
    )
    bucket["source_oids"] = sorted(known_oids.union(source_oids))
    bucket["updated_at"] = now
    lifecycle_refresh_p8_summary(row)
    return True


def lifecycle_migrate_min_rejected_debts_to_p8(row: dict[str, Any], now: int) -> int:
    """Move persisted leg-1 minimum-value rejects out of P3 and into P8."""
    levels = row.setdefault("levels", [])
    migrated = 0
    for entry in list(levels):
        if (
            not isinstance(entry, dict)
            or lifecycle_leg(entry) != 1
            or str(entry.get("status") or "") not in {
                GRID_MARGIN_STATUS,
                GRID_CHAIN_DEBT_STATUS,
                GRID_P8_REACCUMULATE_STATUS,
            }
            or not is_min_order_value_error_text(str(entry.get("last_error") or ""))
        ):
            continue
        size = decimal_or_none(entry.get("size")) or Decimal("0")
        price = decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0")
        if not lifecycle_accumulate_p8_partial_debt(row, entry, size, price, now):
            continue
        levels.remove(entry)
        migrated += 1
    if migrated:
        row["p8_min_reject_migrated"] = int(row.get("p8_min_reject_migrated") or 0) + migrated
        row["p8_min_reject_migrated_at"] = now
    return migrated


def lifecycle_merge_p8_bucket(row: dict[str, Any], incoming: dict[str, Any], now: int) -> None:
    """Merge a migrated P8 remainder into the row's side bucket."""
    side = str(incoming.get("side") or "")
    size = decimal_or_none(incoming.get("size")) or Decimal("0")
    weighted_notional = decimal_or_none(incoming.get("weighted_notional")) or Decimal("0")
    if side not in {"buy", "sell"} or size <= 0 or weighted_notional <= 0:
        return
    buckets = lifecycle_p8_buckets(row)
    bucket = next(
        (
            candidate
            for candidate in buckets
            if isinstance(candidate, dict) and str(candidate.get("side") or "") == side
        ),
        None,
    )
    if bucket is None:
        bucket = {
            "side": side,
            "is_buy": side == "buy",
            "size": "0",
            "weighted_notional": "0",
            "weighted_avg_price": "0",
            "source_count": 0,
            "source_oids": [],
            "created_at": now,
        }
        buckets.append(bucket)
    old_size = decimal_or_none(bucket.get("size")) or Decimal("0")
    old_notional = decimal_or_none(bucket.get("weighted_notional")) or Decimal("0")
    total_size = old_size + size
    total_notional = old_notional + weighted_notional
    known_oids = {
        int(value)
        for value in bucket.get("source_oids") or []
        if isinstance(value, int) or str(value).isdigit()
    }
    incoming_oids = {
        int(value)
        for value in incoming.get("source_oids") or []
        if isinstance(value, int) or str(value).isdigit()
    }
    bucket["size"] = decimal_to_plain(total_size)
    bucket["weighted_notional"] = decimal_to_plain(total_notional)
    bucket["weighted_avg_price"] = decimal_to_plain(total_notional / total_size)
    bucket["source_count"] = int(bucket.get("source_count") or 0) + int(
        incoming.get("source_count") or 0
    )
    bucket["source_oids"] = sorted(known_oids.union(incoming_oids))
    bucket["updated_at"] = now
    lifecycle_refresh_p8_summary(row)


def lifecycle_migrate_oversized_p8_debts(
    row: dict[str, Any],
    now: int,
    cache: dict[str, Any],
) -> int:
    """Split legacy P8-origin debt entries that predate batch metadata.

    Active exchange orders are deliberately left untouched here.  The operator
    migration must cancel those OIDs first, then rerun the same transformation
    with no live order attached; this prevents a state-only split from creating
    duplicate exchange orders.
    """
    levels = row.setdefault("levels", [])
    migrated = 0
    debt_statuses = {
        GRID_MARGIN_STATUS,
        GRID_CHAIN_DEBT_STATUS,
        GRID_P8_REACCUMULATE_STATUS,
    }
    for entry in list(levels):
        if (
            not isinstance(entry, dict)
            or entry.get("birth_source") != "p8_partial_debt"
            or entry.get("oid") is not None
            or str(entry.get("status") or "") not in debt_statuses
        ):
            continue
        try:
            if int(entry.get("p8_batch_count") or 0) > 1:
                continue
        except (TypeError, ValueError):
            pass
        weighted_notional = decimal_or_none(entry.get("p8_weighted_notional")) or Decimal("0")
        average_price = decimal_or_none(entry.get("p8_weighted_avg_price"))
        size = decimal_or_none(entry.get("size")) or Decimal("0")
        if weighted_notional <= Decimal("50") or average_price is None or average_price <= 0:
            continue
        if size <= 0:
            size = weighted_notional / average_price
        side = str(entry.get("side") or "")
        if side not in {"buy", "sell"}:
            continue

        bucket = {
            "side": side,
            "is_buy": side == "buy",
            "size": decimal_to_plain(size),
            "weighted_notional": decimal_to_plain(weighted_notional),
            "weighted_avg_price": decimal_to_plain(average_price),
            "source_count": int(entry.get("p8_source_count") or 0),
            "source_oids": list(entry.get("p8_source_oids") or []),
            "created_at": int(entry.get("p8_promoted_at") or now),
        }
        temporary_row = dict(row)
        temporary_row["levels"] = []
        temporary_row["p8_partial_debts"] = [bucket]
        temporary_row.pop("p8_partial_debt_count", None)
        temporary_row.pop("p8_partial_debt_value", None)
        promoted, _changed = lifecycle_process_p8(temporary_row, now, cache)
        children = temporary_row.get("levels") or []
        if not promoted or not children:
            continue

        parent_chain_id = entry.get("economic_chain_id")
        original_queue_seq = entry.get("p3_queue_seq")
        for child in children:
            child["status"] = str(entry.get("status") or GRID_CHAIN_DEBT_STATUS)
            child["legacy_p8_migrated_at"] = now
            if parent_chain_id:
                child["p8_parent_economic_chain_id"] = parent_chain_id
        if original_queue_seq is not None:
            children[0]["p3_queue_seq"] = original_queue_seq
        levels.remove(entry)
        levels.extend(children)
        for remainder in temporary_row.get("p8_partial_debts") or []:
            lifecycle_merge_p8_bucket(row, remainder, now)
        migrated += 1
    if migrated:
        row["p8_oversized_debt_migrated"] = int(
            row.get("p8_oversized_debt_migrated") or 0
        ) + migrated
        row["p8_oversized_debt_migrated_at"] = now
    return migrated


def lifecycle_process_p8(
    row: dict[str, Any],
    now: int,
    cache: dict[str, Any],
) -> tuple[int, bool]:
    """Promote strictly-above-$10 side buckets into next-round P3 debt."""
    buckets = lifecycle_p8_buckets(row)
    if not buckets:
        lifecycle_refresh_p8_summary(row)
        return 0, False
    levels = row.setdefault("levels", [])
    sz_decimals = int(row.get("sz_decimals") or 0)
    asset = {"szDecimals": sz_decimals}
    coin = str(row.get("coin") or "")
    promoted = 0
    changed = False
    for bucket in list(buckets):
        if not isinstance(bucket, dict):
            buckets.remove(bucket)
            changed = True
            continue
        size = decimal_or_none(bucket.get("size")) or Decimal("0")
        weighted_notional = decimal_or_none(bucket.get("weighted_notional")) or Decimal("0")
        side = str(bucket.get("side") or "")
        if size <= 0 or weighted_notional <= MIN_NOTIONAL or side not in {"buy", "sell"}:
            continue
        average_price = weighted_notional / size
        price = rounded_perp_price(average_price, sz_decimals)
        if price <= 0 or size * price <= MIN_NOTIONAL:
            continue
        batch_sizes, remainder_size = lifecycle_p8_batch_sizes(size, price, sz_decimals)
        if not batch_sizes:
            continue
        batch_count = len(batch_sizes)
        source_count = int(bucket.get("source_count") or 0)
        source_oids = list(bucket.get("source_oids") or [])
        for batch_index, batch_size in enumerate(batch_sizes, start=1):
            batch_notional = weighted_notional * batch_size / size
            order = grid_order_entry(
                row,
                coin,
                asset,
                side == "buy",
                price,
                False,
                size=batch_size,
                gap=Decimal(str(row["gap_rate"])),
                preserve_size=True,
            )
            order.update(
                {
                    "status": GRID_CHAIN_DEBT_STATUS,
                    "oid": None,
                    "grid_leg": 1,
                    "birth_source": "p8_partial_debt",
                    "preserve_fill_size": True,
                    "p8_promoted_at": now,
                    "p8_weighted_notional": decimal_to_plain(batch_notional),
                    "p8_weighted_avg_price": decimal_to_plain(average_price),
                    # Keep aggregate provenance outside p8_source_oids for
                    # split batches.  Reusing the same source OIDs on every
                    # batch would make a later failed batch look duplicated
                    # when it returns to the P8 accumulator.
                    "p8_source_count": source_count if batch_count == 1 else 0,
                    "p8_source_oids": source_oids if batch_count == 1 else [],
                    "p8_batch_source_count": source_count,
                    "p8_batch_source_oids": source_oids,
                    "p8_batch_index": batch_index,
                    "p8_batch_count": batch_count,
                    "p8_batch_total_size": decimal_to_plain(size),
                    "p8_batch_total_notional": decimal_to_plain(weighted_notional),
                    "chain_debt_at": now,
                }
            )
            lifecycle_assign_p3_queue_seq(order, cache)
            levels.append(order)
            promoted += 1
        if remainder_size > 0:
            remainder_notional = weighted_notional * remainder_size / size
            bucket["size"] = decimal_to_plain(remainder_size)
            bucket["weighted_notional"] = decimal_to_plain(remainder_notional)
            bucket["weighted_avg_price"] = decimal_to_plain(average_price)
            bucket["updated_at"] = now
        else:
            buckets.remove(bucket)
        changed = True
    lifecycle_refresh_p8_summary(row)
    return promoted, changed


def lifecycle_process_fills(
    row: dict[str, Any],
    ctx: dict[str, Any],
    cache: dict[str, Any],
) -> tuple[int, bool]:
    levels = row.setdefault("levels", [])
    newly_filled: list[dict[str, Any]] = []
    changed = False
    for entry in list(levels):
        if not isinstance(entry, dict) or not entry.get("side"):
            continue
        if bool(entry.get("replacement_pending")):
            newly_filled.append(entry)
            continue
        if str(entry.get("status") or "") == "filled":
            # A filled row without replacement_pending is terminal history.
            continue
        if str(entry.get("status") or "") != "active":
            continue
        try:
            oid = int(entry.get("oid"))
        except (TypeError, ValueError):
            continue
        if oid in ctx["open_oids"]:
            continue
        fill = ctx["fills_by_oid"].get(oid)
        order_status = None
        if fill is None or lifecycle_leg(entry) == 1:
            order_status = ctx["info"].query_order_by_oid(ctx["account"], oid)
        # ``origSz > sz`` can also be caused by an in-place resize.  In P2,
        # only a fill record keyed by this OID authorizes the partial-fill
        # bookkeeping and P8 remainder path; otherwise leave the order for
        # P5 to classify as an intact, zero-fill cancellation.
        partial_fill_size = (
            grid_order_status_partial_fill_size(order_status)
            if fill is not None
            else None
        )
        if partial_fill_size is not None:
            entry["filled_size"] = decimal_to_plain(partial_fill_size)
            entry["confirmed_partial_filled_oid"] = oid
            entry["exchange_cancel_status"] = grid_order_status_name(order_status)
            partial_remainder = grid_order_status_partial_remainder(order_status)
            if partial_remainder is not None:
                remaining_size, exchange_price = partial_remainder
                entry["partial_unfilled_size"] = decimal_to_plain(remaining_size)
                if exchange_price is not None:
                    entry["partial_unfilled_price"] = decimal_to_plain(exchange_price)
        elif fill is None:
            if grid_order_status_name(order_status) != "filled":
                continue
            entry["confirmed_filled_oid"] = oid
        if fill is not None:
            entry["fill"] = fill
        entry["status"] = "filled"
        entry["filled_at"] = ctx["now"]
        entry["replacement_pending"] = True
        newly_filled.append(entry)
        changed = True

    submitted = 0
    isolated_ready: set[str] = cache.setdefault("lifecycle_isolated_ready", set())
    for source in newly_filled:
        chain_id = economic_chain_id(row, source)
        if not source.get("economic_fill_audited_at"):
            fill_price, fill_size = lifecycle_fill_price_size(source)
            audit_grid_action(
                "grid_fill_confirmed",
                coin=ctx["coin"],
                grid_id=row.get("id"),
                economic_chain_id=chain_id,
                oid=source.get("oid"),
                side=source.get("side"),
                price=decimal_to_plain(fill_price) if fill_price is not None else None,
                size=decimal_to_plain(fill_size) if fill_size is not None else None,
                grid_leg=lifecycle_leg(source),
                iteration=lifecycle_iteration(source),
                birth_source=source.get("birth_source"),
                birth_slot=source.get("birth_slot"),
                fill=source.get("fill"),
            )
            source["economic_fill_audited_at"] = ctx["now"]
            changed = True
        if source.get("confirmed_partial_filled_oid") is not None and lifecycle_leg(source) == 1:
            remaining_size = decimal_or_none(source.get("partial_unfilled_size"))
            if remaining_size is None:
                original_size = decimal_or_none(source.get("size")) or Decimal("0")
                filled_size = decimal_or_none(source.get("filled_size")) or Decimal("0")
                remaining_size = original_size - filled_size
            if remaining_size == 0:
                # Compatibility for rows persisted while a terminal full fill
                # (origSz > 0, sz == 0) was misclassified as a partial fill.
                # Clear the partial marker so P2 builds the normal opposite
                # leg from the confirmed resting fill instead of trying to
                # accumulate a zero-sized P8 remainder forever.
                source["confirmed_filled_oid"] = source.pop("confirmed_partial_filled_oid")
                source.pop("partial_unfilled_size", None)
                source.pop("partial_unfilled_price", None)
                if source.get("last_error") == "partial leg-1 remainder could not enter P8":
                    source.pop("last_error", None)
            else:
                remaining_price = decimal_or_none(
                    source.get("partial_unfilled_price", source.get("price", source.get("limit_px")))
                )
                accumulated = (
                    remaining_size > 0
                    and remaining_price is not None
                    and lifecycle_accumulate_p8_partial_debt(
                        row, source, remaining_size, remaining_price, ctx["now"]
                    )
                )
                if not accumulated:
                    source["last_error"] = "partial leg-1 remainder could not enter P8"
                    continue
                if source in levels:
                    levels.remove(source)
                source["replacement_pending"] = False
                source["replacement_processed_at"] = ctx["now"]
                source["partial_chain_terminal_at"] = ctx["now"]
                source["partial_chain_terminal_reason"] = "partial_fill_remainder_moved_to_p8"
                changed = True
                continue
        child = lifecycle_replacement_from_fill(row, ctx["coin"], ctx["asset"], source)
        if child is None:
            source["last_error"] = "filled lifecycle order could not build its replacement"
            continue
        lifecycle_ensure_context_book(ctx, cache)
        result = lifecycle_submit_order(
            ctx["exchange"], ctx["coin"], child, ctx["now"], row, ctx["asset"],
            ctx["position_size"], ctx["current_mid"], ctx["best_bid"], ctx["best_ask"],
            isolated_ready, ctx["open_orders"], cache, search_outward=True,
            allow_non_reduce_only_fallback=True,
        )
        if source in levels:
            levels.remove(source)
        if result in {"submitted", GRID_MARGIN_STATUS, GRID_CHAIN_DEBT_STATUS}:
            levels.append(child)
        elif result == GRID_P8_REACCUMULATE_STATUS:
            lifecycle_accumulate_p8_partial_debt(
                row,
                child,
                decimal_or_none(child.get("size")) or Decimal("0"),
                decimal_or_none(child.get("price", child.get("limit_px"))) or Decimal("0"),
                ctx["now"],
            )
        source["replacement_pending"] = False
        source["replacement_processed_at"] = ctx["now"]
        submitted += int(result == "submitted")
        changed = True
    return submitted, changed


def lifecycle_process_anomalies(row: dict[str, Any], ctx: dict[str, Any], cache: dict[str, Any]) -> tuple[int, bool]:
    levels = row.setdefault("levels", [])
    enqueued = 0
    changed = False
    for entry in list(levels):
        if not isinstance(entry, dict) or str(entry.get("status") or "") != "active":
            continue
        try:
            oid = int(entry.get("oid"))
        except (TypeError, ValueError):
            continue
        if oid in ctx["open_oids"]:
            continue
        fill = ctx["fills_by_oid"].get(oid)
        order_status = None
        if fill is None or lifecycle_leg(entry) == 1:
            order_status = ctx["info"].query_order_by_oid(ctx["account"], oid)
        status_name = grid_order_status_name(order_status)
        if status_name == "filled" or (fill is not None and not status_name):
            entry["status"] = "filled"
            entry["fill"] = fill if fill is not None else entry.get("fill")
            entry["replacement_pending"] = True
            entry["filled_at"] = ctx["now"]
            changed = True
            continue
        if status_name.strip().lower() in {"open", "resting"}:
            continue
        if status_name == "reduceOnlyCanceled" or grid_order_status_is_cancelled(order_status):
            # A smaller exchange ``sz`` is not sufficient proof of a fill:
            # an order may have been resized in place before the exchange
            # canceled it.  Only a fill record keyed by this OID can certify
            # the partial-fill -> P8 path.  Otherwise preserve the local
            # original size and send the whole leg-1 debt to the P3 tail.
            partial_fill_size = (
                grid_order_status_partial_fill_size(order_status)
                if fill is not None
                else None
            )
            if partial_fill_size is not None:
                partial_remainder = grid_order_status_partial_remainder(order_status)
                entry["status"] = "filled"
                entry["filled_size"] = decimal_to_plain(partial_fill_size)
                entry["confirmed_partial_filled_oid"] = oid
                entry["exchange_cancel_status"] = status_name
                entry["filled_at"] = ctx["now"]
                if partial_remainder is not None:
                    remaining_size, exchange_price = partial_remainder
                    remaining_price = exchange_price or decimal_or_none(
                        entry.get("price", entry.get("limit_px"))
                    )
                else:
                    original_size = decimal_or_none(entry.get("size")) or Decimal("0")
                    remaining_size = original_size - partial_fill_size
                    remaining_price = decimal_or_none(entry.get("price", entry.get("limit_px")))
                if lifecycle_leg(entry) == 1:
                    if (
                        remaining_price is None
                        or not lifecycle_accumulate_p8_partial_debt(
                            row, entry, remaining_size, remaining_price, ctx["now"]
                        )
                    ):
                        entry["last_error"] = "partial leg-1 remainder could not enter P8"
                        changed = True
                        continue
                    entry["replacement_pending"] = False
                    entry["partial_chain_terminal_at"] = ctx["now"]
                    entry["partial_chain_terminal_reason"] = "partial_fill_remainder_moved_to_p8"
                    if entry in levels:
                        levels.remove(entry)
                    changed = True
                    continue
                entry["replacement_pending"] = True
                changed = True
                continue
            if lifecycle_leg(entry) == 0:
                levels.remove(entry)
                changed = True
                continue
            # P5 only classifies the cancelled leg-1 order.  Leave the actual
            # restore to the next worker's P3 FIFO, so all debt follows the
            # same withdrawable, spacing, and action-budget gates.
            entry["oid"] = None
            entry["status"] = GRID_CHAIN_DEBT_STATUS
            entry["chain_debt_at"] = ctx["now"]
            entry["p5_restore_queued_at"] = ctx["now"]
            # This is a new debt event, so it joins the current queue tail
            # instead of reclaiming a FIFO position from an earlier debt.
            # Keep the existing economic chain: P5->P3 is recovery of the
            # same lifecycle leg, not a new economic birth.
            entry.pop("p3_queue_seq", None)
            lifecycle_assign_p3_queue_seq(entry, cache)
            levels.remove(entry)
            levels.append(entry)
            enqueued += 1
            changed = True
    return enqueued, changed


def persist_lifecycle_intent(cache: dict[str, Any]) -> None:
    rows = cache.get("grid_rows")
    if isinstance(rows, list):
        save_server_batch(rows)


def lifecycle_birth_intents(row: dict[str, Any]) -> list[dict[str, Any]]:
    intents = row.setdefault("birth_market_intents", [])
    if not isinstance(intents, list):
        intents = []
        row["birth_market_intents"] = intents
    return intents


def lifecycle_create_birth_intent(
    row: dict[str, Any],
    market: dict[str, Any],
    source: str,
    now: int,
    cache: dict[str, Any],
) -> dict[str, Any]:
    plan = market.get("plan") if isinstance(market.get("plan"), dict) else {}
    chain_id = new_economic_chain_id()
    intent = {
        "cloid": str(Cloid.from_int(uuid.uuid4().int)),
        "economic_chain_id": chain_id,
        "economic_chain_branch_version": 1,
        "source": source,
        "status": "submitting",
        "created_at": now,
        "market_is_buy": bool(plan.get("is_buy", market.get("is_buy"))),
        "market_size": decimal_to_plain(Decimal(str(plan.get("size", market.get("size", "0"))))),
        "market_limit_px": decimal_to_plain(Decimal(str(plan.get("limit_px", market.get("price", "0"))))),
    }
    market["economic_chain_id"] = chain_id
    lifecycle_birth_intents(row).append(intent)
    persist_lifecycle_intent(cache)
    return intent


def lifecycle_birth_intent_economic_chain_id(intent: dict[str, Any]) -> str:
    """Keep pre-migration cloid chains intact; new intents carry a short ID."""
    chain_id = str(intent.get("economic_chain_id") or "").strip()
    if not chain_id:
        chain_id = str(intent.get("cloid") or "").strip()
        if chain_id:
            intent["economic_chain_id"] = chain_id
    return chain_id


def lifecycle_birth_branch_chain_ids(
    intent: dict[str, Any],
    child_count: int,
) -> list[str]:
    """Return stable per-child IDs for newly created split P0/P4 chains."""
    root_chain_id = lifecycle_birth_intent_economic_chain_id(intent)
    if child_count <= 1 or int(intent.get("economic_chain_branch_version") or 0) < 1:
        return [root_chain_id for _ in range(child_count)]
    return [f"{root_chain_id}_{index}" for index in range(1, child_count + 1)]


def lifecycle_remove_birth_intent(row: dict[str, Any], intent: dict[str, Any], cache: dict[str, Any]) -> None:
    intents = lifecycle_birth_intents(row)
    if intent in intents:
        intents.remove(intent)
    if not intents:
        row.pop("birth_market_intents", None)
    persist_lifecycle_intent(cache)


def lifecycle_has_birth_intent(row: dict[str, Any], source: str) -> bool:
    return any(
        isinstance(intent, dict) and str(intent.get("source") or "") == source
        for intent in row.get("birth_market_intents") or []
    )


def lifecycle_birth_twin_orders(
    row: dict[str, Any],
    asset: dict[str, Any],
    near_birth: dict[str, Any],
    fill_price: Decimal,
    fill_size: Decimal,
    _max_child_count: int | None = None,
) -> list[dict[str, Any]]:
    """Split a P0/P4 fill into size-aware near/far children."""
    gap = Decimal(str(row["gap_rate"]))
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    step = Decimal(1).scaleb(-sz_decimals)
    near_price = decimal_or_none(near_birth.get("price", near_birth.get("limit_px")))
    side = str(near_birth.get("side") or "")
    size_key = "base_buy_size" if side == "buy" else "base_sell_size"
    base_size = decimal_or_none(row.get(size_key))
    if base_size is not None:
        base_size = (base_size / step).to_integral_value(rounding=ROUND_FLOOR) * step
    child_count = 2
    if base_size is not None and base_size > 0 and fill_size > base_size * Decimal("3"):
        child_count = max(3, int((fill_size / base_size).to_integral_value(rounding=ROUND_FLOOR)))
    if _max_child_count is not None:
        child_count = max(2, min(child_count, _max_child_count))

    def fewer_layers_or_single() -> list[dict[str, Any]]:
        if child_count > 2:
            return lifecycle_birth_twin_orders(
                row,
                asset,
                near_birth,
                fill_price,
                fill_size,
                child_count - 1,
            )
        near_birth["birth_slot"] = "single"
        return [near_birth]

    child_size = ((fill_size / Decimal(child_count)) / step).to_integral_value(rounding=ROUND_FLOOR) * step
    child_sizes = [child_size for _ in range(child_count)]
    child_sizes[-1] = ((fill_size - child_size * Decimal(child_count - 1)) / step).to_integral_value(
        rounding=ROUND_FLOOR
    ) * step
    if (
        fill_price <= 0
        or gap <= 0
        or near_price is None
        or near_price <= 0
        or side not in {"buy", "sell"}
        or any(size <= 0 for size in child_sizes)
    ):
        return fewer_layers_or_single()

    active_prices = [
        price
        for entry in row.get("levels") or []
        if (
            isinstance(entry, dict)
            and str(entry.get("status") or "") == "active"
            and str(entry.get("side") or "") == side
        )
        if (price := decimal_or_none(entry.get("price", entry.get("limit_px")))) is not None
        and price > 0
    ]
    multiplier = Decimal("1") - gap if side == "buy" else Decimal("1") + gap
    price_search_attempts = (
        GRID_ALO_PRICE_ATTEMPT_LIMIT
        + len(active_prices) * 3
        + child_count * 3
    )

    def find_clear_outward_price(
        anchor_price: Decimal,
        generated_prices: list[Decimal],
    ) -> tuple[Decimal, int] | None:
        price = anchor_price
        for spacing_adjustments in range(price_search_attempts):
            generated_too_close = any(
                abs(price - generated_price) / generated_price <= gap * Decimal("0.95")
                for generated_price in generated_prices
                if generated_price > 0
            )
            if not generated_too_close and not lifecycle_active_price_too_close(row, side, price):
                return price, spacing_adjustments
            next_price = rounded_perp_price(price * multiplier, sz_decimals)
            if next_price <= 0 or next_price == price:
                return None
            price = next_price
        return None

    near_anchor_price = near_price
    near_result = find_clear_outward_price(near_anchor_price, [])
    if near_result is None:
        return []
    near_price, near_spacing_adjustments = near_result
    has_farther_active = False
    if side == "buy":
        farthest_active = min(active_prices) if active_prices else None
        if farthest_active is not None and farthest_active < near_price:
            has_farther_active = True
            far_boundary = farthest_active
        else:
            far_boundary = fill_price * (Decimal("1") - gap * Decimal(child_count + 2))
            if far_boundary >= near_price:
                far_boundary = near_price * (Decimal("1") - gap * Decimal(child_count))
    elif side == "sell":
        farthest_active = max(active_prices) if active_prices else None
        if farthest_active is not None and farthest_active > near_price:
            has_farther_active = True
            far_boundary = farthest_active
        else:
            far_boundary = fill_price * (Decimal("1") + gap * Decimal(child_count + 2))
            if far_boundary <= near_price:
                far_boundary = near_price * (Decimal("1") + gap * Decimal(child_count))

    far_prices: list[Decimal] = []
    far_metadata: list[tuple[Decimal, int]] = []
    for index in range(1, child_count):
        anchor_price = rounded_perp_price(
            near_price + (far_boundary - near_price) * Decimal(index) / Decimal(child_count),
            sz_decimals,
        )
        far_result = find_clear_outward_price(anchor_price, [near_price, *far_prices])
        if far_result is None:
            return []
        far_price, spacing_adjustments = far_result
        far_prices.append(far_price)
        far_metadata.append((anchor_price, spacing_adjustments))

    min_notional = max(
        MIN_NOTIONAL,
        decimal_or_none(row.get("min_order_value")) or MIN_NOTIONAL,
    )
    if (
        any(price <= 0 for price in far_prices)
        or child_sizes[0] * near_price < min_notional
        or any(size * price < min_notional for size, price in zip(child_sizes[1:], far_prices))
    ):
        return fewer_layers_or_single()

    set_grid_order_size_exact(near_birth, child_sizes[0])
    set_grid_order_price(near_birth, near_price)
    near_birth["birth_slot"] = "near"
    near_plan = near_birth.get("plan")
    if isinstance(near_plan, dict):
        near_plan["birth_slot"] = "near"
        near_plan["birth_size_fraction"] = Decimal("1") / Decimal(child_count)
        near_plan["birth_layer_count"] = child_count
        if near_spacing_adjustments:
            near_plan["birth_near_anchor_price"] = near_anchor_price
            near_plan["birth_near_spacing_adjustments"] = near_spacing_adjustments

    births = [near_birth]
    for index, (size, price, metadata) in enumerate(
        zip(child_sizes[1:], far_prices, far_metadata),
        start=1,
    ):
        far_birth = deepcopy(near_birth)
        set_grid_order_size_exact(far_birth, size)
        set_grid_order_price(far_birth, price)
        slot = "far" if child_count == 2 else f"far{index}"
        far_birth["birth_slot"] = slot
        far_plan = far_birth.get("plan")
        if isinstance(far_plan, dict):
            far_plan["birth_slot"] = slot
            far_plan["birth_size_fraction"] = Decimal("1") / Decimal(child_count)
            far_plan["birth_layer_count"] = child_count
            far_plan["birth_far_fraction"] = Decimal(index) / Decimal(child_count)
            if has_farther_active:
                far_plan["birth_far_anchor_source"] = (
                    "near_farthest_active_midpoint"
                    if child_count == 2
                    else "near_farthest_active_fraction"
                )
            else:
                far_plan["birth_far_anchor_source"] = (
                    "fill_3gap" if child_count == 2 else "fill_gap_ladder"
                )
            if has_farther_active:
                far_plan["birth_far_active_price"] = farthest_active
            anchor_price, spacing_adjustments = metadata
            if spacing_adjustments:
                far_plan["birth_far_anchor_price"] = anchor_price
                far_plan["birth_far_spacing_adjustments"] = spacing_adjustments
        births.append(far_birth)
    return births


def lifecycle_materialize_birth_intent(
    row: dict[str, Any],
    ctx: dict[str, Any],
    cache: dict[str, Any],
    intent: dict[str, Any],
    fill_price: Decimal,
    fill_size: Decimal,
) -> bool:
    if fill_price <= 0 or fill_size <= 0:
        intent["status"] = "filled_waiting_for_fill_details"
        intent["last_error"] = "market fill did not include a positive price and size"
        persist_lifecycle_intent(cache)
        return False
    cloid_text = str(intent.get("cloid") or "")
    chain_id = lifecycle_birth_intent_economic_chain_id(intent)
    levels = row.setdefault("levels", [])
    existing_entries = [
        entry
        for entry in levels
        if isinstance(entry, dict) and str(entry.get("birth_intent_cloid") or "") == cloid_text
    ]
    if any(not entry.get("birth_slot") for entry in existing_entries):
        # A pre-twin deployment may already have materialized this cloid as
        # one full-size birth.  Treat it as complete rather than duplicating it.
        lifecycle_remove_birth_intent(row, intent, cache)
        return True

    source = str(intent.get("source") or "")
    market_is_buy = bool(intent.get("market_is_buy"))
    if source == "panic":
        near_birth = panic_reversal_order_from_reduce(
            row,
            ctx["coin"],
            ctx["asset"],
            fill_price,
            market_is_buy,
            fill_size,
            ctx["position_size"],
            grid_limit_policy_from_row(row),
            "market_fill",
        )
    elif source == "limit_chase":
        near_birth = limit_chase_replacement_order_from_market(
            row,
            ctx["coin"],
            ctx["asset"],
            fill_price,
            market_is_buy,
            fill_size,
            "market_fill",
        )
    else:
        near_birth = None
    if near_birth is None:
        intent["status"] = "filled_waiting_for_birth"
        intent["last_error"] = "filled market action could not build its leg-1 birth order"
        persist_lifecycle_intent(cache)
        return False

    births = lifecycle_birth_twin_orders(
        row,
        ctx["asset"],
        near_birth,
        fill_price,
        fill_size,
    )
    if not births:
        intent["status"] = "filled_waiting_for_birth"
        intent["last_error"] = "filled market action could not split its birth order"
        persist_lifecycle_intent(cache)
        return False

    branch_chain_ids = lifecycle_birth_branch_chain_ids(intent, len(births))
    branch_by_slot = {
        str(birth.get("birth_slot") or "single"): (index, branch_chain_ids[index - 1])
        for index, birth in enumerate(births, start=1)
    }
    branching_enabled = (
        len(births) > 1
        and int(intent.get("economic_chain_branch_version") or 0) >= 1
    )
    for entry in existing_entries:
        slot = str(entry.get("birth_slot") or "single")
        branch = branch_by_slot.get(slot)
        if branch is None:
            continue
        branch_index, branch_chain_id = branch
        if branching_enabled or not str(entry.get("economic_chain_id") or "").strip():
            entry["economic_chain_id"] = branch_chain_id
        if branching_enabled:
            entry["economic_chain_root_id"] = chain_id
            entry["economic_chain_branch"] = branch_index

    existing = {
        str(entry.get("birth_slot") or "single"): entry
        for entry in existing_entries
    }
    appended: list[dict[str, Any]] = []
    for branch_index, birth in enumerate(births, start=1):
        slot = str(birth.get("birth_slot") or "single")
        if slot in existing:
            continue
        birth.pop("replace_never_cancel", None)
        birth["grid_leg"] = 1
        birth["iteration"] = 0
        birth["birth_source"] = source
        birth["birth_intent_cloid"] = cloid_text
        birth["economic_chain_id"] = branch_chain_ids[branch_index - 1]
        if branching_enabled:
            birth["economic_chain_root_id"] = chain_id
            birth["economic_chain_branch"] = branch_index
        birth["grid_id"] = row.get("id")
        birth["preserve_fill_size"] = True
        birth["status"] = GRID_CHAIN_DEBT_STATUS
        lifecycle_assign_p3_queue_seq(birth, cache)
        levels.append(birth)
        appended.append(birth)
    intent["status"] = "materializing_branches"
    persist_lifecycle_intent(cache)
    results: dict[str, str] = {}
    for birth in appended:
        result = lifecycle_submit_order(
            ctx["exchange"],
            ctx["coin"],
            birth,
            ctx["now"],
            row,
            ctx["asset"],
            ctx["position_size"],
            ctx["current_mid"],
            ctx["best_bid"],
            ctx["best_ask"],
            cache.setdefault("lifecycle_isolated_ready", set()),
            ctx["open_orders"],
            cache,
            search_outward=False,
            force_gtc=True,
        )
        results[str(birth.get("birth_slot") or "single")] = result
        if result == GRID_P8_REACCUMULATE_STATUS and lifecycle_accumulate_p8_partial_debt(
            row,
            birth,
            decimal_or_none(birth.get("size")) or Decimal("0"),
            decimal_or_none(birth.get("price", birth.get("limit_px"))) or Decimal("0"),
            ctx["now"],
        ):
            if birth in levels:
                levels.remove(birth)
    row[f"{source}_fill_price"] = decimal_to_plain(fill_price)
    row[f"{source}_birth_status"] = results or "already_materialized"
    audit_grid_action(
        "grid_birth_materialized",
        coin=ctx["coin"],
        grid_id=row.get("id"),
        economic_chain_id=chain_id,
        economic_chain_branch_version=(1 if branching_enabled else 0),
        birth_source=source,
        market_oid=intent.get("market_oid"),
        market_side="buy" if market_is_buy else "sell",
        market_fill_price=decimal_to_plain(fill_price),
        market_fill_size=decimal_to_plain(fill_size),
        children=[
            {
                "side": birth.get("side"),
                "price": birth.get("price", birth.get("limit_px")),
                "size": birth.get("size"),
                "birth_slot": birth.get("birth_slot"),
                "status": birth.get("status"),
                "oid": birth.get("oid"),
                "p3_queue_seq": birth.get("p3_queue_seq"),
                "economic_chain_id": birth.get("economic_chain_id"),
                "economic_chain_root_id": birth.get("economic_chain_root_id"),
                "economic_chain_branch": birth.get("economic_chain_branch"),
            }
            for birth in births
        ],
        submit_results=results or "already_materialized",
    )
    lifecycle_remove_birth_intent(row, intent, cache)
    return True


def lifecycle_reconcile_birth_intents(
    row: dict[str, Any],
    ctx: dict[str, Any],
    cache: dict[str, Any],
) -> bool:
    changed = False
    for intent in list(row.get("birth_market_intents") or []):
        if not isinstance(intent, dict) or not intent.get("cloid"):
            continue
        try:
            order_status = ctx["info"].query_order_by_cloid(
                ctx["account"], Cloid.from_str(str(intent["cloid"]))
            )
        except Exception as exc:
            intent["last_error"] = str(exc)
            intent["last_checked_at"] = ctx["now"]
            changed = True
            continue
        status_name = grid_order_status_name(order_status).strip()
        intent["exchange_status"] = status_name
        intent["last_checked_at"] = ctx["now"]
        order_wrapper = order_status.get("order") if isinstance(order_status, dict) else None
        order_data = order_wrapper.get("order") if isinstance(order_wrapper, dict) else None
        oid = None
        if isinstance(order_data, dict) and order_data.get("oid") is not None:
            oid = int(order_data["oid"])
            intent["market_oid"] = oid
        if status_name == "filled" and oid is not None:
            fill = ctx["fills_by_oid"].get(oid)
            fill_price = decimal_or_none(fill.get("px")) if isinstance(fill, dict) else None
            fill_size = decimal_or_none(fill.get("sz")) if isinstance(fill, dict) else None
            if fill_price is not None and fill_size is not None:
                lifecycle_materialize_birth_intent(row, ctx, cache, intent, fill_price, fill_size)
            else:
                intent["status"] = "filled_waiting_for_fill_details"
                persist_lifecycle_intent(cache)
            changed = True
            continue
        lowered = status_name.lower()
        if grid_order_status_is_cancelled(order_status) or lowered in {"rejected", "margincanceled"}:
            lifecycle_remove_birth_intent(row, intent, cache)
            changed = True
            continue
        if lowered == "unknownoid" and ctx["now"] - int(intent.get("created_at") or 0) >= GRID_BIRTH_INTENT_UNKNOWN_GRACE_SECONDS:
            lifecycle_remove_birth_intent(row, intent, cache)
            changed = True
            continue
        intent["status"] = "awaiting_reconcile"
        changed = True
    return changed


def lifecycle_submit_limit_chase(row: dict[str, Any], ctx: dict[str, Any], cache: dict[str, Any]) -> bool:
    if lifecycle_has_birth_intent(row, "limit_chase"):
        return True
    is_buy = grid_limit_chase_direction(row, ctx["position_size"], ctx["position_value"])
    if is_buy is None:
        return False
    reduction_mode = (
        (is_buy and ctx["position_size"] < 0)
        or (not is_buy and ctx["position_size"] > 0)
    )
    if not reduction_mode:
        return False
    sz_decimals = int(row.get("sz_decimals") or ctx["asset"]["szDecimals"])
    reduction_target = (
        grid_limit_chase_reduction_target_size(
            row,
            ctx["position_size"],
            ctx["position_value"],
            ctx["current_mid"],
            is_buy,
            sz_decimals,
        )
    )
    market = build_grid_limit_chase_market_order(
        ctx["exchange"],
        row,
        ctx["coin"],
        ctx["asset"],
        ctx["current_mid"],
        is_buy,
        max_size=abs(ctx["position_size"]),
        target_size=reduction_target,
    )
    if market is None:
        return False
    plan = market.get("plan")
    if not isinstance(plan, dict):
        return False
    intent = lifecycle_create_birth_intent(row, market, "limit_chase", ctx["now"], cache)
    try:
        reserve_grid_exchange_actions(cache)
        result = ctx["exchange"].order(
            ctx["coin"], bool(plan["is_buy"]), float(plan["size"]), float(plan["limit_px"]),
            plan["order_type"], reduce_only=bool(plan.get("reduce_only")),
            cloid=Cloid.from_str(str(intent["cloid"])),
        )
    except Exception as exc:
        intent["status"] = "awaiting_reconcile"
        intent["last_error"] = str(exc)
        intent["last_checked_at"] = ctx["now"]
        row["limit_chase_error"] = str(exc)
        persist_lifecycle_intent(cache)
        return True
    audit_grid_action(
        "limit_chase_market_submit",
        coin=ctx["coin"],
        side=market.get("side"),
        price=market.get("price"),
        size=market.get("size"),
        reduce_only=bool(plan.get("reduce_only")),
        economic_chain_id=lifecycle_birth_intent_economic_chain_id(intent),
        birth_source="limit_chase",
        grid_id=row.get("id"),
        result=result,
    )
    if not isinstance(result, dict) or result.get("status") != "ok":
        row["limit_chase_error"] = str(result)
        lifecycle_remove_birth_intent(row, intent, cache)
        return False
    filled: dict[str, Any] | None = None
    for status in result.get("response", {}).get("data", {}).get("statuses", []):
        if isinstance(status, dict) and isinstance(status.get("filled"), dict):
            filled = status["filled"]
            break
        if isinstance(status, dict) and status.get("error"):
            row["limit_chase_error"] = str(status["error"])
            lifecycle_remove_birth_intent(row, intent, cache)
            return False
    if filled is None:
        row["limit_chase_error"] = str(result)
        intent["status"] = "awaiting_reconcile"
        intent["last_error"] = str(result)
        persist_lifecycle_intent(cache)
        return True
    fill_price = decimal_or_none(filled.get("avgPx")) or ctx["current_mid"]
    fill_size = decimal_or_none(filled.get("totalSz", filled.get("sz"))) or decimal_or_none(market.get("size"))
    intent["market_oid"] = filled.get("oid")
    lifecycle_materialize_birth_intent(
        row, ctx, cache, intent, fill_price, fill_size or Decimal("0")
    )
    row["limit_chase_status"] = str(row.get("limit_chase_birth_status") or "chain_debt")
    row["limit_chase_fill_price"] = decimal_to_plain(fill_price)
    row["limit_chase_at"] = ctx["now"]
    return True


def maintain_grid_p8(row: dict[str, Any], cache: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Run the local-only P8 accumulator without refreshing exchange context."""
    now = int(cache.setdefault("now", int(time.time())))
    changed = migrate_grid_lifecycle(row, now)
    changed = initialize_lifecycle_iterations(row) or changed
    migrated = lifecycle_migrate_min_rejected_debts_to_p8(row, now)
    changed = bool(migrated) or changed
    oversized_migrated = lifecycle_migrate_oversized_p8_debts(row, now, cache)
    changed = bool(oversized_migrated) or changed
    promoted, p8_changed = lifecycle_process_p8(row, now, cache)
    changed = changed or p8_changed
    counters = cache.setdefault("grid_lifecycle_counters", {}).setdefault(id(row), {})
    counters["p8_promoted"] = int(counters.get("p8_promoted") or 0) + promoted
    levels = row.setdefault("levels", [])
    row["open_oids"] = sorted(grid_batch_open_oids(row))
    if row.get("status") != "active" or "error" in row:
        changed = True
    row["status"] = "active"
    row.pop("error", None)
    row["updated_at"] = now
    row["note"] = (
        "grid lifecycle v2; phase=p8; "
        f"chain_debt={sum(1 for entry in levels if isinstance(entry, dict) and str(entry.get('status') or '') == GRID_CHAIN_DEBT_STATUS)}; "
        f"p8={row.get('p8_partial_debt_count', 0)}; "
        f"p8_value={row.get('p8_partial_debt_value', '0')}; "
        f"raw_deficit={raw_action_limit_deficit(cache)}"
    )
    return row, changed


def maintain_grid(row: dict[str, Any], cache: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
    cache = cache if cache is not None else {}
    phase = str(cache.get("grid_action_phase") or GRID_LIFECYCLE_PHASE_P0)
    if phase == GRID_LIFECYCLE_PHASE_P8:
        return maintain_grid_p8(row, cache)
    ctx = lifecycle_context(row, cache)
    # P6 compares legacy prices across markets, so every row needs the live
    # midpoint gathered for this worker run before the account-wide scan.
    if ctx["current_mid"] is not None:
        row["lifecycle_mid"] = decimal_to_plain(ctx["current_mid"])
    changed = migrate_grid_lifecycle(row, ctx["now"])
    invalidated = invalidate_unqueued_legacy_pauses(row, ctx["now"])
    if invalidated:
        changed = True
    changed = initialize_lifecycle_iterations(row) or changed
    migrated = lifecycle_migrate_min_rejected_debts_to_p8(row, ctx["now"])
    changed = bool(migrated) or changed
    oversized_migrated = lifecycle_migrate_oversized_p8_debts(row, ctx["now"], cache)
    changed = bool(oversized_migrated) or changed
    levels = row.setdefault("levels", [])
    counters = cache.setdefault("grid_lifecycle_counters", {}).setdefault(id(row), {})

    if phase == GRID_LIFECYCLE_PHASE_P0:
        changed = lifecycle_reconcile_birth_intents(row, ctx, cache) or changed
        ratio = grid_panic_ratio(row, ctx["position_size"], ctx["current_mid"], ctx["liquidation_px"])
        threshold = grid_panic_ratio_threshold(row)
        row["panic_ratio"] = decimal_to_plain(ratio) if ratio is not None else None
        row["panic_ratio_threshold"] = decimal_to_plain(threshold)
        if ratio is not None and ratio < threshold and not lifecycle_has_birth_intent(row, "panic"):
            market = build_grid_panic_reduce_order(
                ctx["exchange"], row, ctx["coin"], ctx["asset"], ctx["current_mid"], ctx["position_size"]
            )
            if market is not None:
                intent = lifecycle_create_birth_intent(row, market, "panic", ctx["now"], cache)
                account_key = lifecycle_row_account_key(row, ctx["network"], ctx["account"])
                p0_attempt_key = (*account_key, ctx["coin"])
                # Creating the persisted intent commits this worker round to P0
                # for the account/coin. Block later P4/P10 starts even when the
                # exchange result is rejected, times out, or needs reconciliation.
                cache.setdefault("lifecycle_p0_attempted_coins", set()).add(p0_attempt_key)
                counters["p0_attempted"] = int(counters.get("p0_attempted") or 0) + 1
                try:
                    submitted = submit_grid_panic_reduce(
                        ctx["exchange"], ctx["coin"], market, ctx["now"], row, cache,
                        cloid=Cloid.from_str(str(intent["cloid"])),
                    )
                except Exception as exc:
                    intent["status"] = "awaiting_reconcile"
                    intent["last_error"] = str(exc)
                    intent["last_checked_at"] = ctx["now"]
                    persist_lifecycle_intent(cache)
                    submitted = False
                if submitted and str(market.get("status") or "") == "filled":
                    intent["market_oid"] = market.get("oid")
                    fill_price = decimal_or_none(market.get("filled_avg_px")) or ctx["current_mid"]
                    fill_size = decimal_or_none(market.get("filled_size")) or decimal_or_none(market.get("size"))
                    if lifecycle_materialize_birth_intent(
                        row, ctx, cache, intent, fill_price, fill_size or Decimal("0")
                    ):
                        counters["p0_births"] = int(counters.get("p0_births") or 0) + 1
                else:
                    intent["status"] = "awaiting_reconcile"
                    persist_lifecycle_intent(cache)
                changed = True

    elif phase == GRID_LIFECYCLE_PHASE_P1:
        account_key = lifecycle_row_account_key(row, ctx["network"], ctx["account"])
        attempted = cache.setdefault("lifecycle_p1_accounts", set())
        if ctx["withdrawable"] is not None and ctx["withdrawable"] < GRID_WITHDRAWABLE_PAUSE_THRESHOLD and account_key not in attempted:
            candidate = lifecycle_terminal_candidate(
                cache.get("grid_rows") or [row], ctx["network"], ctx["account"]
            )
            if candidate is not None and candidate[0] is row:
                attempted.add(account_key)
                candidate_row, entry = candidate
                candidate_coin = str(candidate_row.get("coin") or ctx["coin"])
                if lifecycle_cancel_terminal_entry(
                    ctx["exchange"], candidate_coin, candidate_row, entry, ctx["now"], cache
                ):
                    counters["p1_terminated"] = int(counters.get("p1_terminated") or 0) + 1
                    changed = True

    elif phase == GRID_LIFECYCLE_PHASE_P2:
        count, phase_changed = lifecycle_process_fills(row, ctx, cache)
        counters["p2_replacements"] = int(counters.get("p2_replacements") or 0) + count
        changed = changed or phase_changed

    elif phase == GRID_LIFECYCLE_PHASE_P3:
        if "lifecycle_p3_restore_budget" not in cache:
            cache["lifecycle_p3_restore_budget"] = lifecycle_p3_restore_budget(ctx["withdrawable"])
        restore_budget = int(cache.get("lifecycle_p3_restore_budget") or 0)
        restored_attempts = int(cache.get("lifecycle_p3_restore_attempts") or 0)
        if raw_action_limit_deficit(cache) < 0 and restored_attempts < restore_budget:
            isolated_ready = cache.setdefault("lifecycle_isolated_ready", set())
            target = cache.get("lifecycle_p3_target")
            pending_entries = [target] if isinstance(target, dict) else list(levels)
            for entry in pending_entries:
                if int(cache.get("lifecycle_p3_restore_attempts") or 0) >= restore_budget:
                    break
                if (
                    not isinstance(entry, dict)
                    or not any(candidate is entry for candidate in levels)
                    or str(entry.get("status") or "") not in {GRID_MARGIN_STATUS, GRID_CHAIN_DEBT_STATUS}
                ):
                    continue
                cache["lifecycle_p3_restore_attempts"] = int(
                    cache.get("lifecycle_p3_restore_attempts") or 0
                ) + 1
                result = lifecycle_submit_order(
                    ctx["exchange"], ctx["coin"], entry, ctx["now"], row, ctx["asset"], ctx["position_size"],
                    ctx["current_mid"], ctx["best_bid"], ctx["best_ask"], isolated_ready,
                    ctx["open_orders"], cache, search_outward=True,
                    allow_non_reduce_only_fallback=True,
                )
                changed = True
                if result == "submitted":
                    counters["p3_restored"] = int(counters.get("p3_restored") or 0) + 1
                elif result == GRID_P8_REACCUMULATE_STATUS:
                    size = decimal_or_none(entry.get("size")) or Decimal("0")
                    price = decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0")
                    if lifecycle_accumulate_p8_partial_debt(
                        row, entry, size, price, ctx["now"]
                    ):
                        if entry in levels:
                            levels.remove(entry)
                        counters["p3_returned_to_p8"] = int(
                            counters.get("p3_returned_to_p8") or 0
                        ) + 1
                elif (
                    result in {GRID_MARGIN_STATUS, GRID_CHAIN_DEBT_STATUS}
                    and lifecycle_p3_failure_is_unknown(entry)
                    and entry in levels
                ):
                    # Known retryable failures keep their queue position. Only
                    # unknown/unresolvable failures move behind other debts.
                    levels.remove(entry)
                    levels.append(entry)
                if raw_action_limit_deficit(cache) >= 0:
                    break

    elif phase == GRID_LIFECYCLE_PHASE_P4:
        if raw_action_limit_deficit(cache) < 0:
            account_key = lifecycle_row_account_key(row, ctx["network"], ctx["account"])
            attempt_key = (*account_key, ctx["coin"])
            if attempt_key in cache.setdefault("lifecycle_p0_attempted_coins", set()):
                audited = cache.setdefault("lifecycle_p4_after_p0_audited", set())
                if attempt_key not in audited:
                    audited.add(attempt_key)
                    audit_grid_action(
                        "grid_p4_after_p0_skipped",
                        network=attempt_key[0],
                        account=attempt_key[1],
                        coin=attempt_key[2],
                    )
            elif lifecycle_submit_limit_chase(row, ctx, cache):
                counters["p4_births"] = int(counters.get("p4_births") or 0) + 1
                changed = True

    elif phase == GRID_LIFECYCLE_PHASE_P5:
        if raw_action_limit_deficit(cache) < -100:
            count, phase_changed = lifecycle_process_anomalies(row, ctx, cache)
            counters["p5_enqueued"] = int(counters.get("p5_enqueued") or 0) + count
            changed = changed or phase_changed

    elif phase == GRID_LIFECYCLE_PHASE_P6:
        account_key = lifecycle_row_account_key(row, ctx["network"], ctx["account"])
        claims = cache.setdefault("lifecycle_p6_claims", {})
        if account_key not in claims:
            claims[account_key] = lifecycle_legacy_pause_candidate(
                cache.get("grid_rows") or [row], ctx["network"], ctx["account"]
            )
        candidate = claims.get(account_key)
        attempted = cache.setdefault("lifecycle_p6_accounts", set())
        if (
            candidate is not None
            and candidate[0] is row
            and account_key not in attempted
            and ctx["withdrawable"] is not None
            and ctx["withdrawable"] > GRID_P6_WITHDRAWABLE_THRESHOLD
        ):
            attempted.add(account_key)
            _candidate_row, entry = candidate
            entry["status"] = GRID_MARGIN_STATUS
            entry["grid_leg"] = 1
            entry["p6_legacy_pause"] = True
            lifecycle_assign_p3_queue_seq(entry, cache)
            entry.pop("legacy_pause_status", None)
            if entry in levels:
                levels.remove(entry)
                levels.append(entry)
            counters["p6_enqueued"] = int(counters.get("p6_enqueued") or 0) + 1
            changed = True

    elif phase == GRID_LIFECYCLE_PHASE_P7:
        p7_changed, p7_orders = lifecycle_process_p7(row, ctx, cache)
        counters["p7_restructured"] = int(counters.get("p7_restructured") or 0) + p7_orders
        changed = changed or p7_changed

    elif phase == GRID_LIFECYCLE_PHASE_P9:
        account_key = lifecycle_row_account_key(row, ctx["network"], ctx["account"])
        claims = cache.setdefault("lifecycle_p9_claims", {})
        if account_key not in claims:
            claims[account_key] = lifecycle_p9_candidate(
                cache.get("grid_rows") or [row], ctx["network"], account_key[1]
            )
        candidate = claims.get(account_key)
        attempted = cache.setdefault("lifecycle_p9_accounts", set())
        row["p9_withdrawable"] = (
            decimal_to_plain(ctx["withdrawable"]) if ctx["withdrawable"] is not None else None
        )
        if (
            candidate is not None
            and candidate[0] is row
            and account_key not in attempted
        ):
            attempted.add(account_key)
            _candidate_row, entry, score, estimated_margin, deviation_rate = candidate
            if (
                ctx["withdrawable"] is not None
                and ctx["withdrawable"] < GRID_P9_WITHDRAWABLE_THRESHOLD
                and int(entry["oid"]) in ctx["open_oids"]
                and lifecycle_cancel_p9_candidate(
                    ctx["exchange"], ctx["coin"], row, entry, score,
                    estimated_margin, deviation_rate, ctx["now"], cache,
                )
            ):
                counters["p9_enqueued"] = int(counters.get("p9_enqueued") or 0) + 1
                changed = True

    elif phase == GRID_LIFECYCLE_PHASE_P10:
        account_key = lifecycle_row_account_key(row, ctx["network"], ctx["account"])
        attempt_key = (*account_key, ctx["coin"])
        attempted = cache.setdefault("lifecycle_p10_coins", set())
        existing_intent = lifecycle_p10_intent(row)
        # Reconcile every persisted intent, but start at most one new P10
        # promotion per account/coin in this worker round. A P0 attempt blocks
        # only a new promotion; persisted P10 intents must still be reconciled.
        p0_attempted = attempt_key in cache.setdefault("lifecycle_p0_attempted_coins", set())
        if existing_intent is None and p0_attempted:
            audited = cache.setdefault("lifecycle_p10_after_p0_audited", set())
            if attempt_key not in audited:
                audited.add(attempt_key)
                audit_grid_action(
                    "grid_p10_after_p0_skipped",
                    network=attempt_key[0],
                    account=attempt_key[1],
                    coin=attempt_key[2],
                )
        elif existing_intent is not None or attempt_key not in attempted:
            p10_changed = lifecycle_process_p10(row, ctx, cache)
            if p10_changed:
                attempted.add(attempt_key)
                counters["p10_promotions"] = int(counters.get("p10_promotions") or 0) + 1
            changed = changed or p10_changed

    row["legacy_pause_remaining"] = sum(
        1 for entry in levels if isinstance(entry, dict) and str(entry.get("status") or "") == GRID_LEGACY_PAUSE_STATUS
    )
    row["open_oids"] = sorted(grid_batch_open_oids(row))
    if row.get("status") != "active" or "error" in row:
        changed = True
    row["status"] = "active"
    row.pop("error", None)
    row["updated_at"] = ctx["now"]
    if phase in {
        GRID_LIFECYCLE_PHASE_P0,
        GRID_LIFECYCLE_PHASE_P2,
        GRID_LIFECYCLE_PHASE_P5,
        GRID_LIFECYCLE_PHASE_P10,
    }:
        row["last_fill_check_ms"] = ctx["now_ms"]
    row["note"] = (
        "grid lifecycle v2; "
        f"phase={phase}; leg0={sum(1 for e in levels if isinstance(e, dict) and lifecycle_leg(e) == 0 and str(e.get('status') or '') == 'active')}; "
        f"leg1={sum(1 for e in levels if isinstance(e, dict) and lifecycle_leg(e) == 1 and str(e.get('status') or '') == 'active')}; "
        f"margin={sum(1 for e in levels if isinstance(e, dict) and str(e.get('status') or '') == GRID_MARGIN_STATUS)}; "
        f"chain_debt={sum(1 for e in levels if isinstance(e, dict) and str(e.get('status') or '') == GRID_CHAIN_DEBT_STATUS)}; "
        f"p8={row.get('p8_partial_debt_count', 0)}; "
        f"p8_value={row.get('p8_partial_debt_value', '0')}; "
        f"birth_intents={len(row.get('birth_market_intents') or [])}; "
        f"legacy_pause={row['legacy_pause_remaining']}; raw_deficit={raw_action_limit_deficit(cache)}"
    )
    return row, changed


def run_once() -> None:
    rows = load_server_batch()
    rows, cancelled_grids_pruned = prune_cancelled_grid_rows(rows)
    cancelled_levels_pruned = prune_grid_level_history(rows)
    if cancelled_grids_pruned or cancelled_levels_pruned:
        save_server_batch(rows)
    lifecycle_prepare_economic_chain_ids(rows)
    active_trail_indexes = [
        index
        for index, row in enumerate(rows)
        if row.get("type") == "trail" and row.get("status") == "active"
    ]
    active_grid_indexes = [
        index
        for index, row in enumerate(rows)
        if grid_row_recoverable_from_error(row)
    ]
    random.shuffle(active_grid_indexes)
    if not active_trail_indexes and not active_grid_indexes:
        print("trail_worker: no active trail/grid orders")
        return

    mids_cache: dict[tuple[str, str], dict[str, Any]] = {}
    grid_cache: dict[str, Any] = {
        "grid_rows": rows,
        "run_started_at": int(time.time()),
        "run_monotonic_started_at": time.monotonic(),
        "p10_chain_realized_surplus": load_economic_chain_realized_surplus_map(),
    }
    changed = lifecycle_initialize_p3_queue(rows, active_grid_indexes)
    chain_backfilled, chain_backfilled_by_status, chain_backfilled_by_coin = (
        lifecycle_backfill_economic_chain_ids(
            rows,
            active_grid_indexes,
            int(grid_cache["run_started_at"]),
        )
    )
    if chain_backfilled:
        save_server_batch(rows)
        audit_grid_action(
            "grid_economic_chain_id_backfill",
            count=chain_backfilled,
            by_status=chain_backfilled_by_status,
            by_coin=chain_backfilled_by_coin,
        )
        changed = False
    for index in active_trail_indexes:
        row = rows[index]
        try:
            grid_cache["api_stat_phase"] = "trail"
            grid_cache["api_stat_context"] = str(row.get("coin") or "-")
            network = str(row.get("network") or "mainnet")
            raw_coin = batch_row_raw_coin(row)
            dex = str(row.get("dex") or "")
            cache_key = (network, dex)
            if cache_key not in mids_cache:
                info, _exchange, account, _signer, _role = build_worker_clients(
                    grid_cache,
                    network,
                    float(row.get("timeout") or 20),
                    raw_coin,
                    need_exchange=False,
                )
                mids_cache[cache_key] = info.all_mids(dex)
                print(f"trail_worker: mids loaded {network}:{dex or 'default'} account={mask(account)}")
            mid_px = Decimal(str(mids_cache[cache_key][row["coin"]]))
            rows[index], row_changed = modify_trail_stop(row, mid_px, grid_cache)
            changed = changed or row_changed
            changed = save_worker_progress(rows, changed)
        except Exception as exc:
            transient_status = transient_error_status(exc)
            if transient_status is None:
                row["status"] = "error"
                row["error"] = str(exc)
            else:
                row["status"] = "active"
                row["last_error"] = str(exc)
                row["note"] = transient_note(transient_status)
                append_rate_limit_log(row, transient_status, exc)
            row["updated_at"] = int(time.time())
            rows[index] = row
            changed = True
            changed = save_worker_progress(rows, changed)

    def process_grid_index(index: int, label: str) -> None:
        nonlocal changed
        row = rows[index]
        try:
            grid_cache["api_stat_context"] = str(row.get("coin") or "-")
            started_at = time.monotonic()
            rows[index], row_changed = maintain_grid(row, grid_cache)
            changed = changed or row_changed
            elapsed = time.monotonic() - started_at
            print(
                f"trail_worker: {label} {row.get('network', 'mainnet')}:{row.get('coin')} "
                f"open={len(grid_batch_open_oids(rows[index]))} elapsed={elapsed:.2f}s"
            )
            changed = save_worker_progress(rows, changed)
        except Exception as exc:
            transient_status = transient_error_status(exc)
            if transient_status is None:
                row["status"] = "error"
                row["error"] = str(exc)
            else:
                row["status"] = "active"
                row["last_error"] = str(exc)
                row["note"] = transient_note(transient_status)
                append_rate_limit_log(row, transient_status, exc)
            row["updated_at"] = int(time.time())
            rows[index] = row
            changed = True
            changed = save_worker_progress(rows, changed)

    for phase in GRID_LIFECYCLE_PHASES:
        grid_cache["grid_action_phase"] = phase
        if phase == GRID_LIFECYCLE_PHASE_P3:
            pending_pool = lifecycle_p3_pending_pool(rows, sorted(active_grid_indexes))
            grid_cache["lifecycle_p3_pending_pool"] = pending_pool
            for index, entry in pending_pool:
                if (
                    "lifecycle_p3_restore_budget" in grid_cache
                    and int(grid_cache.get("lifecycle_p3_restore_attempts") or 0)
                    >= int(grid_cache.get("lifecycle_p3_restore_budget") or 0)
                ):
                    break
                if int(grid_cache.get("lifecycle_p3_processed") or 0) >= GRID_P3_MAX_RESTORES_PER_RUN:
                    break
                grid_cache["lifecycle_p3_target"] = entry
                grid_cache["lifecycle_p3_processed"] = int(grid_cache.get("lifecycle_p3_processed") or 0) + 1
                process_grid_index(index, f"grid {phase}")
            grid_cache.pop("lifecycle_p3_target", None)
            grid_cache.pop("lifecycle_p3_pending_pool", None)
        else:
            indexes = (
                lifecycle_p7_priority_indexes(rows, active_grid_indexes)
                if phase == GRID_LIFECYCLE_PHASE_P7
                else active_grid_indexes
            )
            for index in indexes:
                process_grid_index(index, f"grid {phase}")
        if phase != GRID_LIFECYCLE_PHASE_P8:
            reconcile_cached_grid_open_orders(rows, active_grid_indexes, grid_cache)
        if phase != GRID_LIFECYCLE_PHASE_P10:
            for info, _exchange, _account, _signer, _role in grid_cache.get("clients", {}).values():
                clear_info_cache(info)
            # Recompute position, withdrawable and market context before every
            # following phase. Clearing after local-only P8 forces P9 to read
            # withdrawable again, and clearing after P9 gives P10 a fresh
            # position, order, and margin snapshot before any promotion.
            cache_names = ["books", "user_states", "spot_user_states", "fills"]
            # P6 only ranks legacy pauses and does not submit an order. Reuse
            # the P4 mid snapshot across local/anomaly-only P5, then clear it
            # after P6 so every later action phase still gets a fresh price.
            if phase not in {GRID_LIFECYCLE_PHASE_P4, GRID_LIFECYCLE_PHASE_P5}:
                cache_names.append("mids")
            for cache_name in cache_names:
                grid_cache.pop(cache_name, None)
            # Our own open-order delta is reconciled after every exchange phase;
            # retaining it avoids losing a just-submitted child before the
            # exchange view catches up.
    grid_cache.pop("grid_action_phase", None)

    rows, pruned = prune_done_rows(rows)
    grid_history_pruned = prune_grid_level_history(rows)
    if changed or pruned or grid_history_pruned:
        save_server_batch(rows)
    emit_worker_api_stats(grid_cache)


def main() -> None:
    SERVER_BATCH_PATH.touch(exist_ok=True)
    with server_batch_lock(blocking=False) as acquired:
        if not acquired:
            print("trail_worker: previous run still active, skipping")
            return
        run_once()


if __name__ == "__main__":
    main()
