#!/usr/bin/env python3
"""One-shot server worker for batched trailing stop maintenance."""

from __future__ import annotations

import json
import math
import time
from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from simple_hyper.runtime import ensure_local_venv


ensure_local_venv(__file__)

from hl_order import (  # noqa: E402
    DEFAULT_SLIPPAGE,
    GRID_ACCOUNT_MARGIN_RATIO_THRESHOLD,
    GRID_TARGET_ORDERS_PER_SIDE,
    SERVER_BATCH_PATH,
    asset_requires_isolated_margin,
    build_clients,
    build_grid_limit_order_plan,
    build_trigger_order_plan,
    collect_frontend_open_orders,
    decimal_or_none,
    decimal_to_plain,
    fill_matches_coin,
    format_signed_percent,
    grid_avg_multiplier,
    grid_avg_topup_params,
    grid_batch_open_oids,
    grid_limit_policy_from_row,
    grid_order_allowed_by_max,
    grid_order_should_reduce_only,
    grid_order_would_add_risk,
    grid_size_for_min_notional,
    is_post_only_reject_text,
    load_server_batch,
    log_event,
    mask,
    position_matches_coin,
    resolve_perp_asset,
    rounded_perp_price,
    save_server_batch,
    server_batch_lock,
    trail_stop_price,
    update_isolated_opening_leverage,
)
from simple_hyper.order_specs import MIN_NOTIONAL


