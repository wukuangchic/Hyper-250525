#!/usr/bin/env python3
"""One-shot server worker for batched trailing stop maintenance."""

from __future__ import annotations

import fcntl
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from simple_hyper.runtime import ensure_local_venv


ensure_local_venv(__file__)

from hl_order import (  # noqa: E402
    DEFAULT_SLIPPAGE,
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
    trail_stop_price,
)


LOCK_PATH = Path(__file__).resolve().parent / "server_batch.lock"
RATE_LIMIT_LOG_PATH = Path(__file__).resolve().parent / "logs" / "trail-rate-limit.jsonl"
DONE_RETENTION_DAYS = 7
DONE_RETENTION_MAX = 500
GRID_LEVEL_HISTORY_MAX = 120
GRID_FILL_LOOKBACK_SECONDS = 24 * 60 * 60
TRANSIENT_STATUS_CODES = {429, 502, 503, 504}
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


def grid_order_entry(row: dict[str, Any], coin: str, asset: dict[str, Any], is_buy: bool, price: Decimal, reduce_only: bool) -> dict[str, Any]:
    size_key = "buy_size" if is_buy else "sell_size"
    size = Decimal(str(row.get(size_key) or "0"))
    if size <= 0:
        raise ValueError(f"grid row is missing {size_key}")
    min_notional = Decimal(str(row.get("min_order_value") or "10"))
    size = grid_size_for_min_notional(size, price, int(asset["szDecimals"]), min_notional)
    side = "buy" if is_buy else "sell"
    plan = build_grid_limit_order_plan(coin, is_buy, size, price, asset, reduce_only, side)
    plan["grid_gap"] = Decimal(str(row["gap_rate"]))
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
    fill: dict[str, Any],
    filled_is_buy: bool,
    position_size: Decimal,
    position_value: Decimal,
    max_position_value: Decimal,
    policy: str,
) -> dict[str, Any] | None:
    gap = Decimal(str(row["gap_rate"]))
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    fill_px = decimal_or_none(fill.get("px"))
    if fill_px is None or fill_px <= 0:
        return None
    next_is_buy = not filled_is_buy
    multiplier = Decimal("1") - gap if next_is_buy else Decimal("1") + gap
    next_px = rounded_perp_price(fill_px * multiplier, sz_decimals)
    reduce_only = grid_order_should_reduce_only(position_size, next_is_buy, policy)
    return grid_order_entry(row, coin, asset, next_is_buy, next_px, reduce_only)


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
    gap = Decimal(str(row["gap_rate"]))
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    is_buy = side == "buy"
    base_px = farthest_active_price(row, side, reference_px or current_mid)
    multiplier = Decimal("1") - gap if is_buy else Decimal("1") + gap
    next_px = rounded_perp_price(base_px * multiplier, sz_decimals)
    if next_px <= 0:
        return None
    reduce_only = grid_order_should_reduce_only(position_size, is_buy, policy)
    return grid_order_entry(row, coin, asset, is_buy, next_px, reduce_only)


def submit_grid_order_entry(
    exchange: Any,
    coin: str,
    order: dict[str, Any],
    now: int,
    row: dict[str, Any],
    asset: dict[str, Any],
    position_size: Decimal,
    policy: str,
) -> bool:
    refresh_grid_order_reduce_only(order, position_size, policy)
    ensure_grid_order_min_notional(row, asset, order)
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
        if len(entries) <= target_per_side:
            continue

        def price_key(entry: dict[str, Any]) -> Decimal:
            return decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0")

        # Buy orders farther from the market have lower prices; sell orders farther from
        # the market have higher prices. Cancel those first when a near-side order is added.
        farthest_first = sorted(entries, key=price_key, reverse=side == "sell")
        trimmed += cancel_grid_entries(exchange, coin, farthest_first[: len(entries) - target_per_side], now, "trimmed_excess")
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
    gap = Decimal(str(row["gap_rate"]))
    is_buy = side == "buy"
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    price = rounded_perp_price(reference_px * (Decimal("1") - gap if is_buy else Decimal("1") + gap), sz_decimals)
    if price <= 0:
        return position_value >= max_position_value
    size_key = "buy_size" if is_buy else "sell_size"
    size = Decimal(str(row.get(size_key) or "0"))
    if size <= 0:
        return position_value >= max_position_value
    min_notional = Decimal(str(row.get("min_order_value") or "10"))
    size = grid_size_for_min_notional(size, price, sz_decimals, min_notional)
    order_notional = size * price
    return not grid_order_allowed_by_max(position_size, position_value, is_buy, order_notional, max_position_value, policy)


