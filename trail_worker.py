#!/usr/bin/env python3
"""One-shot server worker for batched trailing stop maintenance."""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from simple_hyper.runtime import ensure_local_venv


ensure_local_venv(__file__)

from hl_order import (  # noqa: E402
    DEFAULT_SLIPPAGE,
    GRID_ACCOUNT_MARGIN_RATIO_THRESHOLD,
    GRID_TARGET_ORDERS_PER_SIDE,
    SERVER_BATCH_PATH,
    build_clients,
    build_grid_limit_order_plan,
    build_trigger_order_plan,
    collect_frontend_open_orders,
    current_position_size_value,
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
    resolve_perp_asset,
    rounded_perp_price,
    save_server_batch,
    server_batch_lock,
    trail_stop_price,
)


RATE_LIMIT_LOG_PATH = Path(__file__).resolve().parent / "logs" / "trail-rate-limit.jsonl"
DONE_RETENTION_DAYS = 7
DONE_RETENTION_MAX = 500
GRID_LEVEL_HISTORY_MAX = 120
GRID_FILL_LOOKBACK_SECONDS = 24 * 60 * 60
GRID_MARGIN_RETRY_SECONDS = 10 * 60
GRID_MAX_SUBMISSIONS_PER_SIDE_PER_RUN = 1
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

    is_buy = side == "buy"
    paused_position_value = decimal_or_none(pause.get("position_value"))
    expired = now >= int(pause.get("retry_at") or 0)
    position_reduced = paused_position_value is not None and position_value < paused_position_value
    no_longer_adds_risk = not grid_order_would_add_risk(position_size, is_buy)
    if expired or position_reduced or no_longer_adds_risk:
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
        "retry_at": now + GRID_MARGIN_RETRY_SECONDS,
        "position_value": decimal_to_plain(position_value),
    }


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


def refresh_grid_order_tif(order: dict[str, Any]) -> None:
    plan = order.get("plan")
    if not isinstance(plan, dict):
        return
    plan["order_type"] = {"limit": {"tif": "Gtc"}}


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
    next_side = "buy" if next_is_buy else "sell"
    size = None
    if str(row.get("avg_favored_side") or "") == next_side:
        size_key = "topup_buy_size" if next_is_buy else "topup_sell_size"
        size = Decimal(
            str(
                row.get(size_key)
                or row.get("base_buy_size" if next_is_buy else "base_sell_size")
                or "0"
            )
        )
    return grid_order_entry(row, coin, asset, next_is_buy, next_px, reduce_only, size=size, gap=gap)


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


def min_grid_spacing(row: dict[str, Any], price: Decimal) -> Decimal:
    return price * Decimal(str(row["gap_rate"])) * Decimal("0.75")


def active_price_too_close(row: dict[str, Any], side: str, price: Decimal) -> bool:
    for entry in active_grid_entries(row, side):
        existing = decimal_or_none(entry.get("price", entry.get("limit_px")))
        if existing is None or existing <= 0:
            continue
        if abs(existing - price) < min_grid_spacing(row, price):
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
            if any(abs(price - kept) < min_grid_spacing(row, price) for kept in kept_prices):
                dense.append(entry)
                continue
            kept_prices.append(price)
    return dense


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
) -> dict[str, Any] | None:
    gap_key = "topup_buy_gap" if side == "buy" else "topup_sell_gap"
    gap = Decimal(str(row.get(gap_key) or row["gap_rate"]))
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    is_buy = side == "buy"
    base_px = farthest_active_price(row, side, reference_px or current_mid)
    multiplier = Decimal("1") - gap if is_buy else Decimal("1") + gap
    next_px = rounded_perp_price(base_px * multiplier, sz_decimals)
    if next_px <= 0:
        return None
    reduce_only = grid_order_should_reduce_only(position_size, is_buy, policy)
    size_key = "topup_buy_size" if is_buy else "topup_sell_size"
    topup_size = Decimal(str(row.get(size_key) or row.get("base_buy_size" if is_buy else "base_sell_size") or "0"))
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
) -> bool:
    refresh_grid_order_reduce_only(order, position_size, policy)
    refresh_grid_order_tif(order)
    if account_margin_protected:
        if grid_order_would_add_risk(position_size, bool(order.get("is_buy"))):
            order["status"] = "paused_account_margin"
            order["oid"] = None
            order["paused_at"] = now
            return False
        order["reduce_only"] = True
        plan = order.get("plan")
        if isinstance(plan, dict):
            plan["reduce_only"] = True
    ensure_grid_order_min_notional(row, asset, order)
    if not grid_reduce_only_capacity_available(row, order, position_size, position_value):
        order["status"] = "paused_reduce_capacity"
        order["oid"] = None
        order["paused_at"] = now
        return False
    try:
        oid, state, status = submit_grid_child_order(exchange, coin, order)
    except GridPostOnlyRejected as exc:
        order["status"] = "skipped_post_only"
        order["oid"] = None
        order["last_error"] = str(exc)
        order["skipped_at"] = now
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
            order["status"] = "paused_margin"
            order["oid"] = None
            order["last_error"] = error_text
            order["paused_at"] = now
            if grid_order_would_add_risk(position_size, bool(order.get("is_buy"))):
                pause_grid_margin_side(row, str(order.get("side")), now, position_value)
            return False
        if not is_min_order_value_error_text(error_text) or order.get("resized_min_retry_at"):
            raise
        bump_grid_order_size_one_step(asset, order)
        order["resized_min_retry_at"] = now
        oid, state, status = submit_grid_child_order(exchange, coin, order)
    order["oid"] = oid
    order["status"] = state
    order["submitted_at"] = now
    order["last_submit_status"] = status
    if state == "filled":
        order["filled_at"] = now
        order["replacement_pending"] = True
    return True


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