RATE_LIMIT_LOG_PATH = Path(__file__).resolve().parent / "logs" / "trail-rate-limit.jsonl"
DONE_RETENTION_DAYS = 7
DONE_RETENTION_MAX = 500
GRID_LEVEL_HISTORY_MAX = 120
GRID_FILL_LOOKBACK_SECONDS = 24 * 60 * 60
GRID_ACCOUNT_MARGIN_RATIO_SOFT_THRESHOLD = Decimal("0.90")
GRID_MAX_SUBMISSIONS_PER_SIDE_PER_RUN = 1
GRID_ADD_RISK_BRAKE_STREAK = 2
GRID_ADD_RISK_BRAKE_PAIR_RETENTION_SECONDS = 24 * 60 * 60
GRID_ADD_RISK_BRAKE_HISTORY_RETENTION_SECONDS = 7 * 24 * 60 * 60
GRID_UNKNOWN_OID_RECOVERY_MAX_AGE_SECONDS = 30 * 60
GRID_ALO_PRICE_ATTEMPT_LIMIT = 20
GRID_ALO_SPACING_MULTIPLIER = Decimal("0.95")
GRID_NEAR_REGRID_STALE_GAP_MULTIPLE = Decimal("30")
GRID_NEAR_REGRID_TARGET_GAP_MULTIPLE = Decimal("15")
GRID_PANIC_RATIO_THRESHOLD = Decimal("30")
GRID_PANIC_RATIO_LEGACY_DEFAULT_THRESHOLD = Decimal("10")
GRID_REPLACEMENT_PAUSE_STATUS = "paused_replacement"
GRID_RISK_DENSITY_PAUSE_STATUS = "paused_risk_density"
GRID_ACTIVE_CAP_PAUSE_STATUS = "paused_active_cap"
GRID_MAX_LEVELS_PER_SIDE = 1024
GRID_MAX_ACTIVE_ORDERS_PER_SIDE = 32
GRID_PAUSED_STATUSES = {
    "paused_max",
    "paused_limit",
    "paused_margin",
    "paused_reduce_capacity",
    GRID_REPLACEMENT_PAUSE_STATUS,
    GRID_RISK_DENSITY_PAUSE_STATUS,
    GRID_ACTIVE_CAP_PAUSE_STATUS,
}
GRID_PRICE_OCCUPANCY_STATUSES = {
    "active",
    "pending",
    "recovery_deferred",
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


def grid_row_recoverable_from_error(row: dict[str, Any]) -> bool:
    if row.get("type") != "grid":
        return False
    if row.get("status") == "active":
        return True
    if row.get("status") != "error":
        return False
    error_text = " ".join(str(row.get(key, "")) for key in ("error", "last_error", "note"))
    if (
        is_post_only_reject_text(error_text)
        or is_transient_error_text(error_text)
        or is_min_order_value_error_text(error_text)
        or is_reduce_only_would_increase_text(error_text)
        or is_insufficient_margin_text(error_text)
    ):
        return True
    for entry in row.get("levels") or []:
        if isinstance(entry, dict) and is_post_only_reject_text(str(entry.get("error", ""))):
            return True
    return False


def is_min_order_value_error_text(text: str) -> bool:
    lowered = text.lower()
    return "minimum value" in lowered or "min value" in lowered


def is_reduce_only_would_increase_text(text: str) -> bool:
    lowered = text.lower()
    return "reduce only" in lowered and "increase position" in lowered


def is_insufficient_margin_text(text: str) -> bool:
    return "insufficient margin" in text.lower()


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


def account_margin_ratio(
    info: Any,
    account: str,
    network: str,
    cache: dict[str, Any],
) -> Decimal | None:
    ratio_cache = cache.setdefault("account_margin_ratios", {})
    cache_key = (network, account)
    if cache_key in ratio_cache:
        return ratio_cache[cache_key]

    spot_state = info.spot_user_state(account)
    total = None
    for balance in spot_state.get("balances", []):
        if not isinstance(balance, dict):
            continue
        if balance.get("token") == 0 or str(balance.get("coin", "")).upper() == "USDC":
            total = decimal_or_none(balance.get("total"))
            break

    available = None
    for item in spot_state.get("tokenToAvailableAfterMaintenance", []):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            token = int(item[0])
        except (TypeError, ValueError):
            continue
        if token == 0:
            available = decimal_or_none(item[1])
            break

    if total is None or total <= 0:
        ratio = Decimal("0")
    elif available is None:
        ratio = None
    else:
        ratio = max(Decimal("0"), available) / total
    ratio_cache[cache_key] = ratio
    return ratio


def grid_reduce_only_capacity_available(
    row: dict[str, Any],
    order: dict[str, Any],
    position_size: Decimal,
    position_value: Decimal,
) -> bool:
    if not bool(order.get("reduce_only", False)):
        return True
    requested_size = decimal_or_none(order.get("size")) or Decimal("0")
    if requested_size <= 0 or position_size == 0:
        return False
    reserved_size = Decimal("0")
    reserved_notional = Decimal("0")
    for entry in active_grid_entries(row, str(order.get("side"))):
        if entry is order or not bool(entry.get("reduce_only", False)):
            continue
        entry_size = decimal_or_none(entry.get("size")) or Decimal("0")
        entry_price = decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0")
        reserved_size += entry_size
        reserved_notional += entry_size * entry_price
    if reserved_size + requested_size > abs(position_size):
        return False
    policy = grid_limit_policy_from_row(row)
    position_matches_target = (policy == "long" and position_size > 0) or (policy == "short" and position_size < 0)
    min_position_value = Decimal(str(row.get("min_position_value") or "0")) if position_matches_target else Decimal("0")
    requested_price = decimal_or_none(order.get("price", order.get("limit_px"))) or Decimal("0")
    return position_value - reserved_notional - requested_size * requested_price >= min_position_value


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


def modify_trail_stop(row: dict[str, Any], mid_px: Decimal) -> tuple[dict[str, Any], bool]:
    info, exchange, account, _signer, _role = build_clients(
        str(row.get("network") or "mainnet"),
        float(row.get("timeout") or 20),
        str(row.get("raw_coin") or row["coin"]),
    )
    coin, asset = resolve_perp_asset(info, str(row.get("raw_coin") or row["coin"]))
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
        row["status"] = "error"
        row["error"] = str(result)
        row["updated_at"] = int(time.time())
        return row, True

    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    if any("error" in status for status in statuses):
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


def submit_grid_child_order(exchange: Any, coin: str, order: dict[str, Any]) -> tuple[int | None, str, dict[str, Any] | None]:
    plan = order.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("grid child order is missing its saved plan")
    result = exchange.order(
        coin,
        bool(plan["is_buy"]),
        float(plan["size"]),
        float(plan["limit_px"]),
        plan["order_type"],
        reduce_only=bool(plan.get("reduce_only", False)),
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


def ensure_grid_order_min_notional(row: dict[str, Any], asset: dict[str, Any], order: dict[str, Any]) -> None:
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


def pause_refreshed_reduce_only_entries(entries: list[dict[str, Any]], now: int) -> int:
    paused = 0
    for entry in entries:
        if str(entry.get("status")) != "refresh_reduce_only" or entry.get("cancelled_at") != now:
            continue
        entry["replacement_order"] = True
        if pause_refresh_reduce_only_replacement(entry, now):
            paused += 1
    return paused


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
    set_grid_order_tif(order, "Alo")


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
    for _attempt in range(max_attempts):
        price = decimal_or_none(order.get("price", order.get("limit_px")))
        if price is None or price <= 0:
            return False
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
) -> dict[str, Any]:
    ensure_grid_base_sizes(row)
    size_key = "base_buy_size" if is_buy else "base_sell_size"
    if size is None:
        size = Decimal(str(row.get(size_key) or row.get("buy_size" if is_buy else "sell_size") or "0"))
    if size <= 0:
        raise ValueError(f"grid row is missing {size_key}")
    min_notional = Decimal(str(row.get("min_order_value") or "10"))
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
        and str(entry.get("status", "active")) == "active"
        and (side is None or str(entry.get("side")) == side)
    ]
    return entries


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
    if threshold == GRID_PANIC_RATIO_LEGACY_DEFAULT_THRESHOLD:
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
        cancel_request = {"coin": coin, "oid": old_oid}
        cancel_result = exchange.bulk_cancel([cancel_request])
        log_event(
            "grid_dense_regrid_cancel",
            {
                "coin": coin,
                "old_oid": old_oid,
                "old_price": decimal_to_plain(old_price) if old_price is not None else None,
                "new_price": decimal_to_plain(new_price),
                "result": cancel_result,
            },
        )
        if cancel_result.get("status") != "ok":
            entry.clear()
            entry.update(old_snapshot)
            raise RuntimeError(f"Failed to cancel dense grid order before regrid: {cancel_result}")
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
        )
        if submitted:
            regridded += 1
    return regridded


def grid_entry_near_to_far_key(entry: dict[str, Any], side: str) -> Decimal:
    price = decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0")
    return -price if side == "buy" else price


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
        cancel_grid_entries(exchange, coin, active_to_cancel, now, "cleared_side_cap")
    clear_ids = {id(entry) for entry in to_clear}
    row["levels"] = [entry for entry in levels if id(entry) not in clear_ids]
    row["side_cap_cleared_at"] = now
    row["side_cap_cleared_count"] = int(row.get("side_cap_cleared_count") or 0) + len(to_clear)
    return len(to_clear)