def near_grid_order_if_stale(
    row: dict[str, Any],
    coin: str,
    asset: dict[str, Any],
    side: str,
    reference_px: Decimal,
    position_size: Decimal,
    position_value: Decimal,
    max_position_value: Decimal,
    policy: str,
) -> dict[str, Any] | None:
    gap = Decimal(str(row["gap_rate"]))
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    is_buy = side == "buy"
    target_px = rounded_perp_price(reference_px * (Decimal("1") - gap if is_buy else Decimal("1") + gap), sz_decimals)
    if target_px <= 0:
        return None

    nearest_px = nearest_active_price(row, side)
    stale_threshold = Decimal("1") - gap * Decimal("2") if is_buy else Decimal("1") + gap * Decimal("2")
    if nearest_px is not None:
        if is_buy and nearest_px >= reference_px * stale_threshold:
            return None
        if not is_buy and nearest_px <= reference_px * stale_threshold:
            return None

    reduce_only = grid_order_should_reduce_only(position_size, is_buy, policy)
    entry = grid_order_entry(row, coin, asset, is_buy, target_px, reduce_only)
    order_notional = Decimal(str(entry["size"])) * Decimal(str(entry["price"]))
    if not grid_order_allowed_by_max(position_size, position_value, is_buy, order_notional, max_position_value, policy):
        return None
    return entry


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
    policy = grid_limit_policy_from_row(row)
    mids_cache = cache.setdefault("mids", {})
    mids_key = (network, dex)
    if mids_key not in mids_cache:
        mids_cache[mids_key] = info.all_mids(dex)
    mids = mids_cache[mids_key]
    current_mid = Decimal(str(mids[coin]))
    best_bid, best_ask = best_bid_ask(info, coin)
    position_size, position_value = current_position_size_value(info, account, coin, dex, current_mid)

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
    changed = False
    missing_without_fill: list[int] = []
    recovered_missing = 0
    newly_filled: list[dict[str, Any]] = []

    levels = row.setdefault("levels", [])
    for entry in levels:
        if not isinstance(entry, dict) or not entry.get("side"):
            continue
        if str(entry.get("status", "active")) != "active":
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
            if submit_grid_order_entry(exchange, coin, entry, now, row, asset, position_size, policy):
                entry["recovered_missing_oid"] = old_oid
                entry["recovered_missing_at"] = now
                missing_without_fill.append(oid)
                recovered_missing += 1
            changed = True
            continue
        entry["status"] = "filled"
        entry["filled_at"] = int(fill.get("time") or now_ms) // 1000
        entry["fill"] = fill
        newly_filled.append(entry)
        changed = True

    paused = 0
    active_entries = active_grid_entries(row)
    to_pause: list[dict[str, Any]] = []
    for entry in active_entries:
        is_buy = bool(entry.get("is_buy"))
        order_notional = Decimal(str(entry.get("size"))) * Decimal(str(entry.get("price", entry.get("limit_px"))))
        if grid_order_allowed_by_max(position_size, position_value, is_buy, order_notional, max_position_value, policy):
            continue
        to_pause.append(entry)
    if to_pause:
        paused = cancel_grid_entries(exchange, coin, to_pause, now, "paused_max")
        changed = True

    refreshed = 0
    to_refresh: list[dict[str, Any]] = []
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
        reference_px = grid_reference_price(side, current_mid, best_bid, best_ask)
        near_order = near_grid_order_if_stale(
            row,
            coin,
            asset,
            side,
            reference_px,
            position_size,
            position_value,
            max_position_value,
            policy,
        )
        if near_order is not None:
            if active_price_too_close(row, side, Decimal(str(near_order["price"]))):
                continue
            submitted = submit_grid_order_entry(exchange, coin, near_order, now, row, asset, position_size, policy)
            if submitted:
                levels.append(near_order)
                near_regrids += 1
            changed = True

    projected_position_value = position_value
    replacements = 0
    for entry in newly_filled:
        fill = entry.get("fill")
        if not isinstance(fill, dict):
            continue
        replacement = replacement_order_from_fill(row, coin, asset, fill, bool(entry.get("is_buy")), position_size, position_value, max_position_value, policy)
        if replacement is None:
            continue
        if active_price_too_close(row, str(replacement["side"]), Decimal(str(replacement["price"]))):
            changed = True
            continue
        order_notional = Decimal(str(replacement["size"])) * Decimal(str(replacement["price"]))
        if not grid_order_allowed_by_max(position_size, projected_position_value, bool(replacement["is_buy"]), order_notional, max_position_value, policy):
            replacement["status"] = "paused_max"
            replacement["paused_at"] = now
            levels.append(replacement)
            changed = True
            continue
        submitted = submit_grid_order_entry(exchange, coin, replacement, now, row, asset, position_size, policy)
        if not submitted:
            changed = True
            continue
        levels.append(replacement)
        if grid_order_would_add_risk(position_size, bool(replacement["is_buy"])):
            projected_position_value += order_notional
        replacements += 1
        changed = True

    topped_up = 0
    target_per_side = int(row.get("target_orders_per_side") or GRID_TARGET_ORDERS_PER_SIDE)
    for side in ("buy", "sell"):
        while len(active_grid_entries(row, side)) < target_per_side:
            reference_px = grid_reference_price(side, current_mid, best_bid, best_ask)
            topup = next_depth_order(row, coin, asset, side, current_mid, position_size, position_value, max_position_value, policy, reference_px)
            if topup is None:
                break
            if active_price_too_close(row, side, Decimal(str(topup["price"]))):
                break
            order_notional = Decimal(str(topup["size"])) * Decimal(str(topup["price"]))
            if not grid_order_allowed_by_max(position_size, projected_position_value, bool(topup["is_buy"]), order_notional, max_position_value, policy):
                topup["status"] = "paused_max"
                topup["paused_at"] = now
                levels.append(topup)
                changed = True
                break
            submitted = submit_grid_order_entry(exchange, coin, topup, now, row, asset, position_size, policy)
            if not submitted:
                changed = True
                break
            levels.append(topup)
            if grid_order_would_add_risk(position_size, bool(topup["is_buy"])):
                projected_position_value += order_notional
            topped_up += 1
            changed = True

    restored = 0
    for entry in levels:
        if not isinstance(entry, dict) or entry.get("side") is None or str(entry.get("status")) not in {"paused_max", "paused_margin"}:
            continue
        order_notional = Decimal(str(entry.get("size"))) * Decimal(str(entry.get("price", entry.get("limit_px"))))
        if not grid_order_allowed_by_max(position_size, projected_position_value, bool(entry.get("is_buy")), order_notional, max_position_value, policy):
            continue
        if active_price_too_close(row, str(entry["side"]), Decimal(str(entry.get("price", entry.get("limit_px"))))):
            continue
        if not submit_grid_order_entry(exchange, coin, entry, now, row, asset, position_size, policy):
            changed = True
            continue
        if grid_order_would_add_risk(position_size, bool(entry.get("is_buy"))):
            projected_position_value += order_notional
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
    row["open_oids"] = sorted(grid_batch_open_oids(row))
    row["note"] = (
        f"grid maintained; replacements={replacements}; topped_up={topped_up}; "
        f"paused={paused}; refreshed={refreshed}; deduped={deduped}; restored={restored}; trimmed={trimmed}; near_regrids={near_regrids}; "
        f"recovered_missing={recovered_missing}"
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

    keep_statuses = {"active", "pending", "paused_max", "paused_margin"}
    live_levels: list[dict[str, Any]] = []
    history_levels: list[dict[str, Any]] = []
    passthrough: list[Any] = []

    for entry in levels:
        if not isinstance(entry, dict) or not entry.get("side"):
            passthrough.append(entry)
            continue
        if str(entry.get("status", "active")) in keep_statuses:
            live_levels.append(entry)
        else:
            history_levels.append(entry)

    if len(history_levels) <= GRID_LEVEL_HISTORY_MAX:
        return False

    history_levels = sorted(history_levels, key=grid_level_updated_at)[-GRID_LEVEL_HISTORY_MAX:]
    row["levels"] = passthrough + live_levels + history_levels
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
    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("trail_worker: previous run still active, skipping")
            return
        run_once()


if __name__ == "__main__":
    main()