def trim_excess_grid_entries(exchange: Any, coin: str, row: dict[str, Any], target_per_side: int, now: int) -> int:
    trimmed = 0
    for side in ("buy", "sell"):
        entries = active_grid_entries(row, side)
        def price_key(entry: dict[str, Any]) -> Decimal:
            return decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0")

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

        if len(unique_entries) <= target_per_side:
            continue

        # Buy orders farther from the market have lower prices; sell orders farther from
        # the market have higher prices. Cancel those first when a near-side order is added.
        farthest_first = sorted(unique_entries, key=price_key, reverse=side == "sell")
        to_trim = farthest_first[: len(unique_entries) - target_per_side]
        trimmed += cancel_grid_entries(exchange, coin, to_trim, now, "trimmed_excess")
        trimmed_oids = {int(entry["oid"]) for entry in to_trim}
        for entry in entries:
            try:
                oid = int(entry["oid"])
            except (KeyError, TypeError, ValueError):
                continue
            if oid in trimmed_oids:
                entry["status"] = "trimmed_excess"
                entry["cancelled_at"] = now
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

    nearest_px = nearest_active_price(row, side)
    if nearest_px is None:
        return []
    stale_threshold = Decimal("1") - gap * Decimal("5") if is_buy else Decimal("1") + gap * Decimal("5")
    if is_buy and nearest_px >= reference_px * stale_threshold:
        return []
    if not is_buy and nearest_px <= reference_px * stale_threshold:
        return []

    reduce_only = grid_order_should_reduce_only(position_size, is_buy, policy)
    entries: list[dict[str, Any]] = []
    for gap_multiple in (Decimal("3"),):
        multiplier = Decimal("1") - gap * gap_multiple if is_buy else Decimal("1") + gap * gap_multiple
        target_px = rounded_perp_price(reference_px * multiplier, sz_decimals)
        if target_px > 0:
            entries.append(grid_order_entry(row, coin, asset, is_buy, target_px, reduce_only))
    return entries