def logarithmic_keep_indexes(count: int, keep_count: int) -> set[int]:
    if keep_count <= 0:
        return set()
    if keep_count >= count:
        return set(range(count))
    if keep_count == 1:
        return {0}
    log_count = Decimal(str(math.log(count)))
    keep: set[int] = set()
    for index in range(keep_count):
        exponent = log_count * Decimal(index) / Decimal(keep_count - 1)
        raw = Decimal(str(math.exp(float(exponent)))) - Decimal("1")
        keep.add(max(0, min(count - 1, int(raw.to_integral_value(rounding=ROUND_HALF_UP)))))
    if len(keep) < keep_count:
        for index in range(count):
            keep.add(index)
            if len(keep) >= keep_count:
                break
    return keep


def grid_margin_gap_multiplier(margin_ratio: Decimal | None) -> Decimal:
    if (
        margin_ratio is None
        or margin_ratio >= GRID_ACCOUNT_MARGIN_RATIO_SOFT_THRESHOLD
        or margin_ratio <= GRID_ACCOUNT_MARGIN_RATIO_THRESHOLD
    ):
        return Decimal("1")
    window = GRID_ACCOUNT_MARGIN_RATIO_SOFT_THRESHOLD - GRID_ACCOUNT_MARGIN_RATIO_THRESHOLD
    distance_to_hard_stop = margin_ratio - GRID_ACCOUNT_MARGIN_RATIO_THRESHOLD
    return Decimal("1") + Decimal(str(math.log(float(window / distance_to_hard_stop))))


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
    keep_indexes = logarithmic_keep_indexes(len(active_add_risk), allowed)
    to_pause = [
        entry
        for index, entry in enumerate(active_add_risk)
        if index not in keep_indexes and not bool(entry.get("replacement_order"))
    ]
    return to_pause, allowed, multiplier


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
    replacements = [entry for entry in entries if bool(entry.get("replacement_order"))]
    regular = [entry for entry in entries if not bool(entry.get("replacement_order"))]
    replacements.sort(key=lambda entry: grid_entry_near_to_far_key(entry, side))
    regular.sort(key=lambda entry: grid_entry_near_to_far_key(entry, side))
    if len(replacements) >= max_active_per_side:
        keep_indexes = logarithmic_keep_indexes(len(replacements), max_active_per_side)
        return {id(entry) for index, entry in enumerate(replacements) if index in keep_indexes}
    regular_keep_count = max_active_per_side - len(replacements)
    keep_indexes = logarithmic_keep_indexes(len(regular), regular_keep_count)
    keep_ids = {id(entry) for entry in replacements}
    keep_ids.update(id(entry) for index, entry in enumerate(regular) if index in keep_indexes)
    return keep_ids


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
    keep_indexes = logarithmic_keep_indexes(len(combined), allowed)
    keep_ids = {id(item) for index, item in enumerate(combined) if index in keep_indexes}
    return id(entry) in keep_ids


def grid_active_cap_restore_allowed(
    row: dict[str, Any],
    entry: dict[str, Any],
    side: str,
    max_active_per_side: int = GRID_MAX_ACTIVE_ORDERS_PER_SIDE,
) -> bool:
    active = active_grid_entries(row, side)
    if len(active) >= max_active_per_side:
        return False
    paused = [
        paused_entry
        for paused_entry in row.get("levels") or []
        if isinstance(paused_entry, dict)
        and str(paused_entry.get("side") or "") == side
        and str(paused_entry.get("status")) == GRID_ACTIVE_CAP_PAUSE_STATUS
    ]
    keep_ids = grid_active_cap_keep_ids(active + paused, side, max_active_per_side)
    return id(entry) in keep_ids


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
    if adds_risk:
        gap *= Decimal(str(row.get("margin_gap_multiplier") or "1"))
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
        next_px = rounded_perp_price(base_px * multiplier, sz_decimals)
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
) -> bool:
    restore_without_reduce_only = grid_reduce_only_canceled_restore_without_reduce_only(order)
    refresh_grid_order_reduce_only(order, position_size, policy)
    refresh_grid_order_tif(order)
    side = str(order.get("side") or "")
    margin_side_key = (coin, side)
    if side and margin_blocked_sides is not None and margin_side_key in margin_blocked_sides:
        error_text = "same-run insufficient margin pause"
        order["status"] = "paused_margin"
        order["oid"] = None
        order["last_error"] = error_text
        order["paused_at"] = now
        pause_grid_margin_side_entries(row, side, now, error_text)
        return False
    if account_margin_protected:
        if grid_order_would_add_risk(position_size, bool(order.get("is_buy"))):
            if bool(order.get("replacement_order")):
                order["status"] = "paused_account_margin"
                order["oid"] = None
                order["paused_at"] = now
                return False
            # Account protection skips this price entirely. Once protection clears,
            # the regular top-up pass builds a fresh level from the live market.
            order["status"] = "skipped_account_margin"
            order["oid"] = None
            order["skipped_at"] = now
            return False
        set_grid_order_reduce_only(order, True)
    elif restore_without_reduce_only:
        set_grid_order_reduce_only(order, False)
        order["reduce_only_canceled_restore_without_reduce_only_at"] = now
    if not grid_reduce_only_capacity_available(row, order, position_size, position_value):
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
    if retry_alo_reject and not move_grid_order_away_from_active(row, asset, order):
        order["status"] = "skipped_alo_price_search"
        order["oid"] = None
        order["skipped_at"] = now
        order["alo_price_attempts"] = GRID_ALO_PRICE_ATTEMPT_LIMIT
        return False
    ensure_grid_order_min_notional(row, asset, order)

    def try_reduce_only_after_margin_reject(error_text: str) -> dict[str, Any] | None:
        if restore_without_reduce_only:
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
            retry_oid, retry_state, retry_status = submit_grid_child_order(exchange, coin, order)
            return {"submitted": True, "oid": retry_oid, "state": retry_state, "status": retry_status}
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
        oid, state, status = submit_grid_child_order(exchange, coin, order)
    except GridPostOnlyRejected as exc:
        if not retry_alo_reject:
            order["status"] = "skipped_post_only"
            order["oid"] = None
            order["last_error"] = str(exc)
            order["skipped_at"] = now
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
        set_grid_order_tif(order, "Gtc")
        for attempt in range(1, GRID_ALO_PRICE_ATTEMPT_LIMIT):
            price = decimal_or_none(order.get("price", order.get("limit_px")))
            if price is None or price <= 0:
                order["status"] = "skipped_alo_price_search"
                order["oid"] = None
                order["skipped_at"] = now
                order["alo_price_attempts"] = attempt
                return False
            if active_price_too_close(row, side, price, exclude=order, spacing_multiplier=GRID_ALO_SPACING_MULTIPLIER):
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
            try:
                oid, state, status = submit_grid_child_order(exchange, coin, order)
                break
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
                if skip_grid_exchange_reject(order, error_text, now):
                    return False
                if not is_min_order_value_error_text(error_text) or order.get("resized_min_retry_at"):
                    raise
                bump_grid_order_size_one_step(asset, order)
                order["resized_min_retry_at"] = now
                oid, state, status = submit_grid_child_order(exchange, coin, order)
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
                    if state == "filled":
                        order["filled_at"] = now
                        order["replacement_pending"] = True
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
        if skip_grid_exchange_reject(order, error_text, now):
            return False
        if not is_min_order_value_error_text(error_text) or order.get("resized_min_retry_at"):
            raise
        bump_grid_order_size_one_step(asset, order)
        order["resized_min_retry_at"] = now
        try:
            oid, state, status = submit_grid_child_order(exchange, coin, order)
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
    size = min(size, max_size)
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    step = Decimal(1).scaleb(-sz_decimals)
    size = (size / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if size <= 0:
        return None
    slippage = Decimal(str(row.get("slippage") or DEFAULT_SLIPPAGE))
    limit_px = Decimal(str(exchange._slippage_price(coin, is_buy, float(slippage), float(current_mid))))
    limit_px = rounded_perp_price(limit_px, sz_decimals)
    if limit_px <= 0:
        return None
    if size * limit_px < MIN_NOTIONAL:
        size = grid_size_for_min_notional(size, limit_px, sz_decimals, MIN_NOTIONAL)
        size = min(size, max_size)
        size = (size / step).to_integral_value(rounding=ROUND_FLOOR) * step
        if size <= 0:
            return None
        if size * limit_px < MIN_NOTIONAL:
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
        },
    }