def maintain_grid(row: dict[str, Any], cache: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
    cache = cache if cache is not None else {}
    network = str(row.get("network") or "mainnet")
    timeout = float(row.get("timeout") or 20)
    raw_coin = str(row.get("raw_coin") or row["coin"])
    dex = str(row.get("dex") or "")
    client_cache = cache.setdefault("clients", {})
    client_key = (network, timeout, dex)
    if client_key not in client_cache:
        client_cache[client_key] = build_clients(network, timeout, raw_coin)
    info, exchange, account, _signer, _role = client_cache[client_key]
    coin, asset = resolve_perp_asset(info, str(row.get("raw_coin") or row["coin"]))
    now = int(cache.setdefault("now", int(time.time())))
    now_ms = now * 1000
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
    best_bid, best_ask = best_bid_ask(info, coin)
    position_size, position_value = current_position_size_value(info, account, coin, dex, current_mid)
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

    open_orders_cache = cache.setdefault("open_orders", {})
    open_orders_key = (network, account, dex)
    if open_orders_key not in open_orders_cache:
        open_orders_cache[open_orders_key] = collect_frontend_open_orders(info, account, dex)
    open_oids = open_order_oids(info, account, dex, coin, open_orders_cache[open_orders_key])

    fills_cache = cache.setdefault("fills", {})
    common_start_ms = (now - GRID_FILL_LOOKBACK_SECONDS) * 1000
    fills_key = (network, account, common_start_ms, now_ms)
    if fills_key not in fills_cache:
        fills_cache[fills_key] = info.user_fills_by_time(account, common_start_ms, now_ms)
        log_event("grid_user_fills_by_time", {"start_ms": common_start_ms, "end_ms": now_ms, "count": len(fills_cache[fills_key])})
    fills_by_oid = recent_fills_by_oid(info, account, coin, start_ms, now_ms, fills_cache[fills_key])
    changed = avg_state_changed
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
            status_name = str(order_status.get("order", {}).get("status") or "")
            if status_name == "filled":
                entry["status"] = "filled"
                entry["filled_at"] = now
                entry["confirmed_filled_oid"] = old_oid
                entry["replacement_pending"] = True
                newly_filled.append(entry)
                changed = True
                continue
            if status_name == "reduceOnlyCanceled":
                entry["status"] = "skipped_reduce_only"
                entry["oid"] = None
                entry["last_error"] = "exchange canceled excess reduce-only order"
                entry["skipped_at"] = now
                changed = True
                continue
            side = str(entry.get("side"))
            if grid_margin_pause_active(row, side, now, position_value, position_size):
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

    pending_replacement_sides = {
        "sell" if bool(entry.get("is_buy")) else "buy"
        for entry in newly_filled
    }
    replacement_quota_sides.update(pending_replacement_sides)

    paused = 0
    to_pause: list[dict[str, Any]] = []
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
                to_pause.append(entry)
                continue
            if grid_order_would_add_risk(position_size, is_buy):
                projected_side_value += order_notional
            else:
                projected_side_value = max(Decimal("0"), projected_side_value - order_notional)
    if to_pause:
        paused = cancel_grid_entries(exchange, coin, to_pause, now, "paused_limit")
        changed = True

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
        changed = True

    deduped = 0
    to_dedup = dense_grid_entries(row)
    if to_dedup:
        deduped = cancel_grid_entries(exchange, coin, to_dedup, now, "dedup_dense")
        changed = True

    near_regrids = 0
    for side in ("buy", "sell"):
        if side in pending_replacement_sides:
            continue
        if grid_margin_pause_active(row, side, now, position_value, position_size):
            continue
        reference_px = grid_reference_price(side, current_mid, best_bid, best_ask)
        old_near_side_entries = active_grid_entries(row, side)
        near_orders = near_grid_orders_if_stale(
            row,
            coin,
            asset,
            side,
            reference_px,
            position_size,
            policy,
        )
        unique_old_entries: dict[int, dict[str, Any]] = {}
        for entry in old_near_side_entries:
            try:
                unique_old_entries.setdefault(int(entry["oid"]), entry)
            except (KeyError, TypeError, ValueError):
                continue
        farthest_old = sorted(
            unique_old_entries.values(),
            key=lambda entry: decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0"),
            reverse=side == "sell",
        )
        projected_near_value = position_value
        for entry in farthest_old:
            order_notional = Decimal(str(entry.get("size"))) * Decimal(str(entry.get("price", entry.get("limit_px"))))
            if grid_order_would_add_risk(position_size, bool(entry.get("is_buy"))):
                projected_near_value += order_notional
            else:
                projected_near_value = max(Decimal("0"), projected_near_value - order_notional)
        submitted_near = 0
        for near_order in near_orders:
            if not side_submission_allowed(side):
                break
            if submitted_near >= len(farthest_old):
                break
            if active_price_too_close(row, side, Decimal(str(near_order["price"]))):
                continue
            old_entry = farthest_old[submitted_near]
            old_notional = Decimal(str(old_entry.get("size"))) * Decimal(str(old_entry.get("price", old_entry.get("limit_px"))))
            projected_after_old_cancel = projected_near_value
            if grid_order_would_add_risk(position_size, bool(old_entry.get("is_buy"))):
                projected_after_old_cancel = max(Decimal("0"), projected_after_old_cancel - old_notional)
            else:
                projected_after_old_cancel += old_notional
            order_notional = Decimal(str(near_order["size"])) * Decimal(str(near_order["price"]))
            if not grid_order_allowed_by_max(
                position_size,
                projected_after_old_cancel,
                bool(near_order["is_buy"]),
                order_notional,
                max_position_value,
                policy,
                min_position_value,
            ):
                continue
            submitted = submit_tracked(near_order)
            if submitted:
                levels.append(near_order)
                if near_order.get("replacement_pending"):
                    newly_filled.append(near_order)
                near_regrids += 1
                submitted_near += 1
                if grid_order_would_add_risk(position_size, bool(near_order["is_buy"])):
                    projected_near_value = projected_after_old_cancel + order_notional
                else:
                    projected_near_value = max(Decimal("0"), projected_after_old_cancel - order_notional)
            changed = True
        if submitted_near:
            replaced = cancel_grid_entries(
                exchange,
                coin,
                farthest_old[:submitted_near],
                now,
                "replaced_far_side",
            )
            if replaced:
                changed = True

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
            changed = True
            continue
        if active_price_too_close(row, str(replacement["side"]), Decimal(str(replacement["price"]))):
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
            levels.append(replacement)
            changed = True
            continue
        submitted = submit_replacement(replacement)
        if not submitted:
            changed = True
            continue
        levels.append(replacement)
        if grid_order_would_add_risk(position_size, bool(replacement["is_buy"])):
            projected_position_values[replacement_side] += order_notional
        else:
            projected_position_values[replacement_side] = max(Decimal("0"), projected_position_value - order_notional)
        replacements += 1
        changed = True

    topped_up = 0
    saved_target_per_side = int(row.get("target_orders_per_side") or GRID_TARGET_ORDERS_PER_SIDE)
    target_per_side = GRID_TARGET_ORDERS_PER_SIDE if saved_target_per_side == 5 else saved_target_per_side
    if target_per_side != saved_target_per_side:
        row["target_orders_per_side"] = target_per_side
        changed = True
    for side in ("buy", "sell"):
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
            topup = next_depth_order(row, coin, asset, side, current_mid, position_size, position_value, max_position_value, policy, reference_px)
            if topup is None:
                break
            if active_price_too_close(row, side, Decimal(str(topup["price"]))):
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
            remaining_topups -= 1
            if grid_order_would_add_risk(position_size, bool(topup["is_buy"])):
                projected_position_values[side] += order_notional
            else:
                projected_position_values[side] = max(Decimal("0"), projected_position_value - order_notional)
            topped_up += 1
            changed = True

    restored = 0
    for entry in levels:
        if not isinstance(entry, dict) or entry.get("side") is None or str(entry.get("status")) not in {
            "paused_max",
            "paused_limit",
            "paused_margin",
            "paused_reduce_capacity",
            "paused_account_margin",
        }:
            continue
        side = str(entry["side"])
        if not side_submission_allowed(side):
            continue
        if len(active_grid_oids(row, side)) >= target_per_side:
            continue
        if grid_margin_pause_active(row, side, now, position_value, position_size):
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
        if active_price_too_close(row, str(entry["side"]), Decimal(str(entry.get("price", entry.get("limit_px"))))):
            continue
        entry_price = Decimal(str(entry.get("price", entry.get("limit_px"))))
        is_buy = bool(entry.get("is_buy"))
        marketable = (is_buy and best_ask is not None and entry_price >= best_ask) or (
            not is_buy and best_bid is not None and entry_price <= best_bid
        )
        if marketable:
            entry["status"] = "skipped_marketable_restore"
            entry["oid"] = None
            entry["skipped_at"] = now
            changed = True
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

    trimmed = trim_excess_grid_entries(exchange, coin, row, target_per_side, now)
    if trimmed:
        changed = True

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
        f"paused={paused}; refreshed={refreshed}; deduped={deduped}; restored={restored}; trimmed={trimmed}; near_regrids={near_regrids}; "
        f"recovered_missing={recovered_missing}; margin_cooldown={margin_cooldowns}; "
        f"submissions=buy:{submissions_by_side['buy']},sell:{submissions_by_side['sell']}; "
        f"filled_stop={','.join(sorted(filled_submission_sides)) or '-'}; "
        f"avg={row.get('avg') if row.get('avg') is not None else '-'}; avg_multiplier={row.get('avg_multiplier', '1')}; "
        f"account_margin={margin_ratio_label}; account_protected={int(account_margin_protected)}"
    )
    return (
        row,
        changed
        or replacements > 0
        or topped_up > 0
        or paused > 0
        or refreshed > 0
        or deduped > 0
        or restored > 0
        or trimmed > 0
        or near_regrids > 0
        or recovered_missing > 0,
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
    paused_statuses = {
        "paused_max",
        "paused_limit",
        "paused_margin",
        "paused_reduce_capacity",
        "paused_account_margin",
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
        elif status in paused_statuses:
            paused_levels.append(entry)
        else:
            history_levels.append(entry)

    target_per_side = int(row.get("target_orders_per_side") or GRID_TARGET_ORDERS_PER_SIDE)
    active_counts = {
        side: len(active_grid_oids(row, side))
        for side in ("buy", "sell")
    }
    kept_paused: list[dict[str, Any]] = []
    for side in ("buy", "sell"):
        keep_count = max(0, target_per_side - active_counts[side])
        if keep_count == 0:
            continue
        side_paused = sorted(
            (entry for entry in paused_levels if str(entry.get("side")) == side),
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