def submit_grid_panic_reduce(
    exchange: Any,
    coin: str,
    order: dict[str, Any],
    now: int,
    row: dict[str, Any],
) -> bool:
    plan = order.get("plan")
    if not isinstance(plan, dict):
        return False
    result = exchange.order(
        coin,
        bool(plan["is_buy"]),
        float(plan["size"]),
        float(plan["limit_px"]),
        plan["order_type"],
        reduce_only=True,
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
            order["oid"] = int(status["filled"].get("oid", 0))
            order["status"] = "filled"
            order["filled_at"] = now
            order["last_submit_status"] = status
            return True
        if isinstance(status.get("resting"), dict):
            order["oid"] = int(status["resting"].get("oid", 0))
            order["status"] = "active"
            order["submitted_at"] = now
            order["last_submit_status"] = status
            return True
    row["panic_reduce_error"] = f"panic reduce response did not include an order id: {result}"
    row["panic_reduce_error_at"] = now
    return False


def cancel_grid_entries(exchange: Any, coin: str, entries: list[dict[str, Any]], now: int, note: str) -> int:
    requests = []
    for entry in entries:
        try:
            requests.append({"coin": coin, "oid": int(entry["oid"])})
        except (KeyError, TypeError, ValueError):
            continue
    if not requests:
        return 0
    result = exchange.bulk_cancel(requests)
    log_event("grid_cancel_entries", {"note": note, "requests": requests, "result": result})
    if result.get("status") != "ok":
        raise RuntimeError(f"Failed to cancel grid orders: {result}")
    cancelled = {int(request["oid"]) for request in requests}
    for entry in entries:
        try:
            oid = int(entry["oid"])
        except (KeyError, TypeError, ValueError):
            continue
        if oid in cancelled:
            entry["status"] = note
            entry["cancelled_at"] = now
    return len(cancelled)


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
            cancelled = cancel_grid_entries(exchange, coin, [target], now, "brake_near_add_risk")
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


def maintain_grid(row: dict[str, Any], cache: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
    phase_started_at = time.monotonic()
    last_phase_at = phase_started_at
    phase_timings: list[tuple[str, float]] = []

    def mark_phase(name: str) -> None:
        nonlocal last_phase_at
        now_phase = time.monotonic()
        phase_timings.append((name, now_phase - last_phase_at))
        last_phase_at = now_phase

    cache = cache if cache is not None else {}
    changed = ensure_grid_base_sizes(row)
    network = str(row.get("network") or "mainnet")
    timeout = float(row.get("timeout") or 20)
    raw_coin = str(row.get("raw_coin") or row["coin"])
    dex = str(row.get("dex") or "")
    client_cache = cache.setdefault("clients", {})
    client_key = (network, timeout, dex)
    if client_key not in client_cache:
        client_cache[client_key] = build_clients(network, timeout, raw_coin)
    info, exchange, account, _signer, _role = client_cache[client_key]
    mark_phase("clients")
    coin, asset = resolve_perp_asset(info, str(row.get("raw_coin") or row["coin"]))
    mark_phase("asset")
    now = int(cache.setdefault("now", int(time.time())))
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
    mark_phase("mids")
    best_bid, best_ask = best_bid_ask(info, coin)
    mark_phase("book")
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
    else:
        position_size = Decimal(str(current_position.get("szi", "0")))
        position_value = decimal_or_none(current_position.get("positionValue"))
        if position_value is None:
            position_value = abs(position_size * current_mid)
        else:
            position_value = abs(position_value)
        liquidation_px = decimal_or_none(current_position.get("liquidationPx"))
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
    margin_ratio = account_margin_ratio(info, account, network, cache)
    account_margin_protected = margin_ratio is not None and margin_ratio < GRID_ACCOUNT_MARGIN_RATIO_THRESHOLD
    margin_gap_multiplier = grid_margin_gap_multiplier(margin_ratio)
    margin_gap_multiplier_text = decimal_to_plain(margin_gap_multiplier)
    margin_state_changed = row.get("margin_gap_multiplier") != margin_gap_multiplier_text
    row["margin_gap_multiplier"] = margin_gap_multiplier_text
    row["margin_gap_soft_threshold"] = decimal_to_plain(GRID_ACCOUNT_MARGIN_RATIO_SOFT_THRESHOLD)
    brake_state_pruned = prune_add_risk_brake_state(row, now)
    mark_phase("margin")

    open_orders_cache = cache.setdefault("open_orders", {})
    open_orders_key = (network, account, dex)
    if open_orders_key not in open_orders_cache:
        open_orders_cache[open_orders_key] = collect_frontend_open_orders(info, account, dex)
    open_oids = open_order_oids(info, account, dex, coin, open_orders_cache[open_orders_key])
    mark_phase("open_orders")

    fills_cache = cache.setdefault("fills", {})
    common_start_ms = (now - GRID_FILL_LOOKBACK_SECONDS) * 1000
    fills_key = (network, account, common_start_ms, now_ms)
    if fills_key not in fills_cache:
        fills_cache[fills_key] = info.user_fills_by_time(account, common_start_ms, now_ms)
        log_event("grid_user_fills_by_time", {"start_ms": common_start_ms, "end_ms": now_ms, "count": len(fills_cache[fills_key])})
    fills_by_oid = recent_fills_by_oid(info, account, coin, start_ms, now_ms, fills_cache[fills_key])
    mark_phase("fills")
    changed = avg_state_changed or margin_state_changed or brake_state_pruned or stale_margin_pauses_cleared
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
        side = str(order.get("side") or "")
        if not side_submission_allowed(side):
            return False
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
        )
        if submitted:
            submissions_by_side[side] = submissions_by_side.get(side, 0) + 1
            if str(order.get("status")) == "filled":
                filled_submission_sides.add(side)
        return submitted

    def submit_replacement(order: dict[str, Any]) -> bool:
        side = str(order.get("side") or "")
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
        )
        if submitted:
            submissions_by_side[side] = submissions_by_side.get(side, 0) + 1
            replacement_quota_sides.add(side)
        return submitted

    for entry in levels:
        if not isinstance(entry, dict) or not entry.get("side"):
            continue
        if str(entry.get("status", "active")) not in {"active", "recovery_deferred"}:
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
            if skip_unknown_oid_grid_recovery(entry, old_oid, now, order_status):
                changed = True
                continue
            side = str(entry.get("side"))
            if grid_margin_pause_active(row, side, now, position_value, position_size):
                changed = True
                continue
            if skip_stale_grid_recovery(entry, old_oid, now, current_mid, best_bid, best_ask):
                changed = True
                continue
            if submit_tracked(entry):
                entry["recovered_missing_oid"] = old_oid
                entry["recovered_missing_at"] = now
                missing_without_fill.append(oid)
                recovered_missing += 1
                if entry.get("replacement_pending"):
                    newly_filled.append(entry)
            else:
                deferred_status = str(entry.get("status") or "recovery_deferred")
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
    mark_phase("missing_scan")

    pending_replacement_sides = {
        "sell" if bool(entry.get("is_buy")) else "buy"
        for entry in newly_filled
    }
    replacement_quota_sides.update(pending_replacement_sides)
    saved_target_per_side = int(row.get("target_orders_per_side") or GRID_TARGET_ORDERS_PER_SIDE)
    target_per_side = GRID_TARGET_ORDERS_PER_SIDE if saved_target_per_side == 5 else saved_target_per_side
    if target_per_side != saved_target_per_side:
        row["target_orders_per_side"] = target_per_side
        changed = True

    add_risk_braked = 0

    paused = 0
    to_pause_limit: list[dict[str, Any]] = []
    for side in ("buy", "sell"):
        entries = active_grid_entries(row, side)
        entries.sort(
            key=lambda entry: decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0"),
            reverse=side == "buy",
        )
        projected_side_value = position_value
        for entry in entries:
            is_buy = bool(entry.get("is_buy"))
            order_notional = Decimal(str(entry.get("size"))) * Decimal(str(entry.get("price", entry.get("limit_px"))))
            if not grid_order_allowed_by_max(
                position_size,
                projected_side_value,
                is_buy,
                order_notional,
                max_position_value,
                policy,
                min_position_value,
            ):
                to_pause_limit.append(entry)
                continue
            if grid_order_would_add_risk(position_size, is_buy):
                projected_side_value += order_notional
            else:
                projected_side_value = max(Decimal("0"), projected_side_value - order_notional)
    if to_pause_limit:
        paused += cancel_grid_entries(exchange, coin, to_pause_limit, now, "paused_limit")
    paused_limit_ids = {id(entry) for entry in to_pause_limit}
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
        to_pause_density.extend(entry for entry in candidates if id(entry) not in paused_limit_ids)
    if to_pause_density:
        paused += cancel_grid_entries(exchange, coin, to_pause_density, now, GRID_RISK_DENSITY_PAUSE_STATUS)
        for entry in to_pause_density:
            entry["risk_density_allowed"] = risk_density_allowed.get(str(entry.get("side") or ""), target_per_side)
            entry["risk_density_multiplier"] = decimal_to_plain(
                risk_density_multiplier.get(str(entry.get("side") or ""), Decimal("1"))
            )
            entry["risk_density_paused_at"] = now
    paused_density_ids = {id(entry) for entry in to_pause_density}
    to_pause_active_cap: list[dict[str, Any]] = []
    for side in ("buy", "sell"):
        candidates, allowed = grid_active_cap_pause_candidates(row, side)
        to_pause_active_cap.extend(
            entry
            for entry in candidates
            if id(entry) not in paused_limit_ids and id(entry) not in paused_density_ids
        )
    if to_pause_active_cap:
        paused += cancel_grid_entries(exchange, coin, to_pause_active_cap, now, GRID_ACTIVE_CAP_PAUSE_STATUS)
        for entry in to_pause_active_cap:
            entry["active_cap_allowed"] = GRID_MAX_ACTIVE_ORDERS_PER_SIDE
            entry["active_cap_paused_at"] = now
    if paused:
        changed = True
    mark_phase("pause_caps")

    refreshed = 0
    to_refresh: list[dict[str, Any]] = []
    if not account_margin_protected:
        for entry in active_grid_entries(row):
            desired_reduce_only = grid_order_should_reduce_only(position_size, bool(entry.get("is_buy")), policy)
            if bool(entry.get("reduce_only", False)) == desired_reduce_only:
                plan = entry.get("plan")
                if not isinstance(plan, dict) or bool(plan.get("reduce_only", False)) == desired_reduce_only:
                    continue
            to_refresh.append(entry)
    if to_refresh:
        refreshed = cancel_grid_entries(exchange, coin, to_refresh, now, "refresh_reduce_only")
        if refreshed:
            pause_refreshed_reduce_only_entries(to_refresh, now)
        changed = True
    mark_phase("refresh")

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
    )
    if dense_regridded:
        changed = True
    mark_phase("dense")

    near_regrids = 0

    projected_position_values: dict[str, Decimal] = {}
    for side in ("buy", "sell"):
        projected = position_value
        for entry in active_grid_entries(row, side):
            order_notional = Decimal(str(entry.get("size"))) * Decimal(str(entry.get("price", entry.get("limit_px"))))
            if grid_order_would_add_risk(position_size, bool(entry.get("is_buy"))):
                projected += order_notional
            else:
                projected = max(Decimal("0"), projected - order_notional)
        projected_position_values[side] = projected
    previous_panic_state = (
        row.get("panic_ratio"),
        row.get("panic_ratio_threshold"),
        row.get("panic_liquidation_px"),
    )
    panic_ratio = grid_panic_ratio(row, position_size, current_mid, liquidation_px)
    panic_threshold = grid_panic_ratio_threshold(row)
    panic_reduced = 0
    row["panic_ratio_threshold"] = decimal_to_plain(panic_threshold)
    row["panic_ratio"] = decimal_to_plain(panic_ratio) if panic_ratio is not None else None
    row["panic_liquidation_px"] = decimal_to_plain(liquidation_px) if liquidation_px is not None else None
    if previous_panic_state != (
        row.get("panic_ratio"),
        row.get("panic_ratio_threshold"),
        row.get("panic_liquidation_px"),
    ):
        changed = True
    if panic_ratio is not None and panic_ratio < panic_threshold:
        panic_order = build_grid_panic_reduce_order(exchange, row, coin, asset, current_mid, position_size)
        if panic_order is not None:
            panic_order["panic_ratio"] = decimal_to_plain(panic_ratio)
            panic_order["panic_ratio_threshold"] = decimal_to_plain(panic_threshold)
            submitted = submit_grid_panic_reduce(exchange, coin, panic_order, now, row)
            if submitted:
                panic_reduced = 1
                row["panic_reduce_at"] = now
                row["panic_reduce_count"] = int(row.get("panic_reduce_count") or 0) + 1
                row["panic_reduce_side"] = panic_order.get("side")
                row["panic_reduce_size"] = panic_order.get("size")
                row["panic_reduce_price"] = panic_order.get("price")
                row["panic_reduce_ratio"] = decimal_to_plain(panic_ratio)
                row.pop("panic_reduce_error", None)
                row.pop("panic_reduce_error_at", None)
                changed = True
    mark_phase("panic")
    replacements = 0
    for entry in newly_filled:
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
        if grid_margin_pause_active(row, replacement_side, now, position_value, position_size):
            preserve_replacement_order(levels, replacement, now, "margin_pause_active")
            changed = True
            continue
        projected_position_value = projected_position_values[replacement_side]
        order_notional = Decimal(str(replacement["size"])) * Decimal(str(replacement["price"]))
        if not grid_order_allowed_by_max(
            position_size,
            projected_position_value,
            bool(replacement["is_buy"]),
            order_notional,
            max_position_value,
            policy,
            min_position_value,
        ):
            replacement["status"] = "paused_limit"
            replacement["paused_at"] = now
            preserve_replacement_order(levels, replacement, now)
            changed = True
            continue
        submitted = submit_replacement(replacement)
        if not submitted:
            preserve_replacement_order(levels, replacement, now)
            changed = True
            continue
        levels.append(replacement)
        order_notional = Decimal(str(replacement["size"])) * Decimal(str(replacement["price"]))
        if grid_order_would_add_risk(position_size, bool(replacement["is_buy"])):
            projected_position_values[replacement_side] += order_notional
        else:
            projected_position_values[replacement_side] = max(Decimal("0"), projected_position_value - order_notional)
        replacements += 1
        changed = True
    mark_phase("replacements")

    to_pause_post_replacement_cap: list[dict[str, Any]] = []
    for side in ("buy", "sell"):
        candidates, _allowed = grid_active_cap_pause_candidates(row, side)
        to_pause_post_replacement_cap.extend(candidates)
    if to_pause_post_replacement_cap:
        paused += cancel_grid_entries(exchange, coin, to_pause_post_replacement_cap, now, GRID_ACTIVE_CAP_PAUSE_STATUS)
        for entry in to_pause_post_replacement_cap:
            entry["active_cap_allowed"] = GRID_MAX_ACTIVE_ORDERS_PER_SIDE
            entry["active_cap_paused_at"] = now
        changed = True
    mark_phase("post_cap")

    restored = 0
    for entry in levels:
        if (
            not isinstance(entry, dict)
            or str(entry.get("status")) != GRID_RISK_DENSITY_PAUSE_STATUS
            or bool(entry.get("replacement_order"))
            or entry.get("side") is None
        ):
            continue
        side = str(entry["side"])
        if panic_reduced and grid_order_would_add_risk(position_size, side == "buy"):
            continue
        if not side_submission_allowed(side):
            continue
        if len(active_grid_oids(row, side)) >= GRID_MAX_ACTIVE_ORDERS_PER_SIDE:
            continue
        if not grid_risk_density_restore_allowed(
            row,
            entry,
            side,
            position_size,
            target_per_side,
            margin_gap_multiplier,
        ):
            continue
        if grid_margin_pause_active(row, side, now, position_value, position_size):
            continue
        if defer_paused_grid_restore_if_crossing(entry, now, current_mid, best_bid, best_ask):
            continue
        projected_position_value = projected_position_values[side]
        order_notional = Decimal(str(entry.get("size"))) * Decimal(str(entry.get("price", entry.get("limit_px"))))
        if not grid_order_allowed_by_max(
            position_size,
            projected_position_value,
            bool(entry.get("is_buy")),
            order_notional,
            max_position_value,
            policy,
            min_position_value,
        ):
            continue
        if not submit_tracked(entry):
            changed = True
            continue
        if grid_order_would_add_risk(position_size, bool(entry.get("is_buy"))):
            projected_position_values[side] += order_notional
        else:
            projected_position_values[side] = max(Decimal("0"), projected_position_value - order_notional)
        restored += 1
        changed = True
    mark_phase("risk_restore")

    topped_up = 0
    for side in ("buy", "sell"):
        if panic_reduced and grid_order_would_add_risk(position_size, side == "buy"):
            continue
        if not side_submission_allowed(side):
            continue
        if grid_margin_pause_active(row, side, now, position_value, position_size):
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
            if not grid_order_allowed_by_max(
                position_size,
                projected_position_value,
                bool(topup["is_buy"]),
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
                changed = True
                break
            levels.append(topup)
            order_notional = Decimal(str(topup["size"])) * Decimal(str(topup["price"]))
            remaining_topups -= 1
            if grid_order_would_add_risk(position_size, bool(topup["is_buy"])):
                projected_position_values[side] += order_notional
            else:
                projected_position_values[side] = max(Decimal("0"), projected_position_value - order_notional)
            topped_up += 1
            changed = True
    mark_phase("topups")

    for entry in levels:
        if isinstance(entry, dict) and pause_refresh_reduce_only_replacement(entry, now):
            changed = True
            continue
        if isinstance(entry, dict) and pause_skipped_account_margin_replacement(levels, entry, now):
            changed = True
            continue
        if isinstance(entry, dict) and str(entry.get("status")) == "paused_account_margin":
            if entry.get("replacement_order"):
                preserve_replacement_order(levels, entry, now, "paused_account_margin")
                changed = True
                continue
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
        is_replacement_order = bool(entry.get("replacement_order"))
        status = str(entry.get("status"))
        if normalize_margin_paused_replacement(entry, now):
            status = str(entry.get("status"))
            changed = True
        if panic_reduced and not is_replacement_order and grid_order_would_add_risk(position_size, side == "buy"):
            continue
        if not side_submission_allowed(side):
            continue
        if status == GRID_ACTIVE_CAP_PAUSE_STATUS and not grid_active_cap_restore_allowed(row, entry, side):
            continue
        if not is_replacement_order:
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
        if grid_margin_pause_active(row, side, now, position_value, position_size):
            continue
        if defer_paused_grid_restore_if_crossing(entry, now, current_mid, best_bid, best_ask):
            continue
        projected_position_value = projected_position_values[side]
        order_notional = Decimal(str(entry.get("size"))) * Decimal(str(entry.get("price", entry.get("limit_px"))))
        if not grid_order_allowed_by_max(
            position_size,
            projected_position_value,
            bool(entry.get("is_buy")),
            order_notional,
            max_position_value,
            policy,
            min_position_value,
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
        order_notional = Decimal(str(entry.get("size"))) * Decimal(str(entry.get("price", entry.get("limit_px"))))
        if grid_order_would_add_risk(position_size, bool(entry.get("is_buy"))):
            projected_position_values[side] += order_notional
        else:
            projected_position_values[side] = max(Decimal("0"), projected_position_value - order_notional)
        restored += 1
        changed = True
    mark_phase("paused_restore")

    trimmed = trim_excess_grid_entries(exchange, coin, row, target_per_side, now)
    if trimmed:
        changed = True
    side_cap_cleared = clear_grid_side_cap_entries(exchange, coin, row, now)
    if side_cap_cleared:
        changed = True
    mark_phase("trim")

    row["status"] = "active"
    row.pop("error", None)
    row.pop("last_error", None)
    row["updated_at"] = now
    row["last_fill_check_ms"] = now_ms
    row["position_value"] = decimal_to_plain(position_value)
    row["position_size"] = decimal_to_plain(position_size)
    row["account_margin_ratio"] = decimal_to_plain(margin_ratio) if margin_ratio is not None else None
    row["account_margin_protected"] = account_margin_protected
    row["open_oids"] = sorted(grid_batch_open_oids(row))
    margin_cooldowns = ",".join(sorted((row.get("margin_pauses") or {}).keys())) or "-"
    margin_ratio_label = f"{margin_ratio * Decimal('100'):.2f}%" if margin_ratio is not None else "unknown"
    row["note"] = (
        f"grid maintained; replacements={replacements}; topped_up={topped_up}; "
        f"paused={paused}; refreshed={refreshed}; dense_regridded={dense_regridded}; restored={restored}; trimmed={trimmed}; near_regrids={near_regrids}; "
        f"add_risk_braked={add_risk_braked}; side_cap_cleared={side_cap_cleared}; "
        f"recovered_missing={recovered_missing}; margin_cooldown={margin_cooldowns}; "
        f"submissions=buy:{submissions_by_side['buy']},sell:{submissions_by_side['sell']}; "
        f"filled_stop={','.join(sorted(filled_submission_sides)) or '-'}; "
        f"avg={row.get('avg') if row.get('avg') is not None else '-'}; avg_multiplier={row.get('avg_multiplier', '1')}; "
        f"margin_gap_multiplier={decimal_to_plain(margin_gap_multiplier)}; "
        f"account_margin={margin_ratio_label}; account_protected={int(account_margin_protected)}; "
        f"panic_ratio={row.get('panic_ratio') or '-'}; panic_reduced={panic_reduced}"
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
        or refreshed > 0
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


def grid_level_updated_at(entry: dict[str, Any]) -> int:
    for key in (
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


def prune_grid_levels(row: dict[str, Any]) -> bool:
    if row.get("type") != "grid":
        return False
    levels = row.get("levels")
    if not isinstance(levels, list):
        return False

    live_statuses = {
        "active",
        "pending",
        "recovery_deferred",
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
    kept_paused: list[dict[str, Any]] = [
        entry
        for entry in paused_levels
        if bool(entry.get("replacement_order"))
    ]
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
            key = (
                str(entry.get("side")),
                str(entry.get("price", entry.get("limit_px", ""))),
                str(entry.get("size", "")),
                bool(entry.get("reduce_only", False)),
            )
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


def run_once() -> None:
    rows = load_server_batch()
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
    if not active_trail_indexes and not active_grid_indexes:
        print("trail_worker: no active trail/grid orders")
        return

    mids_cache: dict[tuple[str, str], dict[str, Any]] = {}
    grid_cache: dict[str, Any] = {}
    changed = False
    for index in active_trail_indexes:
        row = rows[index]
        try:
            network = str(row.get("network") or "mainnet")
            raw_coin = str(row.get("raw_coin") or row["coin"])
            dex = str(row.get("dex") or "")
            cache_key = (network, dex)
            if cache_key not in mids_cache:
                info, _exchange, account, _signer, _role = build_clients(network, float(row.get("timeout") or 20), raw_coin, need_exchange=False)
                mids_cache[cache_key] = info.all_mids(dex)
                print(f"trail_worker: mids loaded {network}:{dex or 'default'} account={mask(account)}")
            mid_px = Decimal(str(mids_cache[cache_key][row["coin"]]))
            rows[index], row_changed = modify_trail_stop(row, mid_px)
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

    for index in active_grid_indexes:
        row = rows[index]
        try:
            started_at = time.monotonic()
            rows[index], row_changed = maintain_grid(row, grid_cache)
            changed = changed or row_changed
            elapsed = time.monotonic() - started_at
            print(
                f"trail_worker: grid maintained {row.get('network', 'mainnet')}:{row.get('coin')} "
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

    rows, pruned = prune_done_rows(rows)
    grid_history_pruned = prune_grid_level_history(rows)
    if changed or pruned or grid_history_pruned:
        save_server_batch(rows)


def main() -> None:
    SERVER_BATCH_PATH.touch(exist_ok=True)
    with server_batch_lock(blocking=False) as acquired:
        if not acquired:
            print("trail_worker: previous run still active, skipping")
            return
        run_once()


if __name__ == "__main__":
    main()
