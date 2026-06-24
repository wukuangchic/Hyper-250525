#!/usr/bin/env python3
"""Small Hyperliquid perp order helper.

Examples:
  ./hl_order.py query
  ./hl_order.py BTC
  ./hl_order.py BTC buy
  ./hl_order.py BTC buy 10 --market
  ./hl_order.py BTC buy --price 75000
  ./hl_order.py BTC both 100 --price 75000 --offset 2%
  ./hl_order.py BTC sell 25 --price 80000
  ./hl_order.py BTC sell 25 --stop 70000
  ./hl_order.py BTC sell 25 --stop 70000+50
  ./hl_order.py BTC buy 25 --take 70000
  ./hl_order.py BTC buy 25 --take 70000+0.2%
  ./hl_order.py BTC buy --tp 2%+0.1% --sl -2%-0.1%
  ./hl_order.py BTC --cancel
  ./hl_order.py BTC --cancel up
  ./hl_order.py BTC --cancel up --price 80000
  ./hl_order.py BTC --cancel hour --range 3 5
  ./hl_order.py BTC --cancel tp
  ./hl_order.py BTC --cancel 123456789
  ./hl_order.py BTC buy --dry-run
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import json
import os
import sys
import time
import traceback
from threading import Lock
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from pathlib import Path
from typing import Any, Optional

from simple_hyper.runtime import ensure_local_venv


ensure_local_venv(__file__)

from simple_hyper.formatting import (
    decimal_or_none,
    decimal_to_display,
    decimal_to_plain,
    format_leverage,
    format_optional_decimal,
    format_optional_percent,
    format_optional_price,
    format_optional_quantity,
    format_percent,
    format_price,
    format_short_timestamp_ms,
    format_signed_decimal,
    format_signed_percent,
    format_timestamp_ms,
    order_amount,
    print_box,
    print_table,
)
from simple_hyper.kline import KLINE_MODES, print_text_box, render_kline_chart
from simple_hyper.order_specs import (
    MIN_NOTIONAL,
    calc_market_size,
    calc_size,
    canonical_coin_input,
    coin_dex,
    coin_display_rate,
    ladder_for_prices,
    ladder_count_to_end_prices,
    ladder_while_prices,
    normalize_signed_option_values,
    parse_entry_trigger_with_limit,
    parse_side,
    parse_slippage,
    protect_ladder_step_values,
    resolve_ladder_step,
    resolve_perp_asset,
    resolve_tpsl_spec,
    rounded_perp_price,
    scale_order_size,
    scale_prices,
    side_code,
    unprotect_ladder_step_value,
    validate_tpsl_direction,
)
from simple_hyper.runtime import load_dotenv, mask

from coin_aliases import coin_alias_key


DEFAULT_SLIPPAGE = "0.05"
ISOLATED_FALLBACK_LEVERAGE = 5
SERVER_BATCH_PATH = Path(__file__).resolve().parent / "server_batch.json"
SERVER_BATCH_LOCK_PATH = Path(__file__).resolve().parent / "server_batch.lock"
SYMMETRIC_SIDE_ALIASES = {"both", "sym", "symmetric", "dual", "双向", "对称", "对称单"}
GRID_SIDE_ALIASES = {"grid", "网格", "网格单"}
DEFAULT_GRID_GAP_LABEL = ["auto-minTick", "auto-takerFee", "auto-makerFee"]
DEFAULT_GRID_RANGE = ["auto", "auto"]
GRID_TARGET_ORDERS_PER_SIDE = 10
GRID_ACCOUNT_MARGIN_RATIO_THRESHOLD = Decimal("0.70")
GRID_AVG_MAX_MULTIPLIER = Decimal("1.62")
CANCEL_AGE_FILTERS = {"hour", "day", "week"}
CANCEL_FILTERS = {"all", "up", "down", "buy", "sell", "tp", "sl", "trail", "grid"} | CANCEL_AGE_FILTERS
CANCEL_AGE_UNIT_MS = {
    "hour": 60 * 60 * 1000,
    "day": 24 * 60 * 60 * 1000,
    "week": 7 * 24 * 60 * 60 * 1000,
}
HISTORY_PNL_MAX_FILLS = 2000
HISTORY_PNL_FUNDING_PAGE_SIZE = 500
INFO_RETRY_STATUS_CODES = {502, 503, 504}
INFO_RETRY_DELAYS = (0.4, 1.0, 2.0)


def protect_grid_range_values(argv: list[str]) -> list[str]:
    if not any(token.strip().lower() in GRID_SIDE_ALIASES for token in argv):
        return argv
    protected: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--range" and index + 2 < len(argv):
            protected.extend([token, f"{argv[index + 1]},{argv[index + 2]}"])
            index += 3
            continue
        protected.append(token)
        index += 1
    return protected


def unprotect_grid_range_value(value: str) -> str:
    return value[1:] if value.startswith("=") else value


class RunLogger:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.path = self._make_path()

    def _make_path(self) -> Path:
        logs_dir = Path(__file__).resolve().parent / "logs"
        logs_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        return logs_dir / f"order-{timestamp}.log"

    def write(self, label: str, value: Any) -> None:
        with self.path.open("a") as handle:
            handle.write(f"\n## {label}\n")
            if isinstance(value, str):
                handle.write(value)
            else:
                handle.write(json.dumps(value, ensure_ascii=False, default=str, indent=2))
            handle.write("\n")

    def init(self) -> None:
        self.write("argv", self.argv)


LOGGER: RunLogger | None = None


def log_event(label: str, value: Any) -> None:
    if LOGGER is not None:
        LOGGER.write(label, value)


class CachedInfo:
    def __init__(self, info: Info) -> None:
        self._info = info
        self._cache: dict[tuple[Any, ...], Any] = {}
        self._lock = Lock()

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def _cached(self, key: tuple[Any, ...], loader: Any) -> Any:
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        value = self._load_with_retries(key, loader)
        with self._lock:
            self._cache[key] = value
        return value

    def _load_with_retries(self, key: tuple[Any, ...], loader: Any) -> Any:
        attempts = len(INFO_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                return loader()
            except Exception as exc:
                status_code = getattr(exc, "status_code", None)
                if status_code not in INFO_RETRY_STATUS_CODES or attempt >= len(INFO_RETRY_DELAYS):
                    raise
                delay = INFO_RETRY_DELAYS[attempt]
                log_event(
                    "info_retry",
                    {
                        "key": key,
                        "attempt": attempt + 1,
                        "status_code": status_code,
                        "delay_seconds": delay,
                        "message": str(exc),
                    },
                )
                time.sleep(delay)

    def user_role(self, address: str) -> Any:
        return self._cached(("user_role", address), lambda: self._info.user_role(address))

    def perp_dexs(self) -> Any:
        return self._cached(("perp_dexs",), lambda: self._info.perp_dexs())

    def spot_user_state(self, account: str) -> Any:
        return self._cached(("spot_user_state", account), lambda: self._info.spot_user_state(account))

    def user_state(self, account: str, dex: str = "") -> Any:
        return self._cached(("user_state", account, dex), lambda: self._info.user_state(account, dex=dex))

    def meta(self, dex: str = "") -> Any:
        return self._cached(("meta", dex), lambda: self._info.meta(dex=dex))

    def meta_and_asset_ctxs(self, dex: str = "") -> Any:
        if dex:
            return self._cached(
                ("meta_and_asset_ctxs", dex),
                lambda: self._info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex}),
            )
        return self._cached(("meta_and_asset_ctxs", dex), lambda: self._info.meta_and_asset_ctxs())

    def all_mids(self, dex: str = "") -> Any:
        return self._cached(("all_mids", dex), lambda: self._info.all_mids(dex))

    def l2_snapshot(self, name: str) -> Any:
        return self._cached(("l2_snapshot", name), lambda: self._info.l2_snapshot(name))

    def candles_snapshot(self, name: str, interval: str, startTime: int, endTime: int) -> Any:
        return self._cached(
            ("candles_snapshot", name, interval, startTime, endTime),
            lambda: self._info.candles_snapshot(name, interval, startTime, endTime),
        )

    def open_orders(self, account: str, dex: str = "") -> Any:
        return self._cached(("open_orders", account, dex), lambda: self._info.open_orders(account, dex=dex))

    def frontend_open_orders(self, account: str, dex: str = "") -> Any:
        return self._cached(
            ("frontend_open_orders", account, dex),
            lambda: self._info.frontend_open_orders(account, dex=dex),
        )

    def query_order_by_oid(self, account: str, oid: int) -> Any:
        return self._cached(
            ("query_order_by_oid", account, oid),
            lambda: self._info.query_order_by_oid(account, oid),
        )

    def user_fills(self, account: str) -> Any:
        return self._cached(("user_fills", account), lambda: self._info.user_fills(account))

    def user_fills_by_time(
        self, account: str, start_time: int, end_time: Optional[int] = None, aggregate_by_time: Optional[bool] = False
    ) -> Any:
        return self._cached(
            ("user_fills_by_time", account, start_time, end_time, aggregate_by_time),
            lambda: self._info.user_fills_by_time(account, start_time, end_time, aggregate_by_time),
        )

    def user_fees(self, account: str) -> Any:
        return self._cached(("user_fees", account), lambda: self._info.user_fees(account))

    def user_funding_history(self, user: str, startTime: int, endTime: Optional[int] = None) -> Any:
        return self._cached(
            ("user_funding_history", user, startTime, endTime),
            lambda: self._info.user_funding_history(user, startTime, endTime),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._info, name)


def clear_info_cache(info: Any) -> None:
    clear = getattr(info, "clear_cache", None)
    if callable(clear):
        clear()


def fetch_user_fills_window(info: Info, account: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    fills = info.user_fills_by_time(account, start_ms, end_ms)
    log_event(
        "user_fills_by_time",
        {
            "startTime": start_ms,
            "endTime": end_ms,
            "count": len(fills),
        },
    )
    return fills


def fetch_user_fills_for_history_pnl(info: Info, account: str, coin: str | None = None) -> list[dict[str, Any]]:
    try:
        recent_fills = info.user_fills(account)
    except Exception as exc:
        log_event(
            "history_pnl_fills_error",
            {"coin": coin, "type": type(exc).__name__, "message": str(exc)},
        )
        recent_fills = []

    fills = list(recent_fills[:HISTORY_PNL_MAX_FILLS])
    fills.sort(key=lambda item: int(item.get("time", 0)))
    log_event(
        "history_pnl_fills",
        {
            "coin": coin,
            "count": len(fills),
            "maxFills": HISTORY_PNL_MAX_FILLS,
        },
    )
    return fills


def fetch_user_funding_for_history_pnl(
    info: Info,
    account: str,
    coin: str | None = None,
    start_ms: int | None = None,
) -> list[dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    if start_ms is None:
        recent_fills = fetch_user_fills_for_history_pnl(info, account)
        start_ms = min((int(fill.get("time", now_ms)) for fill in recent_fills), default=now_ms)
    seen: set[tuple[int, str, str, str]] = set()
    rows: list[dict[str, Any]] = []
    cursor_ms = start_ms

    while cursor_ms <= now_ms:
        try:
            window_rows = info.user_funding_history(account, cursor_ms, now_ms)
        except Exception as exc:
            log_event(
                "history_pnl_funding_error",
                {"coin": coin, "startTime": cursor_ms, "endTime": now_ms, "type": type(exc).__name__, "message": str(exc)},
            )
            break

        for row in window_rows:
            delta = row.get("delta") or {}
            funding_coin = str(delta.get("coin", ""))
            if coin is not None and not fill_matches_coin(funding_coin, coin):
                continue
            row_key = (
                int(row.get("time", 0)),
                str(row.get("hash", "")),
                funding_coin,
                str(delta.get("usdc", "")),
            )
            if row_key in seen:
                continue
            seen.add(row_key)
            rows.append(row)

        if len(window_rows) < HISTORY_PNL_FUNDING_PAGE_SIZE:
            break
        latest_time = max((int(row.get("time", 0)) for row in window_rows), default=cursor_ms)
        next_cursor_ms = latest_time + 1
        if next_cursor_ms <= cursor_ms:
            break
        cursor_ms = next_cursor_ms

    rows.sort(key=lambda item: int(item.get("time", 0)))
    log_event(
        "history_pnl_funding",
        {
            "coin": coin,
            "count": len(rows),
            "startTime": start_ms,
        },
    )
    return rows


def same_side_book_price(info: Info, coin: str, is_buy: bool, level: int) -> Decimal:
    if level < 1:
        raise ValueError("--level must be >= 1")

    book = info.l2_snapshot(coin)
    log_event("l2_snapshot", book)
    side_index = 0 if is_buy else 1
    side_name = "bid" if is_buy else "ask"
    side_levels = book["levels"][side_index]
    if len(side_levels) < level:
        raise ValueError(f"{coin} only has {len(side_levels)} {side_name} levels, cannot use level {level}")
    return Decimal(side_levels[level - 1]["px"])


def resolve_account(info: Info, configured_account: str, signer_address: str) -> tuple[str, dict[str, Any]]:
    role = info.user_role(configured_account)
    if role.get("role") == "agent":
        return role["data"]["user"], role
    if role.get("role") == "user":
        return configured_account, role

    signer_role = info.user_role(signer_address)
    if signer_role.get("role") == "agent":
        return signer_role["data"]["user"], signer_role
    return configured_account, role


def build_clients(network: str, timeout: float, raw_coin: str, need_exchange: bool = True) -> tuple[Any, Any | None, str, str, dict[str, Any]]:
    import eth_account
    from hyperliquid.utils import constants

    env = load_dotenv()
    secret_key = env.get("secret_key")
    account_address = env.get("account_address")
    if not secret_key or not account_address:
        raise ValueError(".env must contain secret_key and account_address")

    wallet = eth_account.Account.from_key(secret_key)
    base_url = constants.TESTNET_API_URL if network == "testnet" else constants.MAINNET_API_URL
    dex = coin_dex(raw_coin)
    perp_dexs = ["", dex] if dex else None

    if need_exchange:
        from hyperliquid.exchange import Exchange

        exchange = Exchange(wallet, base_url, account_address=account_address, timeout=timeout, perp_dexs=perp_dexs)
        raw_info = exchange.info
    else:
        from hyperliquid.info import Info

        exchange = None
        raw_info = Info(base_url, skip_ws=True, timeout=timeout, perp_dexs=perp_dexs)

    info: CachedInfo = CachedInfo(raw_info)
    main_account, role = resolve_account(info, account_address, wallet.address)
    if exchange is not None:
        exchange.account_address = main_account
    log_event(
        "context",
        {
            "network": network,
            "base_url": base_url,
            "configured_account": mask(account_address),
            "main_account": mask(main_account),
            "signer": mask(wallet.address),
            "role": role,
            "perp_dexs": perp_dexs,
        },
    )
    return info, exchange, main_account, wallet.address, role


def all_dex_names(info: Info) -> list[str]:
    names = [""]
    for item in info.perp_dexs()[1:]:
        if isinstance(item, dict) and item.get("name"):
            names.append(item["name"])
    return names


def unified_account_metrics(
    info: Info,
    account: str,
) -> tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    spot_state = info.spot_user_state(account)
    log_event("spot_state", spot_state)
    spot_totals: dict[int, Decimal] = {}
    skipped_balances = []
    for balance in spot_state.get("balances", []):
        token = balance.get("token")
        coin = str(balance.get("coin", ""))
        if token is None and coin.startswith("+") and coin[1:].isdigit():
            token = coin[1:]
        if token is None:
            skipped_balances.append(balance)
            continue
        spot_totals[int(token)] = Decimal(str(balance.get("total", "0")))
    if skipped_balances:
        log_event("spot_balances_without_token", skipped_balances)

    available_after_maintenance: dict[int, Decimal] = {}
    for item in spot_state.get("tokenToAvailableAfterMaintenance", []):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            available_after_maintenance[int(item[0])] = Decimal(str(item[1]))
        except (TypeError, ValueError):
            continue

    usdc_total = spot_totals.get(0, Decimal("0"))
    usdc_available = available_after_maintenance.get(0)
    if usdc_total <= 0:
        account_safety_margin_ratio = Decimal("0")
    elif usdc_available is None:
        account_safety_margin_ratio = None
    else:
        account_safety_margin_ratio = max(Decimal("0"), usdc_available) / usdc_total

    maintenance_by_token: dict[int, Decimal] = {}
    notional_by_token: dict[int, Decimal] = {}
    for dex in all_dex_names(info):
        state = info.user_state(account, dex=dex)
        log_event(f"user_state:{dex or 'default'}", state)
        maintenance = Decimal(str(state.get("crossMaintenanceMarginUsed", "0")))
        notional = Decimal(str(state.get("crossMarginSummary", {}).get("totalNtlPos", "0")))
        if maintenance == 0 and notional == 0:
            continue
        collateral_token = int(info.meta(dex=dex).get("collateralToken", 0))
        maintenance_by_token[collateral_token] = maintenance_by_token.get(collateral_token, Decimal("0")) + maintenance
        notional_by_token[collateral_token] = notional_by_token.get(collateral_token, Decimal("0")) + notional

    ratios = []
    for token, maintenance in maintenance_by_token.items():
        collateral = spot_totals.get(token, Decimal("0"))
        if collateral > 0:
            ratios.append(maintenance / collateral)
        elif maintenance > 0:
            ratios.append(None)

    if not ratios:
        unified_ratio = Decimal("0")
    elif any(ratio is None for ratio in ratios):
        unified_ratio = None
    else:
        unified_ratio = max(ratios)

    active_tokens = {token for token, notional in notional_by_token.items() if notional > 0}
    total_notional = sum((notional_by_token[token] for token in active_tokens), Decimal("0"))
    total_collateral = sum((spot_totals.get(token, Decimal("0")) for token in active_tokens), Decimal("0"))
    if total_notional == 0:
        unified_leverage = Decimal("0")
    elif total_collateral > 0:
        unified_leverage = total_notional / total_collateral
    else:
        unified_leverage = None

    metrics = {
        "maintenance_by_token": maintenance_by_token,
        "notional_by_token": notional_by_token,
        "spot_totals": spot_totals,
        "unified_ratio": unified_ratio,
        "unified_leverage": unified_leverage,
        "account_safety_margin_ratio": account_safety_margin_ratio,
    }
    log_event("unified_metrics", metrics)
    return unified_ratio, unified_leverage, account_safety_margin_ratio


def print_account_metrics(info: Info, account: str) -> None:
    unified_ratio, unified_leverage, account_safety_margin_ratio = unified_account_metrics(info, account)
    protection_status = "未知"
    if account_safety_margin_ratio is not None:
        protection_status = "开启" if account_safety_margin_ratio < GRID_ACCOUNT_MARGIN_RATIO_THRESHOLD else "关闭"
    print_box(
        "Account",
        [
            ("账户安全余量率", format_percent(account_safety_margin_ratio)),
            ("Grid保护(<70%)", protection_status),
            ("统一账户比率", format_percent(unified_ratio)),
            ("统一账户杠杆", format_leverage(unified_leverage)),
        ],
    )


def print_order_row(
    coin: str,
    side: str,
    mid_px: Decimal | str | None,
    limit_px: Decimal | str,
    amount: Decimal | str,
    price_rate: Decimal | None = None,
) -> None:
    rows = [
        ("coin", coin),
        ("side", side),
    ]
    if mid_px is not None:
        rows.append(("midPx", format_price(mid_px, price_rate)))
    rows.extend(
        [
            ("limitPx", decimal_to_display(limit_px)),
            ("amount", decimal_to_display(amount)),
        ]
    )
    print_box(
        "Order",
        rows,
    )


def print_market_order_row(
    coin: str,
    side: str,
    reference_price: Decimal | str,
    limit_px: Decimal | str,
    orig_sz: Decimal | str,
    slippage: Decimal,
    reference_notional: Decimal | str,
    worst_notional: Decimal | str,
    price_rate: Decimal | None = None,
) -> None:
    print_box(
        "Market Order",
        [
            ("coin", coin),
            ("side", side),
            ("referencePx", format_price(reference_price, price_rate)),
            ("iocLimitPx", format_price(limit_px, price_rate)),
            ("slippage", format_percent(slippage)),
            ("sz", format_optional_quantity(orig_sz)),
            ("referenceNtl", decimal_to_display(reference_notional)),
            ("worstNtl", decimal_to_display(worst_notional)),
        ],
    )


def compact_status(status: dict[str, Any] | None) -> str:
    if status is None:
        return "planned"
    if "error" in status:
        text = f"error: {status['error']}"
        return text if len(text) <= 80 else text[:77] + "..."
    if "resting" in status:
        return f"resting #{status['resting'].get('oid', 'n/a')}"
    if "filled" in status:
        filled = status["filled"]
        avg_px = format_optional_decimal(filled.get("avgPx"))
        oid = filled.get("oid", "n/a")
        return f"filled #{oid} @ {avg_px}"
    return str(status)


def format_rate_percent(value: Any, decimals: int = 4) -> str:
    decimal = decimal_or_none(value)
    if decimal is None:
        return "n/a"
    quant = Decimal(1).scaleb(-decimals)
    percent = (decimal * Decimal("100")).quantize(quant, rounding=ROUND_HALF_UP)
    return f"{percent}%"


def order_plan_request(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "coin": plan["coin"],
        "is_buy": plan["is_buy"],
        "sz": float(plan["size"]),
        "limit_px": float(plan["limit_px"]),
        "order_type": plan["order_type"],
        "reduce_only": plan["reduce_only"],
    }


def order_plan_table_row(
    plan: dict[str, Any],
    price_rate: Decimal | None,
    status: dict[str, Any] | None = None,
) -> dict[str, str]:
    trigger_px = plan.get("trigger_px")
    return {
        "label": str(plan["label"]),
        "coin": str(plan["coin"]),
        "side": side_code(bool(plan["is_buy"])),
        "mode": str(plan["mode"]),
        "triggerPx": format_price(trigger_px, price_rate) if trigger_px is not None else "-",
        "limitPx": format_price(plan["limit_px"], price_rate),
        "sz": decimal_to_plain(plan["size"]),
        "amount": decimal_to_display(plan["notional"]),
        "reduce": "1" if plan["reduce_only"] else "0",
        "status": compact_status(status),
    }


def print_order_plan_table(
    title: str,
    plans: list[dict[str, Any]],
    price_rate: Decimal | None = None,
    statuses: list[dict[str, Any]] | None = None,
) -> None:
    rows = [
        order_plan_table_row(plan, price_rate, statuses[index] if statuses and index < len(statuses) else None)
        for index, plan in enumerate(plans)
    ]
    print_table(
        title,
        rows,
        [
            ("label", "label"),
            ("coin", "coin"),
            ("side", "side"),
            ("mode", "mode"),
            ("triggerPx", "triggerPx"),
            ("limitPx", "limitPx"),
            ("sz", "sz"),
            ("amount", "amount"),
            ("reduce", "reduce"),
            ("status", "status"),
        ],
        show_count=False,
    )


def print_explain(title: str, plans: list[dict[str, Any]], args: argparse.Namespace, price_rate: Decimal | None = None) -> None:
    entry_plans = [plan for plan in plans if not plan.get("reduce_only")] or plans
    entry_prices = [Decimal(str(plan.get("reference_price", plan["limit_px"]))) for plan in entry_plans]
    total_entry_notional = sum((Decimal(str(plan["notional"])) for plan in entry_plans), Decimal("0"))
    amount_mode = "total" if getattr(args, "amount_is_total", False) else "per-order"
    if args.scale:
        amount_mode = "total"
    if getattr(args, "grid", False):
        amount_mode = "one-way total"

    rows = [
        ("submit", "0"),
        ("title", title),
        ("side", str(args.side or "-")),
        ("amount_mode", amount_mode),
        ("amount", decimal_to_plain(Decimal(str(args.amount)))),
        ("entry_legs", str(len(entry_plans))),
        ("all_orders", str(len(plans))),
        ("entry_notional", decimal_to_display(total_entry_notional)),
        ("price_range", f"{format_price(min(entry_prices), price_rate)} -> {format_price(max(entry_prices), price_rate)}"),
        ("reduce_only", "1" if args.reduce_only else "0"),
    ]
    if args.ladder_mode:
        if args.ladder_step:
            rows.append(("ladder", f"{args.ladder_mode} {args.ladder_end or args.ladder_count} {args.ladder_step}"))
        else:
            rows.append(("ladder", f"while {args.ladder_end} for {args.ladder_count}"))
    if args.range_spec:
        rows.append(("range", " ".join(args.range_spec)))
    if getattr(args, "grid", False):
        if not args.range_spec:
            rows.append(("range", " ".join(grid_range_spec(args))))
        rows.append(("gap", " ".join(grid_gap_spec(args))))
        rows.append(("trend", args.trend or "0"))
        if getattr(args, "grid_avg", None) is not None:
            rows.append(("avg", str(args.grid_avg)))
        if getattr(args, "resolved_grid_trend", None):
            rows.append(("actual_trend", args.resolved_grid_trend))
    if getattr(args, "symmetric", False):
        rows.append(("symmetric", f"offset {args.symmetric_offset}"))
    if args.take_profit:
        rows.append(("tp", args.take_profit))
    if args.stop_loss:
        rows.append(("sl", args.stop_loss))
    print_box("Explain", rows)
    print_order_plan_table(title, plans, price_rate)


def print_filled_row(
    coin: str,
    side: str,
    filled: dict[str, Any],
    fallback_size: Decimal | str,
    price_rate: Decimal | None = None,
) -> None:
    print_box(
        "Filled",
        [
            ("coin", coin),
            ("side", side),
            ("oid", str(filled.get("oid", "n/a"))),
            ("avgPx", format_optional_price(filled.get("avgPx"), price_rate)),
            ("totalSz", format_optional_quantity(filled.get("totalSz", fallback_size))),
        ],
    )


def format_position_side(size: Decimal) -> str:
    if size > 0:
        return "long"
    if size < 0:
        return "short"
    return "flat"


def format_position_leverage(position: dict[str, Any]) -> str:
    leverage = position.get("leverage") or {}
    value = leverage.get("value")
    if value is None:
        return "n/a"
    leverage_type = leverage.get("type")
    suffix = f" {leverage_type}" if leverage_type else ""
    return f"{value}x{suffix}"


def position_matches_coin(position_coin: str, coin: str) -> bool:
    return coin_alias_key(position_coin) == coin_alias_key(coin)


def fill_matches_coin(fill_coin: str, coin: str) -> bool:
    return canonical_coin_input(fill_coin).upper() == canonical_coin_input(coin).upper()


def collect_frontend_open_orders(info: Info, account: str, dex: str) -> list[dict[str, Any]]:
    try:
        raw_orders = info.frontend_open_orders(account, dex=dex)
        log_event(f"frontend_open_orders:{dex or 'default'}", raw_orders)
    except Exception as exc:
        log_event(f"frontend_open_orders_error:{dex or 'default'}", {"type": type(exc).__name__, "message": str(exc)})
        raw_orders = info.open_orders(account, dex=dex)
        log_event(f"open_orders_fallback:{dex or 'default'}", raw_orders)

    rows: list[dict[str, Any]] = []
    seen_oids: set[int] = set()

    def visit(order: dict[str, Any]) -> None:
        oid = order.get("oid")
        if oid is not None:
            oid_int = int(oid)
            if oid_int in seen_oids:
                return
            seen_oids.add(oid_int)
        rows.append(order)
        for child in order.get("children") or []:
            if isinstance(child, dict):
                visit(child)

    for order in raw_orders:
        if isinstance(order, dict):
            visit(order)
    return rows


def format_open_order_type(order: dict[str, Any]) -> str:
    if order.get("isPositionTpsl"):
        return "positionTpsl"
    order_type = order.get("orderType")
    if order_type:
        return str(order_type)
    if order.get("isTrigger"):
        return "trigger"
    tif = order.get("tif")
    return str(tif) if tif else "limit"


def format_open_order_trigger_price(order: dict[str, Any]) -> str:
    trigger_px = decimal_or_none(order.get("triggerPx"))
    if trigger_px is None or trigger_px == 0:
        return "-"
    return decimal_to_display(trigger_px)


def format_open_order_value(order: dict[str, Any]) -> str:
    limit_px = decimal_or_none(order.get("limitPx"))
    if limit_px is None or limit_px == 0:
        limit_px = decimal_or_none(order.get("triggerPx"))
    size = decimal_or_none(order.get("sz", order.get("origSz")))
    value = order_amount(limit_px, size) if limit_px is not None and size is not None else None
    return format_optional_decimal(value)


def open_order_cancel_price(order: dict[str, Any]) -> Decimal | None:
    if order.get("isTrigger"):
        trigger_px = decimal_or_none(order.get("triggerPx"))
        if trigger_px is not None and trigger_px > 0:
            return trigger_px
    limit_px = decimal_or_none(order.get("limitPx"))
    if limit_px is not None and limit_px > 0:
        return limit_px
    trigger_px = decimal_or_none(order.get("triggerPx"))
    return trigger_px if trigger_px is not None and trigger_px > 0 else None


def open_order_timestamp_ms(order: dict[str, Any]) -> int | None:
    timestamp = order.get("timestamp")
    if timestamp is None:
        return None
    try:
        timestamp_ms = int(timestamp)
    except (TypeError, ValueError):
        return None
    return timestamp_ms if timestamp_ms > 0 else None


def open_order_age_ms(order: dict[str, Any], now_ms: int) -> int | None:
    timestamp_ms = open_order_timestamp_ms(order)
    if timestamp_ms is None:
        return None
    return max(0, now_ms - timestamp_ms)


def open_order_side_matches(order: dict[str, Any], side: str) -> bool:
    normalized = str(order.get("side", "")).strip().lower()
    if side == "buy":
        return normalized in {"b", "buy", "bid", "long"}
    return normalized in {"a", "sell", "ask", "short"}


def open_order_tpsl_kind(order: dict[str, Any]) -> str | None:
    values = [
        order.get("tpsl"),
        order.get("orderType"),
        order.get("type"),
        order.get("triggerCondition"),
    ]
    text = " ".join(str(value).strip().lower() for value in values if value is not None)
    if "take profit" in text or text.split() == ["tp"] or " t/p" in text or "tp " in f"{text} ":
        return "tp"
    if "take market" in text or "take limit" in text:
        return "tp"
    if "stop loss" in text or text.split() == ["sl"] or " s/l" in text or "sl " in f"{text} ":
        return "sl"
    if "stop market" in text or "stop limit" in text:
        return "sl"
    return None


def cancel_filter_label(cancel_arg: str) -> str:
    return "all" if cancel_arg == "all" else cancel_arg


def format_cancel_age_range(unit: str, age_range: tuple[Decimal, Decimal | None]) -> str:
    start, end = age_range
    if end is None:
        return f">= {decimal_to_plain(start)} {unit}"
    return f"{decimal_to_plain(start)}-{decimal_to_plain(end)} {unit}"


def parse_cancel_age_range(values: list[str] | None) -> tuple[Decimal, Decimal | None]:
    if not values:
        return Decimal(1), None
    if len(values) not in {1, 2}:
        raise ValueError("--range for --cancel hour/day/week requires one or two numbers")
    try:
        parsed = [Decimal(value) for value in values]
    except InvalidOperation as exc:
        raise ValueError("--range for --cancel hour/day/week must contain valid decimals") from exc
    if any(not value.is_finite() or value < 0 for value in parsed):
        raise ValueError("--range for --cancel hour/day/week must be non-negative")
    start = parsed[0]
    end = parsed[1] if len(parsed) == 2 else None
    if end is not None and end <= start:
        raise ValueError("--range END must be greater than START for --cancel hour/day/week")
    return start, end


def select_cancel_orders(
    open_orders: list[dict[str, Any]],
    coin: str,
    cancel_arg: str,
    threshold_price: Decimal | None,
    age_range_ms: tuple[int, int | None] | None = None,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    coin_orders = [order for order in open_orders if position_matches_coin(str(order.get("coin", "")), coin)]
    filter_arg = cancel_arg.strip().lower()
    if filter_arg == "all":
        return coin_orders
    if filter_arg == "up":
        if threshold_price is None:
            raise ValueError(f"current mid is unavailable for {coin}; cannot use --cancel up")
        return [order for order in coin_orders if (price := open_order_cancel_price(order)) is not None and price > threshold_price]
    if filter_arg == "down":
        if threshold_price is None:
            raise ValueError(f"current mid is unavailable for {coin}; cannot use --cancel down")
        return [order for order in coin_orders if (price := open_order_cancel_price(order)) is not None and price < threshold_price]
    if filter_arg in {"buy", "sell"}:
        return [order for order in coin_orders if open_order_side_matches(order, filter_arg)]
    if filter_arg in {"tp", "sl"}:
        return [order for order in coin_orders if open_order_tpsl_kind(order) == filter_arg]
    if filter_arg in CANCEL_AGE_FILTERS:
        if age_range_ms is None or now_ms is None:
            raise ValueError(f"age range is unavailable for {coin}; cannot use --cancel {filter_arg}")
        start_ms, end_ms = age_range_ms
        if end_ms is None:
            return [
                order
                for order in coin_orders
                if (age_ms := open_order_age_ms(order, now_ms)) is not None and age_ms >= start_ms
            ]
        return [
            order
            for order in coin_orders
            if (age_ms := open_order_age_ms(order, now_ms)) is not None and start_ms <= age_ms <= end_ms
        ]

    oid = int(cancel_arg)
    return [order for order in coin_orders if int(order.get("oid")) == oid]


def find_current_position(info: Info, account: str, coin: str, dex: str) -> dict[str, Any] | None:
    state = info.user_state(account, dex=dex)
    log_event(f"market_user_state:{dex or 'default'}", state)
    for item in state.get("assetPositions", []):
        position = item.get("position", {})
        if not position_matches_coin(str(position.get("coin", "")), coin):
            continue
        size = Decimal(str(position.get("szi", "0")))
        if size != 0:
            return position
    return None


def market_asset_context(info: Info, coin: str, dex: str) -> dict[str, Any]:
    try:
        meta, asset_ctxs = info.meta_and_asset_ctxs(dex)
        log_event(f"meta_and_asset_ctxs:{dex or 'default'}", {"meta": meta, "assetCtxs": asset_ctxs})
    except Exception as exc:
        log_event(f"meta_and_asset_ctxs_error:{dex or 'default'}", {"type": type(exc).__name__, "message": str(exc)})
        return {}

    universe = meta.get("universe", []) if isinstance(meta, dict) else []
    for index, asset in enumerate(universe):
        if not position_matches_coin(str(asset.get("name", "")), coin):
            continue
        if index < len(asset_ctxs) and isinstance(asset_ctxs[index], dict):
            return asset_ctxs[index]
        return {}
    return {}


def account_fee_info(info: Info, account: str) -> dict[str, Any]:
    try:
        fees = info.user_fees(account)
        log_event("user_fees", fees)
    except Exception as exc:
        log_event("user_fees_error", {"type": type(exc).__name__, "message": str(exc)})
        return {}
    return fees if isinstance(fees, dict) else {}


def perp_dex_config(info: Info, dex: str) -> dict[str, Any]:
    if not dex:
        return {}
    try:
        dexs = info.perp_dexs()
        log_event("perp_dexs", dexs)
    except Exception as exc:
        log_event("perp_dexs_error", {"type": type(exc).__name__, "message": str(exc)})
        return {}
    for item in dexs:
        if isinstance(item, dict) and str(item.get("name", "")).lower() == dex.lower():
            return item
    return {}


def effective_perp_fee_rates(
    info: Info,
    account: str,
    asset: dict[str, Any],
    dex: str,
) -> dict[str, Decimal | None]:
    fees = account_fee_info(info, account)
    maker_base = decimal_or_none(fees.get("userAddRate"))
    taker_base = decimal_or_none(fees.get("userCrossRate"))
    referral_discount = decimal_or_none(fees.get("activeReferralDiscount")) or Decimal("0")

    dex_config = perp_dex_config(info, dex)
    deployer_fee_scale = decimal_or_none(dex_config.get("deployerFeeScale")) if dex_config else None
    if deployer_fee_scale is None:
        hip3_scale = Decimal("1")
    elif deployer_fee_scale < 1:
        hip3_scale = deployer_fee_scale + Decimal("1")
    else:
        hip3_scale = deployer_fee_scale * Decimal("2")

    growth_mode = str(asset.get("growthMode", "")).strip().lower() == "enabled"
    growth_mode_scale = Decimal("0.1") if growth_mode else Decimal("1")
    discount_scale = Decimal("1") - referral_discount

    maker_effective = None
    if maker_base is not None:
        maker_effective = maker_base * growth_mode_scale
        if maker_effective > 0:
            maker_effective *= hip3_scale * discount_scale

    taker_effective = None
    if taker_base is not None:
        taker_effective = taker_base * hip3_scale * growth_mode_scale * discount_scale

    return {
        "maker_base": maker_base,
        "taker_base": taker_base,
        "maker_effective": maker_effective,
        "taker_effective": taker_effective,
    }


def format_fee_rate(effective: Decimal | None, base: Decimal | None) -> str:
    if effective is None:
        return "n/a"
    text = format_rate_percent(effective)
    if base is not None and effective != base:
        text = f"{text} (base {format_rate_percent(base)})"
    return text


def format_history_result(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    if value > 0:
        return "profit"
    if value < 0:
        return "loss"
    return "flat"


def fill_signed_size(fill: dict[str, Any]) -> Decimal | None:
    size = decimal_or_none(fill.get("sz"))
    if size is None:
        return None
    side = str(fill.get("side", "")).strip().upper()
    if side == "B":
        return size
    if side == "A":
        return -size

    direction = str(fill.get("dir", "")).strip().lower()
    if "long" in direction:
        return size if "open" in direction else -size
    if "short" in direction:
        return -size if "open" in direction else size
    return None


def history_pnl_window_hours(fills: list[dict[str, Any]], now_ms: int | None = None) -> int | None:
    if not fills:
        return None
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    earliest_ms = min(int(fill.get("time", current_ms)) for fill in fills)
    elapsed_ms = max(0, current_ms - earliest_ms)
    hour_ms = 60 * 60 * 1000
    return max(1, (elapsed_ms + hour_ms - 1) // hour_ms)


def real_pnl_label(window_hours: int | None) -> str:
    return f"realPnl({window_hours}H)" if window_hours is not None else "realPnl"


def calculate_history_pnl(
    info: Info,
    account: str,
    coin: str,
    mark_px: Decimal | None = None,
    unrealized_pnl: Decimal | None = None,
    fills: list[dict[str, Any]] | None = None,
    funding_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Decimal | int | bool | None]:
    source_fills = fills if fills is not None else fetch_user_fills_for_history_pnl(info, account)
    fills = [
        fill
        for fill in source_fills
        if fill_matches_coin(str(fill.get("coin", "")), coin)
    ]
    funding_start_ms = min((int(fill.get("time", 0)) for fill in source_fills), default=int(time.time() * 1000))
    funding_rows = [
        row
        for row in (
            funding_rows
            if funding_rows is not None
            else fetch_user_funding_for_history_pnl(info, account, coin, start_ms=funding_start_ms)
        )
        if fill_matches_coin(str((row.get("delta") or {}).get("coin", "")), coin)
    ]
    realized = Decimal("0")
    fees = Decimal("0")
    funding = Decimal("0")
    position_size = Decimal("0")
    cost_basis = Decimal("0")
    has_cost_basis = True

    for fill in fills:
        closed_pnl = decimal_or_none(fill.get("closedPnl"))
        if closed_pnl is not None:
            realized += closed_pnl

        fee = decimal_or_none(fill.get("fee"))
        if fee is not None:
            fees += fee

        signed_size = fill_signed_size(fill)
        px = decimal_or_none(fill.get("px"))
        if signed_size is None or px is None:
            has_cost_basis = False
            continue
        if signed_size == 0:
            continue

        if position_size == 0 or (position_size > 0) == (signed_size > 0):
            position_size += signed_size
            cost_basis += signed_size * px
            continue

        if abs(signed_size) < abs(position_size):
            remaining_ratio = (abs(position_size) - abs(signed_size)) / abs(position_size)
            position_size += signed_size
            cost_basis *= remaining_ratio
        elif abs(signed_size) == abs(position_size):
            position_size = Decimal("0")
            cost_basis = Decimal("0")
        else:
            remainder = position_size + signed_size
            position_size = remainder
            cost_basis = remainder * px

    for row in funding_rows:
        delta = row.get("delta") or {}
        funding_usdc = decimal_or_none(delta.get("usdc"))
        if funding_usdc is not None:
            funding += funding_usdc

    avg_px = cost_basis / position_size if position_size != 0 and has_cost_basis else None
    open_pnl = unrealized_pnl
    if open_pnl is None and avg_px is not None and mark_px is not None:
        open_pnl = (mark_px - avg_px) * position_size

    real_pnl = realized - fees + funding
    if open_pnl is not None:
        real_pnl += open_pnl

    return {
        "fills": len(fills),
        "fundingRows": len(funding_rows),
        "realized": realized,
        "fees": fees,
        "funding": funding,
        "openSize": position_size if has_cost_basis else None,
        "avgPx": avg_px,
        "openPnl": open_pnl,
        "realPnl": real_pnl,
        "netPnl": real_pnl,
        "windowHours": history_pnl_window_hours(source_fills),
        "partial": len(source_fills) >= HISTORY_PNL_MAX_FILLS,
    }


def market_mark_price(info: Info, coin: str, dex: str) -> Decimal | None:
    mids = info.all_mids(dex)
    mid = mids.get(coin)
    if mid is not None:
        return Decimal(str(mid))
    for mid_coin, mid_value in mids.items():
        if position_matches_coin(str(mid_coin), coin):
            return Decimal(str(mid_value))
    asset_ctx = market_asset_context(info, coin, dex)
    return decimal_or_none(asset_ctx.get("markPx"))


def print_market_overview(
    info: Info,
    account: str,
    network: str,
    raw_coin: str,
    coin: str,
    dex: str,
    asset: dict[str, Any],
    price_rate: Decimal | None,
    kline_mode: str = "hour",
) -> None:
    mode_config = KLINE_MODES.get(kline_mode, KLINE_MODES["hour"])
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - mode_config["lookback_days"] * 24 * 60 * 60 * 1000
    asset_ctx = market_asset_context(info, coin, dex)
    latest_price = market_mark_price(info, coin, dex)
    if latest_price is None:
        latest_price = decimal_or_none(asset_ctx.get("markPx"))
    if latest_price is None:
        raise ValueError(f"No mark price found for {raw_coin}")

    candles: list[dict[str, Any]] = []
    candle_error: Exception | None = None
    try:
        candles = info.candles_snapshot(coin, mode_config["interval"], start_ms, end_ms)
        log_event(
            f"candles_{kline_mode}",
            {
                "coin": coin,
                "dex": dex or "default",
                "interval": mode_config["interval"],
                "count": len(candles),
                "candles": candles,
            },
        )
    except Exception as exc:
        candle_error = exc
        log_event(
            f"candles_{kline_mode}_error",
            {
                "coin": coin,
                "dex": dex or "default",
                "interval": mode_config["interval"],
                "type": type(exc).__name__,
                "message": str(exc),
            },
        )

    chart_candles = candles[-mode_config["candles"] :] if candles else []
    if chart_candles:
        open_price = Decimal(str(chart_candles[0]["o"]))
        notional_volume = sum(
            (Decimal(str(candle.get("v", "0"))) * Decimal(str(candle.get("c", "0"))) for candle in chart_candles),
            Decimal("0"),
        )
        change = latest_price - open_price
        change_percent = change / open_price if open_price else Decimal("0")
        if change > 0:
            trend = "up"
        elif change < 0:
            trend = "down"
        else:
            trend = "flat"
    else:
        notional_volume = None
        change = None
        change_percent = None
        trend = "n/a"

    fee_rates = effective_perp_fee_rates(info, account, asset=asset, dex=dex)
    rows = [
        ("coin", coin),
        (
            "trend",
            f"{trend} {format_signed_decimal(change)} ({format_signed_percent(change_percent)})"
            if change is not None and change_percent is not None
            else trend,
        ),
        ("latest", format_price(latest_price, price_rate)),
        ("markPx", format_optional_price(asset_ctx.get("markPx"), price_rate)),
        ("funding", format_rate_percent(asset_ctx.get("funding"))),
        ("premium", format_rate_percent(asset_ctx.get("premium"))),
        ("takerFee", format_fee_rate(fee_rates.get("taker_effective"), fee_rates.get("taker_base"))),
        ("makerFee", format_fee_rate(fee_rates.get("maker_effective"), fee_rates.get("maker_base"))),
        ("turnover", decimal_to_display(notional_volume) if notional_volume is not None else "n/a"),
    ]
    if candle_error is not None:
        rows.append(("chart", f"skipped: {type(candle_error).__name__}"))
    elif not chart_candles:
        rows.append(("chart", "skipped: no candle data"))
    print_box(mode_config["market_title"], rows)
    if chart_candles:
        chart_lines, chart_overlay = render_kline_chart(chart_candles, latest_price, kline_mode)
        print_text_box(mode_config["title"], chart_lines, chart_overlay)

    position = find_current_position(info, account, coin, dex)
    if position is not None:
        print_box(
            "Position",
            [
                ("side", format_position_side(Decimal(str(position.get("szi", "0"))))),
                ("entryPx", format_optional_decimal(position.get("entryPx"))),
                ("value", format_optional_decimal(position.get("positionValue"))),
                ("leverage", format_position_leverage(position)),
            ],
        )
    current_unrealized_pnl = (
        decimal_or_none(position.get("unrealizedPnl")) if position is not None else Decimal("0")
    )
    history_pnl = calculate_history_pnl(
        info,
        account,
        coin,
        mark_px=latest_price,
        unrealized_pnl=current_unrealized_pnl,
    )
    history_rows = [
        ("result", format_history_result(history_pnl.get("realPnl"))),
        ("realPnl", format_optional_decimal(history_pnl.get("realPnl"))),
    ]
    if history_pnl.get("partial"):
        history_rows.append(("note", f"limited to latest {HISTORY_PNL_MAX_FILLS} fills"))
    window_hours = history_pnl.get("windowHours")
    pnl_title = f"Real PnL ({window_hours}H)" if isinstance(window_hours, int) else "Real PnL"
    print_box(pnl_title, history_rows)
    batch_rows = load_server_batch()
    open_orders = collect_open_orders_for_coin(info, account, network, coin, dex, batch_rows)
    if open_orders:
        print_table(
            "Open Orders",
            open_orders,
            [
                ("coin", "coin"),
                ("side", "side"),
                ("type", "type"),
                ("triggerPx", "triggerPx"),
                ("limitPx", "limitPx"),
                ("value", "value"),
                ("oid", "oid"),
                ("time", "time"),
            ],
            show_count=False,
        )
    print_server_batch(batch_rows, network, account, coin)
    print_recent_history(info, account, coin=coin)


def collect_account_positions_and_orders(
    info: Info,
    account: str,
    network: str,
    batch_rows: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    positions: list[dict[str, str]] = []
    orders: list[dict[str, str]] = []
    seen_order_keys: set[tuple[str, int]] = set()
    dex_names = all_dex_names(info)
    batch_rows = batch_rows if batch_rows is not None else load_server_batch()
    server_batch_oids = active_server_batch_oids(batch_rows, network, account)
    history_fills = fetch_user_fills_for_history_pnl(info, account)
    history_start_ms = min(
        (int(fill.get("time", 0)) for fill in history_fills),
        default=int(time.time() * 1000),
    )
    history_funding = fetch_user_funding_for_history_pnl(info, account, start_ms=history_start_ms)
    log_event("query_dex_names", dex_names)

    for dex in dex_names:
        dex_name = dex or "default"
        state = info.user_state(account, dex=dex)
        log_event(f"query_user_state:{dex_name}", state)
        for item in state.get("assetPositions", []):
            position = item.get("position", {})
            size = Decimal(str(position.get("szi", "0")))
            if size == 0:
                continue
            position_coin = str(position.get("coin", ""))
            mark_px = market_mark_price(info, position_coin, dex)
            history_pnl = calculate_history_pnl(
                info,
                account,
                position_coin,
                mark_px=mark_px,
                unrealized_pnl=decimal_or_none(position.get("unrealizedPnl")),
                fills=history_fills,
                funding_rows=history_funding,
            )
            positions.append(
                {
                    "dex": dex_name,
                    "coin": position_coin,
                    "side": format_position_side(size),
                    "szi": decimal_to_plain(size),
                    "entryPx": format_optional_decimal(position.get("entryPx")),
                    "value": format_optional_decimal(position.get("positionValue")),
                    "nPnl": format_optional_decimal(position.get("unrealizedPnl")),
                    "realPnl": format_optional_decimal(history_pnl.get("realPnl")),
                    "pnlWindowHours": str(history_pnl.get("windowHours") or ""),
                    "result": format_history_result(history_pnl.get("realPnl")),
                    "roe": format_optional_percent(position.get("returnOnEquity")),
                    "liqPx": format_optional_decimal(position.get("liquidationPx")),
                    "lev": format_position_leverage(position),
                }
            )

        open_orders = collect_frontend_open_orders(info, account, dex)
        for order in open_orders:
            oid = int(order["oid"])
            if oid in server_batch_oids:
                continue
            order_key = (str(order.get("coin", "")), oid)
            if order_key in seen_order_keys:
                continue
            seen_order_keys.add(order_key)
            orders.append(
                {
                    "dex": dex_name,
                    "coin": str(order.get("coin", "")),
                    "side": str(order.get("side", "")),
                    "type": format_open_order_type(order),
                    "triggerPx": format_open_order_trigger_price(order),
                    "limitPx": format_optional_decimal(order.get("limitPx")),
                    "value": format_open_order_value(order),
                    "oid": str(oid),
                    "time": format_timestamp_ms(order.get("timestamp")),
                }
            )

    positions.sort(key=lambda row: (row["dex"], row["coin"], row["side"]))
    orders.sort(key=lambda row: (row["dex"], row["coin"], row["oid"]))
    return positions, orders


def collect_open_orders_for_coin(
    info: Info,
    account: str,
    network: str,
    coin: str,
    dex: str,
    batch_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    open_orders = collect_frontend_open_orders(info, account, dex)
    batch_rows = batch_rows if batch_rows is not None else load_server_batch()
    server_batch_oids = active_server_batch_oids(batch_rows, network, account, coin)
    rows: list[dict[str, str]] = []

    for order in open_orders:
        if not position_matches_coin(str(order.get("coin", "")), coin):
            continue
        oid = int(order.get("oid", -1))
        if oid in server_batch_oids:
            continue
        rows.append(
            {
                "coin": str(order.get("coin", "")),
                "side": str(order.get("side", "")),
                "type": format_open_order_type(order),
                "triggerPx": format_open_order_trigger_price(order),
                "limitPx": format_optional_decimal(order.get("limitPx")),
                "value": format_open_order_value(order),
                "oid": str(oid),
                "time": format_timestamp_ms(order.get("timestamp")),
            }
        )

    rows.sort(key=lambda row: row["oid"])
    return rows


def collect_recent_history(info: Info, account: str, coin: str | None = None, limit: int = 10) -> list[dict[str, str]]:
    now_ms = int(time.time() * 1000)
    windows_days = [7, 14, 30]
    seen: set[tuple[str, int, int, str, str, str, str]] = set()
    entries: list[tuple[int, dict[str, str]]] = []

    for days in windows_days:
        if len(entries) >= limit:
            break

        start_ms = now_ms - days * 24 * 60 * 60 * 1000
        try:
            fills = fetch_user_fills_window(info, account, start_ms, now_ms)
        except Exception as exc:
            log_event(
                "recent_history_fills_error",
                {"startTime": start_ms, "endTime": now_ms, "type": type(exc).__name__, "message": str(exc)},
            )
            break
        for fill in sorted(fills, key=lambda item: int(item.get("time", 0)), reverse=True):
            fill_coin = str(fill.get("coin", ""))
            if coin is not None and not fill_matches_coin(fill_coin, coin):
                continue

            fill_time = int(fill.get("time", 0))
            fill_key = (
                str(fill.get("hash", "")),
                int(fill.get("oid", -1)),
                fill_time,
                fill_coin,
                str(fill.get("side", "")),
                str(fill.get("px", "")),
                str(fill.get("sz", "")),
            )
            if fill_key in seen:
                continue
            seen.add(fill_key)

            px = decimal_or_none(fill.get("px"))
            sz = decimal_or_none(fill.get("sz"))
            value = px * sz if px is not None and sz is not None else None
            entries.append(
                (
                    fill_time,
                    {
                        "time": format_short_timestamp_ms(fill.get("time")),
                        "coin": fill_coin,
                        "dir": str(fill.get("dir", "n/a")) or "n/a",
                        "px": format_optional_decimal(fill.get("px")),
                        "value": decimal_to_display(value) if value is not None else "n/a",
                    },
                )
            )
            if len(entries) >= limit:
                break

        entries.sort(key=lambda item: item[0], reverse=True)

    return [row for _, row in entries[:limit]]


def print_recent_history(
    info: Info,
    account: str,
    coin: str | None = None,
    limit: int = 10,
    show_empty: bool = False,
) -> None:
    rows = collect_recent_history(info, account, coin=coin, limit=limit)
    if not rows and not show_empty:
        return
    print_table(
        "History",
        rows,
        [
            ("time", "time"),
            ("coin", "coin"),
            ("dir", "dir"),
            ("px", "px"),
            ("value", "value"),
        ],
        show_count=False,
    )


def grid_entry_sort_key(entry: dict[str, Any]) -> tuple[int, Decimal, int]:
    side_rank = 0 if str(entry.get("side")) == "buy" else 1
    price = decimal_or_none(entry.get("price", entry.get("limit_px"))) or Decimal("0")
    if side_rank == 0:
        price = -price
    try:
        oid = int(entry.get("oid") or 0)
    except (TypeError, ValueError):
        oid = 0
    return side_rank, price, oid


def format_grid_detail_rows(row: dict[str, Any], open_oids: set[int]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    entries = [item for item in row.get("levels") or [] if isinstance(item, dict) and item.get("side")]
    live_statuses = {
        "active",
        "pending",
        "paused_max",
        "paused_limit",
        "paused_margin",
        "paused_reduce_capacity",
        "paused_account_margin",
    }
    live_entries = [entry for entry in entries if str(entry.get("status", "active")) in live_statuses]
    history_entries = [entry for entry in entries if str(entry.get("status", "active")) not in live_statuses]
    history_entries = sorted(history_entries, key=lambda entry: int(entry.get("submitted_at") or entry.get("recovered_at") or entry.get("filled_at") or entry.get("cancelled_at") or entry.get("skipped_at") or entry.get("paused_at") or 0))[-120:]
    for entry in sorted(live_entries + history_entries, key=grid_entry_sort_key):
        try:
            oid = int(entry.get("oid") or 0)
        except (TypeError, ValueError):
            oid = 0
        status = str(entry.get("status", ""))
        live = "1" if status == "active" and oid in open_oids else "0"
        fill = entry.get("fill") if isinstance(entry.get("fill"), dict) else {}
        rows.append(
            {
                "side": str(entry.get("side", "")),
                "status": status,
                "live": live,
                "price": format_optional_decimal(entry.get("price", entry.get("limit_px"))),
                "size": format_optional_quantity(entry.get("size")),
                "value": format_open_order_value({"limitPx": entry.get("price", entry.get("limit_px")), "sz": entry.get("size")}),
                "oid": str(entry.get("oid", "")),
                "updated": format_timestamp_ms(int(entry.get("submitted_at") or entry.get("recovered_at") or entry.get("filled_at") or entry.get("cancelled_at") or 0) * 1000)
                if (entry.get("submitted_at") or entry.get("recovered_at") or entry.get("filled_at") or entry.get("cancelled_at"))
                else "-",
                "fillPx": format_optional_decimal(fill.get("px")) if fill else "-",
            }
        )
    return rows


def grid_query_avg_summary(
    row: dict[str, Any],
    asset: dict[str, Any],
    position_size: Decimal,
    position_value: Decimal,
) -> list[tuple[str, str]]:
    avg_value = decimal_or_none(row.get("avg"))
    base_gap = Decimal(str(row.get("gap_rate") or "0"))
    base_buy_size = Decimal(str(row.get("base_buy_size") or row.get("buy_size") or "0"))
    base_sell_size = Decimal(str(row.get("base_sell_size") or row.get("sell_size") or "0"))
    topup_buy_size = base_buy_size
    topup_sell_size = base_sell_size
    avg_label = "-"
    avg_position_label = "-"
    avg_multiplier_label = "-"
    avg_side_label = "-"
    if avg_value is not None:
        policy = grid_limit_policy_from_row(row)
        avg_multiplier, avg_side, avg_position = grid_avg_multiplier(
            policy,
            Decimal(str(row.get("min_position_value") or "0")),
            Decimal(str(row.get("max_position_value") or "0")),
            avg_value,
            position_size,
            position_value,
        )
        topup_buy_size, topup_sell_size, topup_buy_gap, topup_sell_gap = grid_avg_topup_params(
            base_gap,
            base_buy_size,
            base_sell_size,
            avg_multiplier,
            avg_side,
            int(row.get("sz_decimals") or asset["szDecimals"]),
        )
        avg_label = decimal_to_plain(avg_value)
        avg_position_label = decimal_to_plain(avg_position)
        avg_multiplier_label = decimal_to_plain(avg_multiplier)
        avg_side_label = avg_side or "balanced"
    else:
        topup_buy_gap = base_gap
        topup_sell_gap = base_gap
    return [
        ("avg", avg_label),
        ("avg_position", avg_position_label),
        ("avg_multiplier", avg_multiplier_label),
        ("avg_side", avg_side_label),
        ("base_gap", f"{row.get('gap', '')} ({row.get('gap_rate', '')})"),
        ("topup_gap", f"buy {decimal_to_plain(topup_buy_gap)} / sell {decimal_to_plain(topup_sell_gap)}"),
        ("base_size", f"buy {decimal_to_plain(base_buy_size)} / sell {decimal_to_plain(base_sell_size)}"),
        ("topup_size", f"buy {decimal_to_plain(topup_buy_size)} / sell {decimal_to_plain(topup_sell_size)}"),
    ]


def query_grid(args: argparse.Namespace) -> None:
    info, _exchange, account, signer, role = build_clients(args.network, args.timeout, args.coin, need_exchange=False)
    coin, asset = resolve_perp_asset(info, args.coin)
    dex = coin_dex(coin)
    if args.verbose:
        print("network:", args.network)
        print("account:", mask(account))
        print("signer:", mask(signer))
        print("account_role_source:", role)

    print_account_metrics(info, account)
    batch_rows = load_server_batch()
    rows = [
        row
        for row in batch_rows
        if row.get("type") == "grid" and batch_row_matches_context(row, args.network, account, coin)
    ]
    if not rows:
        print_box("Grid", [("coin", coin), ("status", "not found")])
        return

    open_oids: set[int] = set()
    for order in collect_frontend_open_orders(info, account, dex):
        if order.get("oid") is None or not position_matches_coin(str(order.get("coin", "")), coin):
            continue
        try:
            open_oids.add(int(order["oid"]))
        except (TypeError, ValueError):
            continue
    current_mid = None
    try:
        mids = info.all_mids(dex)
        if mids.get(coin) is not None:
            current_mid = Decimal(str(mids[coin]))
    except Exception as exc:
        log_event("grid_query_mids_error", {"type": type(exc).__name__, "message": str(exc)})
    position_size, position_value = (
        current_position_size_value(info, account, coin, dex, current_mid)
        if current_mid is not None
        else (Decimal("0"), Decimal("0"))
    )

    for row in rows:
        active_buy = len([entry for entry in active_grid_entries_for_row(row, "buy")])
        active_sell = len([entry for entry in active_grid_entries_for_row(row, "sell")])
        live_count = len(grid_batch_open_oids(row) & open_oids)
        summary = [
            ("coin", coin),
            ("status", str(row.get("status", ""))),
            ("limit", grid_limit_display(row)),
            ("min", str(row.get("min_order_value", MIN_NOTIONAL))),
            ("position", f"{decimal_to_plain(position_size)} / {decimal_to_display(position_value)}"),
            *grid_query_avg_summary(row, asset, position_size, position_value),
            ("trend", f"{row.get('trend', '0')} actual {row.get('actual_trend', '0%')}"),
            ("target_side", str(row.get("target_orders_per_side", GRID_TARGET_ORDERS_PER_SIDE))),
            ("active_buy", str(active_buy)),
            ("active_sell", str(active_sell)),
            ("live_oids", f"{live_count}/{len(grid_batch_open_oids(row))}"),
            ("updated", format_timestamp_ms(int(row.get("updated_at", 0)) * 1000) if row.get("updated_at") else "-"),
            ("note", str(row.get("note", ""))),
        ]
        print_box("Grid", summary)
        detail_rows = format_grid_detail_rows(row, open_oids)
        print_table(
            "Grid Orders",
            detail_rows,
            [
                ("side", "side"),
                ("status", "status"),
                ("live", "live"),
                ("price", "price"),
                ("size", "size"),
                ("value", "value"),
                ("oid", "oid"),
                ("updated", "updated"),
                ("fillPx", "fillPx"),
            ],
            show_count=False,
        )
    print_recent_history(info, account, coin=coin, limit=10)


def active_grid_entries_for_row(row: dict[str, Any], side: str | None = None) -> list[dict[str, Any]]:
    return [
        entry
        for entry in row.get("levels") or []
        if isinstance(entry, dict)
        and entry.get("side")
        and str(entry.get("status", "active")) == "active"
        and (side is None or str(entry.get("side")) == side)
    ]


def query_account(args: argparse.Namespace) -> None:
    info, _exchange, account, signer, role = build_clients(args.network, args.timeout, "", need_exchange=False)
    if args.verbose:
        print("network:", args.network)
        print("account:", mask(account))
        print("signer:", mask(signer))
        print("account_role_source:", role)

    print_account_metrics(info, account)
    batch_rows = load_server_batch()
    positions, orders = collect_account_positions_and_orders(info, account, args.network, batch_rows)
    pnl_window_hours = None
    if positions and positions[0].get("pnlWindowHours"):
        pnl_window_hours = int(positions[0]["pnlWindowHours"])
    print_table(
        "Positions",
        positions,
        [
            ("dex", "dex"),
            ("coin", "coin"),
            ("side", "side"),
            ("szi", "szi"),
            ("entryPx", "entryPx"),
            ("value", "value"),
            ("realPnl", real_pnl_label(pnl_window_hours)),
            ("result", "result"),
            ("roe", "ROE"),
            ("liqPx", "liqPx"),
            ("lev", "lev"),
        ],
    )
    print_table(
        "Open Orders",
        orders,
        [
            ("dex", "dex"),
            ("coin", "coin"),
            ("side", "side"),
            ("type", "type"),
            ("triggerPx", "triggerPx"),
            ("limitPx", "limitPx"),
            ("value", "value"),
            ("oid", "oid"),
            ("time", "time"),
        ],
        show_count=False,
    )
    print_server_batch(batch_rows, args.network, account)
    print_recent_history(info, account, show_empty=True)


def cancel_order(
    exchange: Exchange,
    info: Info,
    account: str,
    network: str,
    coin: str,
    dex: str,
    cancel_arg: str,
    dry_run: bool,
    cancel_price: Decimal | None = None,
    cancel_age_range: tuple[Decimal, Decimal | None] | None = None,
    price_rate: Decimal | None = None,
) -> None:
    filter_arg = cancel_arg.strip().lower()
    if filter_arg == "trail":
        cancel_trail_batch_orders(exchange, info, account, network, coin, dry_run, price_rate)
        return
    if filter_arg == "grid":
        cancel_grid_batch_orders(exchange, info, account, network, coin, dry_run, price_rate)
        return

    open_orders = collect_frontend_open_orders(info, account, dex)
    log_event("open_orders_before", open_orders)

    threshold_price: Decimal | None = cancel_price
    threshold_label = "price" if cancel_price is not None else "current_mid"
    age_range_ms: tuple[int, int | None] | None = None
    now_ms: int | None = None
    if filter_arg in {"up", "down"}:
        if threshold_price is None:
            mids = info.all_mids(dex)
            threshold_price = Decimal(str(mids[coin])) if mids.get(coin) is not None else None
            log_event("cancel_current_mid", {"dex": dex or "default", "coin": coin, "mid": mids.get(coin)})
        else:
            log_event("cancel_price", {"dex": dex or "default", "coin": coin, "price": decimal_to_plain(threshold_price)})
    if filter_arg in CANCEL_AGE_FILTERS:
        unit_ms = CANCEL_AGE_UNIT_MS[filter_arg]
        age_range = cancel_age_range or (Decimal(1), None)
        start, end = age_range
        age_range_ms = (int(start * unit_ms), None if end is None else int(end * unit_ms))
        now_ms = int(time.time() * 1000)
        log_event(
            "cancel_age_range",
            {
                "dex": dex or "default",
                "coin": coin,
                "unit": filter_arg,
                "range": [decimal_to_plain(start), None if end is None else decimal_to_plain(end)],
            },
        )
    matching_orders = select_cancel_orders(open_orders, coin, cancel_arg, threshold_price, age_range_ms, now_ms)

    if not matching_orders:
        print_account_metrics(info, account)
        rows = [("coin", coin), ("filter", cancel_filter_label(filter_arg)), ("cancelled", "0")]
        if threshold_price is not None:
            rows.append((threshold_label, format_price(threshold_price, price_rate)))
        if filter_arg in CANCEL_AGE_FILTERS:
            rows.append(("age", format_cancel_age_range(filter_arg, cancel_age_range or (Decimal(1), None))))
        print_box("Cancel", rows)
        return

    cancel_requests = [{"coin": str(order.get("coin", coin)), "oid": int(order["oid"])} for order in matching_orders]
    if dry_run:
        print_account_metrics(info, account)
        rows = [("dry_run", "1"), ("filter", cancel_filter_label(filter_arg))]
        if threshold_price is not None:
            rows.append((threshold_label, format_price(threshold_price, price_rate)))
        if filter_arg in CANCEL_AGE_FILTERS:
            rows.append(("age", format_cancel_age_range(filter_arg, cancel_age_range or (Decimal(1), None))))
        print_box("Run", rows)
        for order in matching_orders:
            display_px = open_order_cancel_price(order) or decimal_or_none(order.get("limitPx")) or Decimal("0")
            print_order_row(
                order["coin"],
                order["side"],
                None,
                display_px,
                order_amount(display_px, order.get("origSz", order.get("sz", "0"))),
                price_rate,
            )
        return

    result = exchange.bulk_cancel(cancel_requests)
    log_event("cancel_requests", cancel_requests)
    log_event("cancel_result", result)
    if result.get("status") != "ok":
        print_account_metrics(info, account)
        print("error:", result)
        return
    cancelled_oids = {int(request["oid"]) for request in cancel_requests}
    batch_cancelled = mark_cancelled_server_batch_oids(
        network,
        account,
        cancelled_oids,
        f"cancelled by --cancel {cancel_filter_label(filter_arg)}",
    )

    clear_info_cache(info)
    print_account_metrics(info, account)
    rows = [("coin", coin), ("filter", cancel_filter_label(filter_arg)), ("cancelled", str(len(matching_orders)))]
    if batch_cancelled:
        rows.append(("batch", str(batch_cancelled)))
    if threshold_price is not None:
        rows.append((threshold_label, format_price(threshold_price, price_rate)))
    if filter_arg in CANCEL_AGE_FILTERS:
        rows.append(("age", format_cancel_age_range(filter_arg, cancel_age_range or (Decimal(1), None))))
    print_box("Cancel", rows)
    for order in matching_orders:
        display_px = open_order_cancel_price(order) or decimal_or_none(order.get("limitPx")) or Decimal("0")
        print_order_row(
            order["coin"],
            order["side"],
            None,
            display_px,
            order_amount(display_px, order.get("origSz", order.get("sz", "0"))),
            price_rate,
        )


def update_order_leverage(exchange: Exchange, max_leverage: int, coin: str) -> tuple[str, dict[str, Any]]:
    result = exchange.update_leverage(max_leverage, coin, is_cross=True)
    log_event("update_leverage_result", {"mode": "cross", "leverage": max_leverage, "result": result})
    if result.get("status") == "ok":
        return "cross", result

    response = str(result.get("response", ""))
    if "Cross margin is not allowed" not in response:
        return "cross", result

    isolated_leverage = min(ISOLATED_FALLBACK_LEVERAGE, max_leverage)
    result = exchange.update_leverage(isolated_leverage, coin, is_cross=False)
    log_event("update_leverage_result", {"mode": "isolated", "leverage": isolated_leverage, "result": result})
    return "isolated", result


def asset_requires_isolated_margin(asset: dict[str, Any]) -> bool:
    margin_mode = str(asset.get("marginMode") or "").strip().lower()
    return bool(asset.get("onlyIsolated")) or margin_mode in {"nocross", "isolated"}


def update_isolated_opening_leverage(
    exchange: Exchange,
    max_leverage: int,
    coin: str,
) -> tuple[int, dict[str, Any]]:
    leverage = min(ISOLATED_FALLBACK_LEVERAGE, max_leverage)
    result = exchange.update_leverage(leverage, coin, is_cross=False)
    log_event(
        "update_leverage_result",
        {"mode": "isolated_opening", "leverage": leverage, "coin": coin, "result": result},
    )
    return leverage, result


def build_limit_order_plan(
    coin: str,
    is_buy: bool,
    amount: Decimal,
    asset: dict[str, Any],
    price: Decimal,
    reduce_only: bool,
    tif: str | None,
    current_mid: Decimal | None,
    label: str = "entry",
    price_source: str = "user",
) -> dict[str, Any]:
    sz_decimals = int(asset["szDecimals"])
    price = rounded_perp_price(price, sz_decimals)
    min_value_price = min(price, current_mid) if current_mid is not None else price
    size, notional, target_notional, minimum_value_notional = calc_size(
        amount,
        price,
        sz_decimals,
        min_value_price,
    )
    return {
        "label": label,
        "coin": coin,
        "is_buy": is_buy,
        "size": size,
        "limit_px": price,
        "order_type": {"limit": {"tif": tif or "Alo"}},
        "reduce_only": reduce_only,
        "mode": "limit",
        "notional": notional,
        "target_notional": target_notional,
        "worst_notional": notional,
        "reference_price": price,
        "price_source": price_source,
        "minimum_value_notional": minimum_value_notional,
        "min_value_price": min_value_price,
    }


def resolve_symmetric_offset(base_px: Decimal, offset_spec: str) -> Decimal:
    text = offset_spec.strip()
    if text.startswith("+"):
        text = text[1:]
    if not text or text.startswith("-"):
        raise ValueError("--offset must be positive, e.g. 2% or 1500")
    try:
        if text.endswith("%"):
            pct_text = text[:-1]
            if not pct_text:
                raise InvalidOperation
            offset = base_px * (Decimal(pct_text) / Decimal("100"))
        else:
            offset = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("--offset must be a positive number or percent, e.g. 2% or 1500") from exc
    if offset <= 0:
        raise ValueError("--offset must be positive")
    return offset


def build_entry_order_plan(
    args: argparse.Namespace,
    info: Info,
    exchange: Exchange,
    coin: str,
    asset: dict[str, Any],
    is_buy: bool,
    amount: Decimal,
    current_mid: Decimal | None,
    slippage: Decimal,
) -> dict[str, Any]:
    sz_decimals = int(asset["szDecimals"])
    if args.market:
        if current_mid is None:
            raise ValueError(f"No mid price found for {coin}, cannot place market order")
        reference_price = current_mid
        price = Decimal(str(exchange._slippage_price(coin, is_buy, float(slippage), float(reference_price))))
        price_source = f"mid with {format_percent(slippage)} slippage protection"
        size, notional, target_notional, worst_notional = calc_market_size(
            amount,
            reference_price,
            price,
            sz_decimals,
        )
        order_type = {"limit": {"tif": "Ioc"}}
        mode = "market"
        minimum_value_notional = None
        min_value_price = None
    else:
        if args.price:
            price = Decimal(args.price)
            price_source = "user"
        else:
            price = same_side_book_price(info, coin, is_buy, args.book_level)
            price_source = f"same-side book level {args.book_level}"
        return build_limit_order_plan(
            coin,
            is_buy,
            amount,
            asset,
            Decimal(price),
            args.reduce_only,
            args.tif,
            current_mid,
            label="entry",
            price_source=price_source,
        )

    return {
        "label": "entry",
        "coin": coin,
        "is_buy": is_buy,
        "size": size,
        "limit_px": price,
        "order_type": order_type,
        "reduce_only": args.reduce_only,
        "mode": mode,
        "notional": notional,
        "target_notional": target_notional,
        "worst_notional": worst_notional,
        "reference_price": reference_price,
        "price_source": price_source,
        "minimum_value_notional": minimum_value_notional,
        "min_value_price": min_value_price,
    }


def build_trigger_order_plan(
    coin: str,
    is_buy: bool,
    amount: Decimal,
    asset: dict[str, Any],
    exchange: Exchange,
    slippage: Decimal,
    label: str,
    trigger_px: Decimal,
    trigger_limit_px: Decimal | None,
    reduce_only: bool,
    tpsl: str | None = None,
    size: Decimal | None = None,
    size_ratio: Decimal = Decimal("1"),
) -> dict[str, Any]:
    if trigger_px <= 0:
        raise ValueError(f"{label} trigger price must be positive")
    sz_decimals = int(asset["szDecimals"])
    trigger_px = rounded_perp_price(trigger_px, sz_decimals)
    if trigger_limit_px is not None:
        trigger_limit_px = rounded_perp_price(trigger_limit_px, sz_decimals)
    is_market_trigger = trigger_limit_px is None
    if is_market_trigger:
        limit_px = Decimal(str(exchange._slippage_price(coin, is_buy, float(slippage), float(trigger_px))))
        limit_px = rounded_perp_price(limit_px, sz_decimals)
        min_value_price = limit_px
        mode = f"{label}-market"
        if size is None:
            size, notional, target_notional, worst_notional = calc_market_size(
                amount,
                trigger_px,
                limit_px,
                sz_decimals,
            )
        else:
            notional = size * trigger_px
            target_notional = notional
            worst_notional = size * limit_px
    else:
        limit_px = trigger_limit_px
        mode = f"{label}-limit"
        if limit_px <= 0:
            raise ValueError(f"{label} limit price must be positive")
        min_value_price = min(trigger_px, limit_px)
        if size is None:
            size, notional, target_notional, _minimum_value_notional = calc_size(
                amount,
                limit_px,
                sz_decimals,
                min_value_price,
            )
        else:
            notional = size * limit_px
            target_notional = notional
        worst_notional = notional

    size = scale_order_size(size, size_ratio, sz_decimals, min_value_price, label)
    if is_market_trigger:
        notional = size * trigger_px
        target_notional = notional
        worst_notional = size * limit_px
    else:
        notional = size * limit_px
        target_notional = notional
        worst_notional = notional

    return {
        "label": label,
        "coin": coin,
        "is_buy": is_buy,
        "size": size,
        "limit_px": limit_px,
        "trigger_px": trigger_px,
        "reference_price": trigger_px,
        "order_type": {
            "trigger": {
                "triggerPx": float(trigger_px),
                "isMarket": is_market_trigger,
                "tpsl": tpsl or ("tp" if label == "tp" else "sl"),
            }
        },
        "reduce_only": reduce_only,
        "mode": mode,
        "notional": notional,
        "target_notional": target_notional,
        "worst_notional": worst_notional,
    }


def build_stop_entry_order_plan(
    coin: str,
    is_buy: bool,
    amount: Decimal,
    asset: dict[str, Any],
    exchange: Exchange,
    slippage: Decimal,
    trigger_px: Decimal,
    trigger_limit_px: Decimal | None,
    current_mid: Decimal | None,
) -> dict[str, Any]:
    sz_decimals = int(asset["szDecimals"])
    trigger_px = rounded_perp_price(trigger_px, sz_decimals)
    if trigger_limit_px is not None:
        trigger_limit_px = rounded_perp_price(trigger_limit_px, sz_decimals)
    if current_mid is not None:
        if is_buy and trigger_px <= current_mid:
            raise ValueError(
                f"Stop-entry buy orders must trigger above the current mid ({decimal_to_display(current_mid)}); "
                "use --take-entry for if-touched entries below the market."
            )
        if not is_buy and trigger_px >= current_mid:
            raise ValueError(
                f"Stop-entry sell orders must trigger below the current mid ({decimal_to_display(current_mid)}); "
                "use --take-entry for if-touched entries above the market."
            )

    return build_trigger_order_plan(
        coin,
        is_buy,
        amount,
        asset,
        exchange,
        slippage,
        "stop-entry",
        trigger_px,
        trigger_limit_px,
        False,
        tpsl="sl",
    )


def build_take_entry_order_plan(
    coin: str,
    is_buy: bool,
    amount: Decimal,
    asset: dict[str, Any],
    exchange: Exchange,
    slippage: Decimal,
    trigger_px: Decimal,
    trigger_limit_px: Decimal | None,
    current_mid: Decimal | None,
) -> dict[str, Any]:
    sz_decimals = int(asset["szDecimals"])
    trigger_px = rounded_perp_price(trigger_px, sz_decimals)
    if trigger_limit_px is not None:
        trigger_limit_px = rounded_perp_price(trigger_limit_px, sz_decimals)
    if current_mid is not None:
        if is_buy and trigger_px >= current_mid:
            raise ValueError(
                f"Take-entry buy orders must trigger below the current mid ({decimal_to_display(current_mid)}); "
                "use --stop-entry for breakouts above the market."
            )
        if not is_buy and trigger_px <= current_mid:
            raise ValueError(
                f"Take-entry sell orders must trigger above the current mid ({decimal_to_display(current_mid)}); "
                "use --stop-entry for breakouts below the market."
            )

    return build_trigger_order_plan(
        coin,
        is_buy,
        amount,
        asset,
        exchange,
        slippage,
        "take-entry",
        trigger_px,
        trigger_limit_px,
        False,
        tpsl="tp",
    )


def parse_percent_decimal(value: str, label: str, allow_signed: bool = True) -> Decimal:
    text = value.strip()
    if not text:
        raise ValueError(f"{label} is required")
    if text.endswith("%"):
        number_text = text[:-1]
        scale = Decimal("100")
    else:
        number_text = text
        scale = Decimal("1")
    if not allow_signed and number_text.startswith(("+", "-")):
        raise ValueError(f"{label} must be positive")
    try:
        value_decimal = Decimal(number_text) / scale
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a number or percent") from exc
    return value_decimal


def parse_grid_gap(value: str | list[str]) -> tuple[Decimal, Decimal | None]:
    if isinstance(value, list):
        if len(value) not in {1, 2}:
            raise ValueError("--gap accepts BASE or BASE OFFSET, e.g. --gap 0.1% 0.03%")
        if len(value) == 2:
            gap = parse_percent_decimal(value[0], "--gap", allow_signed=False)
            if gap <= 0:
                raise ValueError("--gap base spacing must be positive")
            limit_offset = abs(parse_percent_decimal(value[1], "--gap limit offset", allow_signed=True))
            if limit_offset <= 0:
                raise ValueError("--gap limit offset must be positive")
            return gap, limit_offset
        value = value[0]

    text = value.strip().replace(" ", "")
    if not text:
        raise ValueError("--gap is required for grid orders")

    base_end = text.find("%")
    if base_end >= 0:
        base_text = text[: base_end + 1]
        suffix = text[base_end + 1 :]
    else:
        split_at = None
        for index, char in enumerate(text[1:], start=1):
            if char in "+-":
                split_at = index
                break
        if split_at is None:
            base_text = text
            suffix = ""
        else:
            base_text = text[:split_at]
            suffix = text[split_at:]

    gap = parse_percent_decimal(base_text, "--gap", allow_signed=False)
    if gap <= 0:
        raise ValueError("--gap base spacing must be positive")
    if suffix == "":
        return gap, None
    if suffix.startswith("+-") or suffix.startswith("-+"):
        suffix = suffix[1:]
    limit_offset = abs(parse_percent_decimal(suffix, "--gap limit offset", allow_signed=True))
    if limit_offset <= 0:
        raise ValueError("--gap limit offset must be positive")
    return gap, limit_offset


def grid_gap_spec(args: argparse.Namespace) -> list[str]:
    resolved = getattr(args, "resolved_grid_gap_spec", None)
    if resolved:
        return list(resolved)
    gap = getattr(args, "gap", None)
    if gap is None:
        return list(DEFAULT_GRID_GAP_LABEL)
    if isinstance(gap, list):
        return gap
    return [gap]


def grid_limit_policy_from_row(row: dict[str, Any]) -> str:
    policy = str(row.get("position_limit_mode") or row.get("max_position_mode") or "abs").strip().lower()
    if policy in {"long", "short", "abs"}:
        return policy
    return "abs"


def grid_limit_display(row: dict[str, Any]) -> str:
    policy = grid_limit_policy_from_row(row)
    minimum = Decimal(str(row.get("min_position_value") or "0"))
    maximum = str(row.get("max_position_value", ""))
    if policy in {"long", "short"} and minimum > 0:
        return f"{policy} {decimal_to_plain(minimum)}-{maximum}"
    return f"{policy} {maximum}"


def grid_limit_arg(args: argparse.Namespace) -> tuple[str | None, str | None, str | None]:
    values = [
        ("long", getattr(args, "grid_long", None)),
        ("short", getattr(args, "grid_short", None)),
        ("abs", getattr(args, "grid_abs", None)),
    ]
    selected = [(policy, value) for policy, value in values if value is not None]
    if not selected:
        return None, None, None
    policy, raw_value = selected[0]
    values_list = list(raw_value) if isinstance(raw_value, list) else [raw_value]
    if policy == "abs":
        if len(values_list) != 1:
            raise ValueError("--abs accepts exactly one value")
        return policy, "0", str(values_list[0])
    if len(values_list) == 1:
        return policy, "0", str(values_list[0])
    if len(values_list) == 2:
        return policy, str(values_list[0]), str(values_list[1])
    raise ValueError(f"--{policy} accepts one or two values")


def grid_avg_bounds(policy: str, min_position_value: Decimal, max_position_value: Decimal) -> tuple[Decimal, Decimal]:
    if policy == "abs":
        return -max_position_value, max_position_value
    return min_position_value, max_position_value


def grid_avg_position_value(policy: str, position_size: Decimal, position_value: Decimal) -> Decimal:
    signed_value = position_value.copy_sign(position_size) if position_size != 0 else Decimal("0")
    return -signed_value if policy == "short" else signed_value


def grid_avg_multiplier(
    policy: str,
    min_position_value: Decimal,
    max_position_value: Decimal,
    avg_position_value: Decimal,
    position_size: Decimal,
    position_value: Decimal,
) -> tuple[Decimal, str | None, Decimal]:
    lower, upper = grid_avg_bounds(policy, min_position_value, max_position_value)
    current = grid_avg_position_value(policy, position_size, position_value)
    if current == avg_position_value:
        return Decimal("1"), None, current
    if current < avg_position_value:
        span = avg_position_value - lower
        deviation = Decimal("1") if span <= 0 else (avg_position_value - current) / span
        favored_side = "sell" if policy == "short" else "buy"
    else:
        span = upper - avg_position_value
        deviation = Decimal("1") if span <= 0 else (current - avg_position_value) / span
        favored_side = "buy" if policy == "short" else "sell"
    deviation = max(Decimal("0"), min(Decimal("1"), deviation))
    multiplier = Decimal("1") + (GRID_AVG_MAX_MULTIPLIER - Decimal("1")) * deviation
    return multiplier, favored_side, current


def round_grid_size_nearest(value: Decimal, sz_decimals: int) -> Decimal:
    step = Decimal(1).scaleb(-sz_decimals)
    return (value / step).to_integral_value(rounding=ROUND_HALF_UP) * step


def grid_avg_size_pair(
    base_buy_size: Decimal,
    base_sell_size: Decimal,
    multiplier: Decimal,
    favored_side: str | None,
    sz_decimals: int,
) -> tuple[Decimal, Decimal]:
    buy_size = base_buy_size
    sell_size = base_sell_size
    if favored_side == "buy":
        buy_size = round_grid_size_nearest(base_buy_size * multiplier, sz_decimals)
    elif favored_side == "sell":
        sell_size = round_grid_size_nearest(base_sell_size * multiplier, sz_decimals)
    return buy_size, sell_size


def grid_avg_topup_params(
    base_gap: Decimal,
    base_buy_size: Decimal,
    base_sell_size: Decimal,
    multiplier: Decimal,
    favored_side: str | None,
    sz_decimals: int,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    buy_size, sell_size = grid_avg_size_pair(
        base_buy_size,
        base_sell_size,
        multiplier,
        favored_side,
        sz_decimals,
    )
    buy_gap = base_gap
    sell_gap = base_gap
    if favored_side == "buy":
        sell_gap *= multiplier
    elif favored_side == "sell":
        buy_gap *= multiplier
    return buy_size, sell_size, buy_gap, sell_gap


def refresh_grid_row_strategy_params(
    row: dict[str, Any],
    asset: dict[str, Any],
    current_mid: Decimal,
    position_size: Decimal,
    position_value: Decimal,
) -> None:
    sz_decimals = int(row.get("sz_decimals") or asset["szDecimals"])
    base_gap = Decimal(str(row["gap_rate"]))
    first_buy_px = rounded_perp_price(current_mid * (Decimal("1") - base_gap), sz_decimals)
    first_sell_px = rounded_perp_price(current_mid * (Decimal("1") + base_gap), sz_decimals)
    if first_buy_px <= 0 or first_buy_px >= first_sell_px:
        raise ValueError("grid --gap is too small for this price precision")

    trend = parse_percent_decimal(str(row.get("trend") or "0"), "--trend", allow_signed=True)
    if trend <= Decimal("-1"):
        raise ValueError("--trend must be greater than -100%")
    min_notional = Decimal(str(row.get("min_order_value") or MIN_NOTIONAL))
    base_buy_size, base_sell_size = grid_limit_size_pair_for_trend(
        trend,
        sz_decimals,
        first_buy_px,
        first_sell_px,
        min_notional,
    )

    avg_value = decimal_or_none(row.get("avg"))
    multiplier = Decimal("1")
    favored_side: str | None = None
    avg_current_value = grid_avg_position_value(
        grid_limit_policy_from_row(row),
        position_size,
        position_value,
    )
    if avg_value is not None:
        balanced_size = max(base_buy_size, base_sell_size)
        base_buy_size = balanced_size
        base_sell_size = balanced_size
        multiplier, favored_side, avg_current_value = grid_avg_multiplier(
            grid_limit_policy_from_row(row),
            Decimal(str(row.get("min_position_value") or "0")),
            Decimal(str(row.get("max_position_value") or "0")),
            avg_value,
            position_size,
            position_value,
        )

    topup_buy_size, topup_sell_size, topup_buy_gap, topup_sell_gap = grid_avg_topup_params(
        base_gap,
        base_buy_size,
        base_sell_size,
        multiplier,
        favored_side,
        sz_decimals,
    )
    row["buy_size"] = decimal_to_plain(base_buy_size)
    row["sell_size"] = decimal_to_plain(base_sell_size)
    row["base_buy_size"] = decimal_to_plain(base_buy_size)
    row["base_sell_size"] = decimal_to_plain(base_sell_size)
    row["topup_buy_size"] = decimal_to_plain(topup_buy_size)
    row["topup_sell_size"] = decimal_to_plain(topup_sell_size)
    row["topup_buy_gap"] = decimal_to_plain(topup_buy_gap)
    row["topup_sell_gap"] = decimal_to_plain(topup_sell_gap)
    row["effective_gap_rate"] = decimal_to_plain(base_gap * multiplier)
    row["avg_multiplier"] = decimal_to_plain(multiplier)
    row["avg_favored_side"] = favored_side
    row["avg_current_value"] = decimal_to_plain(avg_current_value)
    if topup_buy_size > topup_sell_size:
        row["actual_trend"] = format_signed_percent(topup_buy_size / topup_sell_size - Decimal("1"))
    elif topup_sell_size > topup_buy_size:
        row["actual_trend"] = format_signed_percent(-(topup_sell_size / topup_buy_size - Decimal("1")))
    else:
        row["actual_trend"] = "0%"


def grid_min_notional(args: argparse.Namespace) -> Decimal:
    value = getattr(args, "grid_min", None)
    if value is None:
        return MIN_NOTIONAL
    amount = Decimal(str(value))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("--min must be positive")
    return amount


def grid_range_spec(args: argparse.Namespace) -> list[str]:
    range_spec = getattr(args, "range_spec", None)
    if range_spec:
        return list(range_spec)
    return list(DEFAULT_GRID_RANGE)


def minimum_grid_gap(anchor: Decimal, sz_decimals: int) -> Decimal:
    if anchor <= 0:
        raise ValueError("grid anchor price must be positive")

    price_step = Decimal(1).scaleb(-max(0, 6 - sz_decimals))
    base_px = rounded_perp_price(anchor, sz_decimals)
    while rounded_perp_price(anchor + price_step, sz_decimals) <= base_px:
        price_step *= Decimal("10")

    gap = price_step / anchor
    for _ in range(12):
        buy_trigger = rounded_perp_price(anchor * (Decimal("1") - gap / Decimal("2")), sz_decimals)
        sell_trigger = rounded_perp_price(anchor * (Decimal("1") + gap / Decimal("2")), sz_decimals)
        if buy_trigger < sell_trigger:
            return gap
        gap += price_step / anchor

    raise ValueError("could not resolve a minimum grid gap for this price precision")


def resolve_grid_gap(
    args: argparse.Namespace,
    info: Info,
    account: str,
    asset: dict[str, Any],
    dex: str,
    start_px: Decimal,
    end_px: Decimal,
) -> tuple[Decimal, Decimal | None]:
    if getattr(args, "gap", None):
        return parse_grid_gap(args.gap)

    sz_decimals = int(asset["szDecimals"])
    gap = max(minimum_grid_gap(start_px, sz_decimals), minimum_grid_gap(end_px, sz_decimals))
    fee_rates = effective_perp_fee_rates(info, account, asset, dex)
    taker_fee = fee_rates.get("taker_effective")
    maker_fee = fee_rates.get("maker_effective")
    if taker_fee is None or maker_fee is None:
        raise ValueError("grid default --gap requires effective taker and maker fees; specify --gap explicitly")
    fee_gap = taker_fee + maker_fee
    if fee_gap <= 0:
        raise ValueError("grid default --gap resolved to a non-positive fee gap; specify --gap explicitly")

    total_gap = gap + fee_gap
    args.resolved_grid_gap_spec = [
        f"{format_rate_percent(total_gap)} (minTick {format_rate_percent(gap)} + taker {format_rate_percent(taker_fee)} + maker {format_rate_percent(maker_fee)})"
    ]
    return gap, fee_gap


def resolve_grid_spacing(
    args: argparse.Namespace,
    info: Info,
    account: str,
    asset: dict[str, Any],
    dex: str,
    anchor_px: Decimal,
) -> Decimal:
    if getattr(args, "gap", None):
        gap, _offset = parse_grid_gap(args.gap)
        return gap

    sz_decimals = int(asset["szDecimals"])
    min_gap = minimum_grid_gap(anchor_px, sz_decimals)
    fee_rates = effective_perp_fee_rates(info, account, asset, dex)
    taker_fee = fee_rates.get("taker_effective")
    maker_fee = fee_rates.get("maker_effective")
    if taker_fee is None or maker_fee is None:
        raise ValueError("grid default --gap requires effective taker and maker fees; specify --gap explicitly")
    spacing = min_gap + taker_fee + maker_fee
    if spacing <= 0:
        raise ValueError("grid default --gap resolved to a non-positive spacing; specify --gap explicitly")
    args.resolved_grid_gap_spec = [
        f"{format_rate_percent(spacing)} (minTick {format_rate_percent(min_gap)} + taker {format_rate_percent(taker_fee)} + maker {format_rate_percent(maker_fee)})"
    ]
    return spacing


def round_size_up(value: Decimal, sz_decimals: int) -> Decimal:
    step = Decimal(1).scaleb(-sz_decimals)
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def decimal_ratio_period(value: Decimal) -> int:
    normalized = value.normalize()
    if normalized.as_tuple().exponent >= 0:
        return 1
    denominator = 10 ** abs(normalized.as_tuple().exponent)
    numerator = int(normalized * denominator)
    return denominator // denominator_gcd(abs(numerator), denominator)


def denominator_gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a or 1


def ceil_decimal_to_int(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_UP))


def choose_grid_size_units(
    min_base_units: int,
    target_ratio: Decimal,
    total_amount: Decimal,
    step: Decimal,
    base_max_limit: Decimal,
    tilted_max_limit: Decimal,
    trend_label: str,
) -> tuple[int, int, Decimal]:
    def tilted_units_for(base_units: int) -> int:
        return ceil_decimal_to_int(Decimal(base_units) * target_ratio)

    def one_way_notional(base_units: int) -> Decimal:
        tilted_units = tilted_units_for(base_units)
        return max(Decimal(base_units) * step * base_max_limit, Decimal(tilted_units) * step * tilted_max_limit)

    low = min_base_units
    if one_way_notional(low) > total_amount:
        return low, tilted_units_for(low), one_way_notional(low)

    high = low
    while one_way_notional(high) <= total_amount:
        high *= 2

    left, right = low, high - 1
    while left <= right:
        mid = (left + right) // 2
        if one_way_notional(mid) <= total_amount:
            left = mid + 1
        else:
            right = mid - 1
    max_base_units = right

    period = decimal_ratio_period(target_ratio)
    exact_units = ((min_base_units + period - 1) // period) * period
    if exact_units > max_base_units:
        exact_notional = one_way_notional(exact_units)
        raise ValueError(
            f"grid max is too small to fit --trend {trend_label} at this size precision; "
            f"needs at least {decimal_to_display(exact_notional)} USD one-way total"
        )

    chosen_base_units = exact_units
    chosen_tilted_units = tilted_units_for(chosen_base_units)
    return chosen_base_units, chosen_tilted_units, one_way_notional(chosen_base_units)


def grid_size_pair_for_trend(
    trend: Decimal,
    sz_decimals: int,
    total_amount: Decimal,
    min_buy_limit: Decimal,
    min_sell_limit: Decimal,
    max_buy_limit: Decimal,
    max_sell_limit: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    step = Decimal(1).scaleb(-sz_decimals)
    target_ratio = Decimal("1") + abs(trend)
    if trend >= 0:
        min_sell_units = ceil_decimal_to_int(MIN_NOTIONAL / min_sell_limit / step)
        sell_units, buy_units, max_one_way_notional = choose_grid_size_units(
            min_sell_units,
            target_ratio,
            total_amount,
            step,
            max_sell_limit,
            max_buy_limit,
            format_signed_percent(trend),
        )
    else:
        min_buy_units = ceil_decimal_to_int(MIN_NOTIONAL / min_buy_limit / step)
        buy_units, sell_units, max_one_way_notional = choose_grid_size_units(
            min_buy_units,
            target_ratio,
            total_amount,
            step,
            max_buy_limit,
            max_sell_limit,
            format_signed_percent(trend),
        )
    return Decimal(buy_units) * step, Decimal(sell_units) * step, max_one_way_notional


def grid_limit_size_pair_for_trend(
    trend: Decimal,
    sz_decimals: int,
    buy_px: Decimal,
    sell_px: Decimal,
    min_notional: Decimal = MIN_NOTIONAL,
) -> tuple[Decimal, Decimal]:
    step = Decimal(1).scaleb(-sz_decimals)

    def min_units(price: Decimal) -> int:
        return max(1, ceil_decimal_to_int(min_notional / price / step))

    target_ratio = Decimal("1") + abs(trend)
    if trend >= 0:
        sell_units = min_units(sell_px)
        buy_units = max(min_units(buy_px), ceil_decimal_to_int(Decimal(sell_units) * target_ratio))
    else:
        buy_units = min_units(buy_px)
        sell_units = max(min_units(sell_px), ceil_decimal_to_int(Decimal(buy_units) * target_ratio))
    return Decimal(buy_units) * step, Decimal(sell_units) * step


def grid_size_for_min_notional(size: Decimal, price: Decimal, sz_decimals: int, min_notional: Decimal) -> Decimal:
    if price <= 0:
        return size
    step = Decimal(1).scaleb(-sz_decimals)
    min_units = ceil_decimal_to_int(min_notional / price / step)
    return max(size, Decimal(min_units) * step)


def current_position_size_value(info: Info, account: str, coin: str, dex: str, current_mid: Decimal) -> tuple[Decimal, Decimal]:
    position = find_current_position(info, account, coin, dex)
    if position is None:
        return Decimal("0"), Decimal("0")
    size = Decimal(str(position.get("szi", "0")))
    value = decimal_or_none(position.get("positionValue"))
    if value is None:
        value = abs(size * current_mid)
    return size, abs(value)


def grid_order_would_add_risk(position_size: Decimal, is_buy: bool) -> bool:
    if position_size == 0:
        return True
    return (position_size > 0 and is_buy) or (position_size < 0 and not is_buy)


def grid_order_allowed_by_policy(position_size: Decimal, is_buy: bool, policy: str) -> bool:
    if policy == "abs":
        return True
    if policy == "long":
        return is_buy or position_size > 0
    if policy == "short":
        return (not is_buy) or position_size < 0
    return True


def grid_order_should_reduce_only(position_size: Decimal, is_buy: bool, policy: str) -> bool:
    if position_size == 0:
        return False
    reduces_position = (position_size > 0 and not is_buy) or (position_size < 0 and is_buy)
    if policy == "long":
        return reduces_position
    if policy == "short":
        return reduces_position
    return False


def grid_order_allowed_by_max(
    position_size: Decimal,
    position_value: Decimal,
    is_buy: bool,
    order_notional: Decimal,
    max_position_value: Decimal,
    policy: str = "abs",
    min_position_value: Decimal = Decimal("0"),
) -> bool:
    if not grid_order_allowed_by_policy(position_size, is_buy, policy):
        return False
    if grid_order_would_add_risk(position_size, is_buy):
        return position_value + order_notional <= max_position_value
    position_matches_target = (policy == "long" and position_size > 0) or (policy == "short" and position_size < 0)
    if position_matches_target:
        return max(Decimal("0"), position_value - order_notional) >= min_position_value
    return True


def build_grid_limit_order_plan(
    coin: str,
    is_buy: bool,
    size: Decimal,
    price: Decimal,
    asset: dict[str, Any],
    reduce_only: bool,
    label: str,
) -> dict[str, Any]:
    sz_decimals = int(asset["szDecimals"])
    price = rounded_perp_price(price, sz_decimals)
    notional = size * price
    return {
        "label": label,
        "coin": coin,
        "is_buy": is_buy,
        "size": size,
        "limit_px": price,
        "order_type": {"limit": {"tif": "Alo"}},
        "reduce_only": reduce_only,
        "mode": "grid-limit",
        "notional": notional,
        "target_notional": notional,
        "worst_notional": notional,
        "reference_price": price,
        "price_source": "grid",
        "minimum_value_notional": None,
        "min_value_price": price,
        "grid_side": "buy" if is_buy else "sell",
    }


def grid_anchor_prices(start: Decimal, end: Decimal, count: int, sz_decimals: int) -> list[Decimal]:
    if count < 1:
        raise ValueError("grid count must be positive")
    if count == 1:
        return [rounded_perp_price((start + end) / Decimal("2"), sz_decimals)]
    return [
        rounded_perp_price(start + (end - start) * Decimal(index) / Decimal(count - 1), sz_decimals)
        for index in range(count)
    ]


def is_auto_range_value(value: str) -> bool:
    return value.strip().lower() in {"auto", "自动"}


def open_order_trigger_decimal(order: dict[str, Any]) -> Decimal | None:
    trigger_px = decimal_or_none(order.get("triggerPx"))
    if trigger_px is not None and trigger_px > 0:
        return trigger_px
    return None


def resolve_grid_auto_range_value(
    value: str,
    side: str,
    info: Info,
    account: str,
    coin: str,
    dex: str,
    current_mid: Decimal,
) -> Decimal:
    if not is_auto_range_value(value):
        return Decimal(value)

    open_orders = collect_frontend_open_orders(info, account, dex)
    trigger_prices = [
        trigger_px
        for order in open_orders
        if position_matches_coin(str(order.get("coin", "")), coin)
        and (order.get("isTrigger") or open_order_trigger_decimal(order) is not None)
        and (trigger_px := open_order_trigger_decimal(order)) is not None
    ]
    if side == "lower":
        candidates = [price for price in trigger_prices if price < current_mid]
        if not candidates:
            raise ValueError(f"grid --range auto could not find an open trigger order below current mid ({decimal_to_display(current_mid)})")
        return max(candidates)

    candidates = [price for price in trigger_prices if price > current_mid]
    if not candidates:
        raise ValueError(f"grid --range auto could not find an open trigger order above current mid ({decimal_to_display(current_mid)})")
    return min(candidates)


def build_grid_orders(
    args: argparse.Namespace,
    info: Info,
    account: str,
    dex: str,
    exchange: Exchange,
    coin: str,
    asset: dict[str, Any],
    max_position_value: Decimal,
    current_mid: Decimal | None,
    slippage: Decimal,
) -> list[dict[str, Any]]:
    if current_mid is None:
        raise ValueError(f"No mid price found for {coin}, cannot place grid orders")
    if max_position_value < MIN_NOTIONAL:
        raise ValueError(f"grid max must be at least {MIN_NOTIONAL} USD")

    trend = parse_percent_decimal(args.trend or "0", "--trend", allow_signed=True)
    if trend <= Decimal("-1"):
        raise ValueError("--trend must be greater than -100%")

    sz_decimals = int(asset["szDecimals"])
    anchor = rounded_perp_price(current_mid, sz_decimals)
    base_gap = resolve_grid_spacing(args, info, account, asset, dex, anchor)
    position_size, position_value = current_position_size_value(info, account, coin, dex, current_mid)
    policy = getattr(args, "grid_position_limit_mode", None) or "abs"
    min_position_value = Decimal(str(getattr(args, "grid_position_min_value", None) or "0"))
    avg_value = decimal_or_none(getattr(args, "grid_avg", None))
    avg_multiplier = Decimal("1")
    avg_favored_side: str | None = None
    avg_current_value = grid_avg_position_value(policy, position_size, position_value)
    if avg_value is not None:
        avg_multiplier, avg_favored_side, avg_current_value = grid_avg_multiplier(
            policy,
            min_position_value,
            max_position_value,
            avg_value,
            position_size,
            position_value,
        )
    effective_gap = base_gap * avg_multiplier
    first_buy_px = rounded_perp_price(anchor * (Decimal("1") - base_gap), sz_decimals)
    first_sell_px = rounded_perp_price(anchor * (Decimal("1") + base_gap), sz_decimals)
    if first_buy_px <= 0 or first_buy_px >= first_sell_px:
        raise ValueError("grid --gap is too small for this price precision")

    min_notional = grid_min_notional(args)
    base_buy_size, base_sell_size = grid_limit_size_pair_for_trend(trend, sz_decimals, first_buy_px, first_sell_px, min_notional)
    if avg_value is not None:
        balanced_size = max(base_buy_size, base_sell_size)
        base_buy_size = balanced_size
        base_sell_size = balanced_size
    topup_buy_size, topup_sell_size, topup_buy_gap, topup_sell_gap = grid_avg_topup_params(
        base_gap,
        base_buy_size,
        base_sell_size,
        avg_multiplier,
        avg_favored_side,
        sz_decimals,
    )
    if topup_buy_size > topup_sell_size:
        args.resolved_grid_trend = format_signed_percent(topup_buy_size / topup_sell_size - Decimal("1"))
    elif topup_sell_size > topup_buy_size:
        args.resolved_grid_trend = format_signed_percent(-(topup_sell_size / topup_buy_size - Decimal("1")))
    else:
        args.resolved_grid_trend = "0%"

    projected_position_values = {"buy": position_value, "sell": position_value}
    plans: list[dict[str, Any]] = []
    for depth in range(1, GRID_TARGET_ORDERS_PER_SIDE + 1):
        for is_buy, base_size, label, multiplier in (
            (True, base_buy_size, f"buy {depth}", Decimal("1") - base_gap * Decimal(depth)),
            (False, base_sell_size, f"sell {depth}", Decimal("1") + base_gap * Decimal(depth)),
        ):
            price = rounded_perp_price(anchor * multiplier, sz_decimals)
            if price <= 0:
                continue
            size = grid_size_for_min_notional(base_size, price, sz_decimals, min_notional)
            notional = size * price
            side = "buy" if is_buy else "sell"
            projected_position_value = projected_position_values[side]
            if not grid_order_allowed_by_max(
                position_size,
                projected_position_value,
                is_buy,
                notional,
                max_position_value,
                policy,
                min_position_value,
            ):
                continue
            reduce_only = grid_order_should_reduce_only(position_size, is_buy, policy)
            plan = build_grid_limit_order_plan(coin, is_buy, size, price, asset, reduce_only, label)
            plan["grid_anchor"] = anchor
            plan["grid_gap"] = base_gap
            plan["grid_base_gap"] = base_gap
            plan["grid_depth"] = depth
            plan["grid_buy_size"] = base_buy_size
            plan["grid_sell_size"] = base_sell_size
            plan["grid_base_buy_size"] = base_buy_size
            plan["grid_base_sell_size"] = base_sell_size
            plan["grid_topup_buy_size"] = topup_buy_size
            plan["grid_topup_sell_size"] = topup_sell_size
            plan["grid_topup_buy_gap"] = topup_buy_gap
            plan["grid_topup_sell_gap"] = topup_sell_gap
            plan["grid_effective_gap"] = effective_gap
            plan["grid_avg_multiplier"] = avg_multiplier
            plan["grid_avg_favored_side"] = avg_favored_side
            plan["grid_avg_current_value"] = avg_current_value
            plans.append(plan)
            if grid_order_would_add_risk(position_size, is_buy):
                projected_position_values[side] += notional
            else:
                projected_position_values[side] = max(Decimal("0"), projected_position_value - notional)
    if not plans:
        raise ValueError("grid position limit is already reached and no reducing grid order can be placed")
    return plans


def status_order_oid(status: dict[str, Any] | None) -> int | None:
    if not isinstance(status, dict):
        return None
    for key in ("resting", "filled"):
        payload = status.get(key)
        if isinstance(payload, dict) and payload.get("oid") is not None:
            return int(payload["oid"])
    return None


def is_post_only_reject_text(text: str) -> bool:
    lowered = text.lower()
    return "post only" in lowered or "would immediately match" in lowered or "only limit" in lowered or "只限挂单" in text


def status_is_post_only_reject(status: dict[str, Any] | None) -> bool:
    if not isinstance(status, dict) or not status.get("error"):
        return False
    return is_post_only_reject_text(str(status.get("error")))


def status_order_state(status: dict[str, Any] | None) -> str:
    if not isinstance(status, dict):
        return "unknown"
    if status.get("error"):
        if status_is_post_only_reject(status):
            return "skipped_post_only"
        return "error"
    if "filled" in status:
        return "filled"
    if "resting" in status:
        return "active"
    return "unknown"


def build_grid_batch_row(
    args: argparse.Namespace,
    account: str,
    coin: str,
    dex: str,
    asset: dict[str, Any],
    plans: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
    max_position_value: Decimal,
    slippage: Decimal,
) -> dict[str, Any]:
    now = int(time.time())
    levels: list[dict[str, Any]] = []
    for index, plan in enumerate(plans):
        status = statuses[index] if index < len(statuses) else None
        state = status_order_state(status)
        oid = status_order_oid(status)
        side = str(plan.get("grid_side") or ("buy" if plan["is_buy"] else "sell"))
        order_state: dict[str, Any] = {
            "index": index + 1,
            "side": side,
            "status": state,
            "oid": oid,
            "is_buy": bool(plan["is_buy"]),
            "limit_px": decimal_to_plain(Decimal(str(plan["limit_px"]))),
            "price": decimal_to_plain(Decimal(str(plan["limit_px"]))),
            "size": decimal_to_plain(Decimal(str(plan["size"]))),
            "mode": str(plan["mode"]),
            "reduce_only": bool(plan.get("reduce_only", False)),
            "plan": plan,
        }
        if state == "filled":
            order_state["filled_at"] = now
        if state == "error" and isinstance(status, dict):
            order_state["error"] = str(status.get("error"))
        levels.append(order_state)

    row_status = "active"
    fatal_errors = [
        status.get("error")
        for status in statuses
        if isinstance(status, dict) and status.get("error") and not status_is_post_only_reject(status)
    ]
    status_errors = [status.get("error") for status in statuses if isinstance(status, dict) and status.get("error")]
    if fatal_errors:
        row_status = "error"
    gap_rate = Decimal(str(plans[0].get("grid_gap", "0"))) if plans else Decimal("0")
    base_gap_rate = Decimal(str(plans[0].get("grid_base_gap", gap_rate))) if plans else Decimal("0")
    effective_gap_rate = Decimal(str(plans[0].get("grid_effective_gap", gap_rate))) if plans else Decimal("0")
    buy_sizes = [Decimal(str(plan.get("grid_buy_size", plan["size"]))) for plan in plans]
    sell_sizes = [Decimal(str(plan.get("grid_sell_size", plan["size"]))) for plan in plans]
    base_buy_sizes = [Decimal(str(plan.get("grid_base_buy_size", plan.get("grid_buy_size", plan["size"])))) for plan in plans]
    base_sell_sizes = [Decimal(str(plan.get("grid_base_sell_size", plan.get("grid_sell_size", plan["size"])))) for plan in plans]
    topup_buy_sizes = [Decimal(str(plan.get("grid_topup_buy_size", plan.get("grid_buy_size", plan["size"])))) for plan in plans]
    topup_sell_sizes = [Decimal(str(plan.get("grid_topup_sell_size", plan.get("grid_sell_size", plan["size"])))) for plan in plans]
    topup_buy_gaps = [Decimal(str(plan.get("grid_topup_buy_gap", gap_rate))) for plan in plans]
    topup_sell_gaps = [Decimal(str(plan.get("grid_topup_sell_gap", gap_rate))) for plan in plans]

    return {
        "id": f"{now}-grid-{coin}",
        "type": "grid",
        "status": row_status,
        "network": args.network,
        "account": account,
        "coin": coin,
        "raw_coin": args.coin,
        "dex": dex,
        "side": "grid",
        "position_limit_mode": getattr(args, "grid_position_limit_mode", "abs"),
        "min_position_value": decimal_to_plain(Decimal(str(getattr(args, "grid_position_min_value", None) or "0"))),
        "max_position_value": decimal_to_plain(max_position_value),
        "grid_tif": "Alo",
        "min_order_value": decimal_to_plain(grid_min_notional(args)),
        "gap": " ".join(grid_gap_spec(args)),
        "gap_rate": decimal_to_plain(base_gap_rate),
        "effective_gap_rate": decimal_to_plain(effective_gap_rate),
        "trend": args.trend or "0",
        "avg": str(args.grid_avg) if getattr(args, "grid_avg", None) is not None else None,
        "avg_multiplier": decimal_to_plain(Decimal(str(plans[0].get("grid_avg_multiplier", "1")))) if plans else "1",
        "avg_current_value": decimal_to_plain(Decimal(str(plans[0].get("grid_avg_current_value", "0")))) if plans else "0",
        "actual_trend": getattr(args, "resolved_grid_trend", "0%"),
        "target_orders_per_side": GRID_TARGET_ORDERS_PER_SIDE,
        "buy_size": decimal_to_plain(buy_sizes[0]) if buy_sizes else "",
        "sell_size": decimal_to_plain(sell_sizes[0]) if sell_sizes else "",
        "base_buy_size": decimal_to_plain(base_buy_sizes[0]) if base_buy_sizes else "",
        "base_sell_size": decimal_to_plain(base_sell_sizes[0]) if base_sell_sizes else "",
        "topup_buy_size": decimal_to_plain(topup_buy_sizes[0]) if topup_buy_sizes else "",
        "topup_sell_size": decimal_to_plain(topup_sell_sizes[0]) if topup_sell_sizes else "",
        "topup_buy_gap": decimal_to_plain(topup_buy_gaps[0]) if topup_buy_gaps else decimal_to_plain(base_gap_rate),
        "topup_sell_gap": decimal_to_plain(topup_sell_gaps[0]) if topup_sell_gaps else decimal_to_plain(base_gap_rate),
        "slippage": decimal_to_plain(slippage),
        "sz_decimals": int(asset["szDecimals"]),
        "created_at": now,
        "updated_at": now,
        "last_fill_check_ms": (now - 3600) * 1000,
        "levels": levels,
        "note": "; ".join(str(error) for error in status_errors) if status_errors else "active true grid",
    }


def print_grid_batch_status(row: dict[str, Any], price_rate: Decimal | None) -> None:
    open_oids = grid_batch_open_oids(row)
    rows = [
        ("coin", str(row.get("coin", ""))),
        ("status", str(row.get("status", ""))),
        ("limit", grid_limit_display(row)),
        ("min", str(row.get("min_order_value", MIN_NOTIONAL))),
        ("gap", str(row.get("gap", ""))),
        ("effective_gap", str(row.get("effective_gap_rate", row.get("gap_rate", "")))),
        ("avg", str(row.get("avg")) if row.get("avg") is not None else "-"),
        ("avg_multiplier", str(row.get("avg_multiplier", "1"))),
        ("orders", str(len(row.get("levels") or []))),
        ("open_oids", str(len(open_oids))),
        ("actual_trend", str(row.get("actual_trend", "0%"))),
    ]
    print_box("True Grid", rows)


def build_tpsl_child_plans(
    args: argparse.Namespace,
    exchange: Exchange,
    coin: str,
    asset: dict[str, Any],
    parent_is_buy: bool,
    size: Decimal,
    amount: Decimal,
    slippage: Decimal,
    base_px: Decimal | None,
) -> list[dict[str, Any]]:
    child_is_buy = not parent_is_buy
    parent_is_long = parent_is_buy
    plans: list[dict[str, Any]] = []
    if args.take_profit:
        take_trigger_px, take_limit_px, take_ratio = resolve_tpsl_spec(
            args.take_profit,
            args.take_profit_limit,
            base_px,
            "tp",
            parent_is_long,
        )
        take_trigger_px = rounded_perp_price(take_trigger_px, int(asset["szDecimals"]))
        if take_limit_px is not None:
            take_limit_px = rounded_perp_price(take_limit_px, int(asset["szDecimals"]))
        validate_tpsl_direction("tp", take_trigger_px, base_px, parent_is_long)
        plans.append(
            build_trigger_order_plan(
                coin,
                child_is_buy,
                amount,
                asset,
                exchange,
                slippage,
                "tp",
                take_trigger_px,
                take_limit_px,
                True,
                size=size,
                size_ratio=take_ratio,
            )
        )
    if args.stop_loss:
        stop_trigger_px, stop_limit_px, stop_ratio = resolve_tpsl_spec(
            args.stop_loss,
            args.stop_loss_limit,
            base_px,
            "sl",
            parent_is_long,
        )
        stop_trigger_px = rounded_perp_price(stop_trigger_px, int(asset["szDecimals"]))
        if stop_limit_px is not None:
            stop_limit_px = rounded_perp_price(stop_limit_px, int(asset["szDecimals"]))
        validate_tpsl_direction("sl", stop_trigger_px, base_px, parent_is_long)
        plans.append(
            build_trigger_order_plan(
                coin,
                child_is_buy,
                amount,
                asset,
                exchange,
                slippage,
                "sl",
                stop_trigger_px,
                stop_limit_px,
                True,
                size=size,
                size_ratio=stop_ratio,
            )
        )
    return plans


def submit_order_plans(
    exchange: Exchange,
    info: Info,
    account: str,
    coin: str,
    max_leverage: int,
    plans: list[dict[str, Any]],
    grouping: str,
    args: argparse.Namespace,
    price_rate: Decimal | None,
    title: str,
    update_leverage: bool = True,
) -> list[dict[str, Any]] | None:
    if args.explain:
        print_explain(title, plans, args, price_rate)
        return None

    if args.dry_run:
        print_account_metrics(info, account)
        print_box("Run", [("dry_run", "1")])
        print_order_plan_table(title, plans, price_rate)
        return None

    if update_leverage and not args.reduce_only:
        leverage_mode, leverage_result = update_order_leverage(exchange, max_leverage, coin)
        if args.verbose:
            print("leverage_mode:", leverage_mode)
            print("update_leverage_result:", leverage_result)
        if leverage_result.get("status") != "ok":
            raise RuntimeError(f"Failed to update {leverage_mode} leverage; order was not submitted.")

    requests = [order_plan_request(plan) for plan in plans]
    result = exchange.bulk_orders(requests, grouping=grouping)
    if args.verbose:
        print("order_result:", result)
    log_event("bulk_order_requests", requests)
    log_event("order_result", result)

    clear_info_cache(info)
    print_account_metrics(info, account)
    if result.get("status") != "ok":
        print("error:", result)
        return None

    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    print_order_plan_table(title, plans, price_rate, statuses)
    return statuses


def parse_trail_distance(value: str, anchor_px: Decimal) -> tuple[Decimal, str]:
    text = value.strip()
    if not text:
        raise ValueError("--trail is required")
    try:
        if text.endswith("%"):
            fraction = parse_percent_decimal(text, "--trail", allow_signed=False)
            if fraction <= 0:
                raise ValueError("--trail must be positive")
            return anchor_px * fraction, text
        distance = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("--trail must be a positive number or percent, e.g. 2% or 800") from exc
    if distance <= 0:
        raise ValueError("--trail must be positive")
    return distance, text


def trail_stop_price(best_px: Decimal, distance: Decimal, is_buy: bool, sz_decimals: int) -> Decimal:
    stop_px = best_px + distance if is_buy else best_px - distance
    if stop_px <= 0:
        raise ValueError("trail stop price must be positive")
    return rounded_perp_price(stop_px, sz_decimals)


def load_server_batch(path: Path = SERVER_BATCH_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text() or "[]")
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return [item for item in data if isinstance(item, dict)]


@contextlib.contextmanager
def server_batch_lock(path: Path = SERVER_BATCH_LOCK_PATH, blocking: bool = True):
    path.touch(exist_ok=True)
    with path.open("w") as lock:
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(lock, operation)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def args_need_server_batch_lock(args: argparse.Namespace) -> bool:
    return not args.query and bool(args.grid or args.trail or args.cancel is not None)


def save_server_batch(rows: list[dict[str, Any]], path: Path = SERVER_BATCH_PATH) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(json.dumps(rows, ensure_ascii=False, default=str, indent=2))
        tmp_path.replace(path)
    except PermissionError as exc:
        raise PermissionError(
            f"Cannot write server batch file {path}. "
            f"Make sure the command user and trail worker user can write this directory, "
            f"or fix ownership with: sudo chown -R $(whoami) {path.parent}"
        ) from exc
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def append_server_batch(row: dict[str, Any], path: Path = SERVER_BATCH_PATH) -> None:
    rows = load_server_batch(path)
    rows.append(row)
    save_server_batch(rows, path)


def batch_row_matches_coin(row: dict[str, Any], coin: str | None) -> bool:
    if coin is None:
        return True
    return position_matches_coin(str(row.get("coin", "")), coin)


def batch_row_matches_context(row: dict[str, Any], network: str, account: str | None, coin: str | None = None) -> bool:
    if str(row.get("network", "mainnet")) != network:
        return False
    row_account = str(row.get("account", ""))
    if account is not None and row_account and row_account.lower() != account.lower():
        return False
    return batch_row_matches_coin(row, coin)


def trail_batch_indexes(
    rows: list[dict[str, Any]],
    network: str,
    account: str | None,
    coin: str | None = None,
    statuses: set[str] | None = None,
) -> list[int]:
    return [
        index
        for index, row in enumerate(rows)
        if row.get("type") == "trail"
        and (statuses is None or str(row.get("status")) in statuses)
        and batch_row_matches_context(row, network, account, coin)
    ]


def active_trail_batch_indexes(rows: list[dict[str, Any]], network: str, account: str | None, coin: str | None = None) -> list[int]:
    return trail_batch_indexes(rows, network, account, coin, {"active"})


def cancellable_trail_batch_indexes(rows: list[dict[str, Any]], network: str, account: str | None, coin: str | None = None) -> list[int]:
    return trail_batch_indexes(rows, network, account, coin, {"active", "error"})


def grid_batch_indexes(
    rows: list[dict[str, Any]],
    network: str,
    account: str | None,
    coin: str | None = None,
    statuses: set[str] | None = None,
) -> list[int]:
    return [
        index
        for index, row in enumerate(rows)
        if row.get("type") == "grid"
        and (statuses is None or str(row.get("status")) in statuses)
        and batch_row_matches_context(row, network, account, coin)
    ]


def cancellable_grid_batch_indexes(rows: list[dict[str, Any]], network: str, account: str | None, coin: str | None = None) -> list[int]:
    return grid_batch_indexes(rows, network, account, coin, {"active", "error"})


def grid_batch_open_oids(row: dict[str, Any]) -> set[int]:
    oids: set[int] = set()
    for level in row.get("levels") or []:
        if not isinstance(level, dict):
            continue
        if level.get("side") and str(level.get("status", "active")) == "active":
            try:
                oids.add(int(level["oid"]))
            except (KeyError, TypeError, ValueError):
                pass
            continue
        for side in ("buy", "sell"):
            order = level.get(side)
            if not isinstance(order, dict) or str(order.get("status", "active")) != "active":
                continue
            try:
                oids.add(int(order["oid"]))
            except (KeyError, TypeError, ValueError):
                continue
    return oids


def format_server_batch_rows(rows: list[dict[str, Any]], network: str, account: str | None, coin: str | None = None) -> list[dict[str, str]]:
    display_rows: list[dict[str, str]] = []
    for row in rows:
        if not batch_row_matches_context(row, network, account, coin):
            continue
        is_grid = row.get("type") == "grid"
        limit = grid_limit_display(row) if is_grid else "-"
        trend = f"{row.get('trend', '0')} / {row.get('actual_trend', '0%')}" if is_grid else "-"
        display_rows.append(
            {
                "type": str(row.get("type", "")),
                "status": str(row.get("status", "")),
                "coin": str(row.get("coin", "")),
                "side": str(row.get("side", "grid" if is_grid else "")),
                "limit": limit,
                "trend": trend,
                "trail": str(row.get("trail", row.get("gap", "-"))),
                "bestPx": format_optional_decimal(row.get("best_px")),
                "stopPx": format_optional_decimal(row.get("stop_px")),
                "oid": str(row.get("oid", f"{len(grid_batch_open_oids(row))} open" if is_grid else "")),
                "updated": format_timestamp_ms(int(row.get("updated_at", 0)) * 1000) if row.get("updated_at") else "-",
            }
        )
    display_rows.sort(key=lambda item: (item["status"], item["type"], item["coin"], item["oid"]))
    return display_rows


def active_server_batch_oids(rows: list[dict[str, Any]], network: str, account: str | None, coin: str | None = None) -> set[int]:
    oids: set[int] = set()
    for row in rows:
        if str(row.get("status")) not in {"active", "error"}:
            continue
        if not batch_row_matches_context(row, network, account, coin):
            continue
        try:
            oids.add(int(row["oid"]))
        except (KeyError, TypeError, ValueError):
            pass
        oids.update(grid_batch_open_oids(row))
    return oids


def print_server_batch(rows: list[dict[str, Any]], network: str, account: str | None, coin: str | None = None) -> None:
    display_rows = format_server_batch_rows(rows, network, account, coin)
    if not display_rows:
        return
    print_table(
        "Server Batch",
        display_rows,
        [
            ("type", "type"),
            ("status", "status"),
            ("coin", "coin"),
            ("side", "side"),
            ("limit", "limit"),
            ("trend", "trend"),
            ("trail", "trail"),
            ("bestPx", "bestPx"),
            ("stopPx", "stopPx"),
            ("oid", "oid"),
            ("updated", "updated"),
        ],
        show_count=False,
    )


def cancel_trail_batch_orders(
    exchange: Exchange,
    info: Info,
    account: str,
    network: str,
    coin: str,
    dry_run: bool,
    price_rate: Decimal | None = None,
) -> None:
    batch_rows = load_server_batch()
    matching_indexes = cancellable_trail_batch_indexes(batch_rows, network, account, coin)

    if not matching_indexes:
        print_account_metrics(info, account)
        print_box("Cancel", [("coin", coin), ("filter", "trail"), ("cancelled", "0")])
        return

    cancel_requests = [
        {"coin": str(batch_rows[index].get("coin", coin)), "oid": int(batch_rows[index]["oid"])}
        for index in matching_indexes
    ]

    if dry_run:
        print_account_metrics(info, account)
        print_box("Run", [("dry_run", "1"), ("filter", "trail"), ("matched", str(len(matching_indexes)))])
        print_server_batch([batch_rows[index] for index in matching_indexes], network, account, coin)
        return

    result = exchange.bulk_cancel(cancel_requests)
    log_event("cancel_trail_batch_requests", cancel_requests)
    log_event("cancel_trail_batch_result", result)
    if result.get("status") != "ok":
        print_account_metrics(info, account)
        print("error:", result)
        return
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    status_errors = [status.get("error") for status in statuses if isinstance(status, dict) and status.get("error")]
    if status_errors:
        print_account_metrics(info, account)
        print("error:", status_errors)
        return

    now = int(time.time())
    for index in matching_indexes:
        batch_rows[index]["status"] = "cancelled"
        batch_rows[index]["cancelled_at"] = now
        batch_rows[index]["updated_at"] = now
        batch_rows[index]["note"] = "cancelled by --cancel trail"
    save_server_batch(batch_rows)

    clear_info_cache(info)
    print_account_metrics(info, account)
    print_box("Cancel", [("coin", coin), ("filter", "trail"), ("cancelled", str(len(matching_indexes)))])
    print_server_batch([batch_rows[index] for index in matching_indexes], network, account, coin)


def cancel_grid_batch_orders(
    exchange: Exchange,
    info: Info,
    account: str,
    network: str,
    coin: str,
    dry_run: bool,
    price_rate: Decimal | None = None,
) -> None:
    batch_rows = load_server_batch()
    matching_indexes = cancellable_grid_batch_indexes(batch_rows, network, account, coin)
    cancel_requests: list[dict[str, Any]] = []
    for index in matching_indexes:
        row_coin = str(batch_rows[index].get("coin", coin))
        for oid in sorted(grid_batch_open_oids(batch_rows[index])):
            cancel_requests.append({"coin": row_coin, "oid": oid})

    if not matching_indexes:
        print_account_metrics(info, account)
        print_box("Cancel", [("coin", coin), ("filter", "grid"), ("cancelled", "0")])
        return

    if dry_run:
        print_account_metrics(info, account)
        print_box("Run", [("dry_run", "1"), ("filter", "grid"), ("matched", str(len(matching_indexes))), ("open_oids", str(len(cancel_requests)))])
        print_server_batch([batch_rows[index] for index in matching_indexes], network, account, coin)
        return

    result = exchange.bulk_cancel(cancel_requests) if cancel_requests else {"status": "ok", "response": {"data": {"statuses": []}}}
    log_event("cancel_grid_batch_requests", cancel_requests)
    log_event("cancel_grid_batch_result", result)
    if result.get("status") != "ok":
        print_account_metrics(info, account)
        print("error:", result)
        return
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    status_errors = [status.get("error") for status in statuses if isinstance(status, dict) and status.get("error")]
    if status_errors:
        print_account_metrics(info, account)
        print("error:", status_errors)
        return

    now = int(time.time())
    cancelled_oids = {int(request["oid"]) for request in cancel_requests}
    for index in matching_indexes:
        row = batch_rows[index]
        row["status"] = "cancelled"
        row["cancelled_at"] = now
        row["updated_at"] = now
        row["note"] = "cancelled by --cancel grid"
        for level in row.get("levels") or []:
            if not isinstance(level, dict):
                continue
            if level.get("side"):
                try:
                    oid = int(level["oid"])
                except (KeyError, TypeError, ValueError):
                    continue
                if oid in cancelled_oids:
                    level["status"] = "cancelled"
                    level["cancelled_at"] = now
                continue
            for side in ("buy", "sell"):
                order = level.get(side)
                if not isinstance(order, dict):
                    continue
                try:
                    oid = int(order["oid"])
                except (KeyError, TypeError, ValueError):
                    continue
                if oid in cancelled_oids:
                    order["status"] = "cancelled"
                    order["cancelled_at"] = now
    save_server_batch(batch_rows)

    clear_info_cache(info)
    print_account_metrics(info, account)
    print_box("Cancel", [("coin", coin), ("filter", "grid"), ("cancelled", str(len(cancel_requests)))])
    print_server_batch([batch_rows[index] for index in matching_indexes], network, account, coin)


def modify_grid_batch_order(
    args: argparse.Namespace,
    exchange: Exchange,
    info: Info,
    account: str,
    coin: str,
    dex: str,
    asset: dict[str, Any],
    max_leverage: int,
    current_mid: Decimal | None,
    slippage: Decimal,
    price_rate: Decimal | None,
) -> None:
    batch_rows = load_server_batch()
    matching_indexes = grid_batch_indexes(batch_rows, args.network, account, coin, {"active", "error"})
    if not matching_indexes:
        print_account_metrics(info, account)
        print_box("Grid Modify", [("coin", coin), ("matched", "0")])
        return
    if len(matching_indexes) > 1:
        raise ValueError(f"multiple active grid batches found for {coin}; cancel extras before modifying")

    index = matching_indexes[0]
    row = batch_rows[index]
    now = int(time.time())
    old_limit_mode = grid_limit_policy_from_row(row)
    old_min_position_value = Decimal(str(row.get("min_position_value") or "0"))
    new_limit_mode = str(args.grid_position_limit_mode) if args.grid_position_limit_value is not None else old_limit_mode
    new_min_position_value = (
        Decimal(str(args.grid_position_min_value or "0"))
        if args.grid_position_limit_value is not None
        else old_min_position_value
    )
    gap_changed = bool(args.gap)
    avg_changed = args.grid_avg is not None
    updates: list[tuple[str, str]] = []
    if args.grid_position_limit_value is not None:
        row["position_limit_mode"] = new_limit_mode
        row["min_position_value"] = decimal_to_plain(new_min_position_value)
        row["max_position_value"] = decimal_to_plain(Decimal(str(args.grid_position_limit_value)))
        updates.append(("limit", grid_limit_display(row)))
        if new_limit_mode != old_limit_mode:
            updates.append(("mode_change", f"{old_limit_mode}->{new_limit_mode}"))
    if args.grid_min is not None:
        row["min_order_value"] = decimal_to_plain(grid_min_notional(args))
        updates.append(("min", row["min_order_value"]))
    if gap_changed:
        gap = resolve_grid_spacing(args, info, account, asset, dex, current_mid or Decimal("1"))
        row["gap"] = " ".join(grid_gap_spec(args))
        row["gap_rate"] = decimal_to_plain(gap)
        updates.append(("gap", row["gap"]))
    if args.trend is not None:
        row["trend"] = args.trend or "0"
        row["avg"] = None
        updates.append(("trend", row["trend"]))
    if avg_changed:
        avg_value = Decimal(str(args.grid_avg))
        avg_lower, avg_upper = grid_avg_bounds(
            new_limit_mode,
            new_min_position_value,
            Decimal(str(row.get("max_position_value"))),
        )
        if not avg_value.is_finite() or avg_value < avg_lower or avg_value > avg_upper:
            raise ValueError(
                f"--avg must be between {decimal_to_plain(avg_lower)} and {decimal_to_plain(avg_upper)} for --{new_limit_mode}"
            )
        row["avg"] = decimal_to_plain(avg_value)
        row["trend"] = "0"
        updates.append(("avg", row["avg"]))

    args.grid_position_limit_mode = grid_limit_policy_from_row(row)
    args.grid_position_min_value = str(row.get("min_position_value") or "0")
    args.grid_position_limit_value = str(row.get("max_position_value"))
    if args.grid_min is None and row.get("min_order_value") is not None:
        args.grid_min = str(row.get("min_order_value"))
    if not gap_changed:
        saved_gap_rate = Decimal(str(row.get("gap_rate")))
        args.gap = [f"{decimal_to_plain(saved_gap_rate * Decimal('100'))}%"]
        args.resolved_grid_gap_spec = [str(row.get("gap") or args.gap[0])]
    if args.trend is None:
        args.trend = str(row.get("trend") or "0")
    if args.grid_avg is None and row.get("avg") is not None:
        args.grid_avg = str(row["avg"])
    if args.grid_avg is not None:
        effective_avg = Decimal(str(args.grid_avg))
        effective_lower, effective_upper = grid_avg_bounds(
            args.grid_position_limit_mode,
            Decimal(str(args.grid_position_min_value or "0")),
            Decimal(str(args.grid_position_limit_value)),
        )
        if not effective_avg.is_finite() or effective_avg < effective_lower or effective_avg > effective_upper:
            raise ValueError(
                f"--avg must be between {decimal_to_plain(effective_lower)} and {decimal_to_plain(effective_upper)} "
                f"for --{args.grid_position_limit_mode}"
            )

    if current_mid is None:
        raise ValueError(f"No mid price found for {coin}, cannot modify grid strategy parameters")
    position_size, position_value = current_position_size_value(info, account, coin, dex, current_mid)
    refresh_grid_row_strategy_params(row, asset, current_mid, position_size, position_value)

    if args.explain or args.dry_run:
        print_account_metrics(info, account)
        print_box(
            "Grid Modify",
            [
                ("dry_run", "1" if args.dry_run else "0"),
                ("coin", coin),
                ("regrid", "0"),
                ("updates", ", ".join(f"{key}={value}" for key, value in updates) or "-"),
            ],
        )
        print_server_batch([row], args.network, account, coin)
        return

    row["status"] = "active"
    row["updated_at"] = now
    row["note"] = "modified grid config; existing orders kept"
    batch_rows[index] = row
    save_server_batch(batch_rows)
    print_grid_batch_status(row, price_rate)


def recoverable_grid_open_orders(
    info: Info,
    account: str,
    dex: str,
    coin: str,
    network: str,
) -> list[dict[str, Any]]:
    batch_oids = active_server_batch_oids(load_server_batch(), network, account, coin)
    orders: list[dict[str, Any]] = []
    for order in collect_frontend_open_orders(info, account, dex):
        if not position_matches_coin(str(order.get("coin", "")), coin):
            continue
        if order.get("isTrigger") or decimal_or_none(order.get("triggerPx")) not in {None, Decimal("0")}:
            continue
        try:
            oid = int(order["oid"])
        except (KeyError, TypeError, ValueError):
            continue
        if oid in batch_oids:
            continue
        price = decimal_or_none(order.get("limitPx"))
        size = decimal_or_none(order.get("sz", order.get("origSz")))
        if price is None or price <= 0 or size is None or size <= 0:
            continue
        side = str(order.get("side", "")).strip().upper()
        if side not in {"B", "A"}:
            continue
        orders.append({**order, "oid": oid, "_price": price, "_size": size, "_is_buy": side == "B"})
    return orders


def select_grid_recovery_orders(orders: list[dict[str, Any]], target_per_side: int) -> list[dict[str, Any]]:
    buys = sorted((order for order in orders if order["_is_buy"]), key=lambda order: order["_price"], reverse=True)
    sells = sorted((order for order in orders if not order["_is_buy"]), key=lambda order: order["_price"])
    return buys[:target_per_side] + sells[:target_per_side]


def build_recovered_grid_batch_row(
    args: argparse.Namespace,
    account: str,
    coin: str,
    dex: str,
    asset: dict[str, Any],
    orders: list[dict[str, Any]],
    max_position_value: Decimal,
    gap_rate: Decimal,
    slippage: Decimal,
    current_mid: Decimal,
) -> dict[str, Any]:
    now = int(time.time())
    levels: list[dict[str, Any]] = []
    for index, order in enumerate(orders, start=1):
        is_buy = bool(order["_is_buy"])
        side = "buy" if is_buy else "sell"
        plan = build_grid_limit_order_plan(
            coin,
            is_buy,
            Decimal(str(order["_size"])),
            Decimal(str(order["_price"])),
            asset,
            bool(order.get("reduceOnly", False)),
            side,
        )
        plan["grid_gap"] = gap_rate
        levels.append(
            {
                "index": index,
                "side": side,
                "status": "active",
                "oid": int(order["oid"]),
                "is_buy": is_buy,
                "limit_px": decimal_to_plain(Decimal(str(order["_price"]))),
                "price": decimal_to_plain(Decimal(str(order["_price"]))),
                "size": decimal_to_plain(Decimal(str(order["_size"]))),
                "mode": "grid-limit",
                "reduce_only": bool(order.get("reduceOnly", False)),
                "recovered_at": now,
                "plan": plan,
            }
        )

    buy_sizes = [Decimal(str(order["_size"])) for order in orders if order["_is_buy"]]
    sell_sizes = [Decimal(str(order["_size"])) for order in orders if not order["_is_buy"]]
    buy_size = buy_sizes[0] if buy_sizes else Decimal("0")
    sell_size = sell_sizes[0] if sell_sizes else Decimal("0")
    if buy_size <= 0 or sell_size <= 0:
        sz_decimals = int(asset["szDecimals"])
        buy_px = rounded_perp_price(current_mid * (Decimal("1") - gap_rate), sz_decimals)
        sell_px = rounded_perp_price(current_mid * (Decimal("1") + gap_rate), sz_decimals)
        inferred_buy_size, inferred_sell_size = grid_limit_size_pair_for_trend(
            parse_percent_decimal(args.trend or "0", "--trend", allow_signed=True),
            sz_decimals,
            buy_px,
            sell_px,
            grid_min_notional(args),
        )
        if buy_size <= 0:
            buy_size = inferred_buy_size
        if sell_size <= 0:
            sell_size = inferred_sell_size
    base_buy_size = buy_size
    base_sell_size = sell_size
    if getattr(args, "grid_avg", None) is not None:
        balanced_size = max(buy_size, sell_size)
        base_buy_size = balanced_size
        base_sell_size = balanced_size
    actual_trend = "0%"
    if buy_size > 0 and sell_size > 0:
        if buy_size > sell_size:
            actual_trend = format_signed_percent(buy_size / sell_size - Decimal("1"))
        elif sell_size > buy_size:
            actual_trend = format_signed_percent(-(sell_size / buy_size - Decimal("1")))

    return {
        "id": f"{now}-grid-{coin}-recovered",
        "type": "grid",
        "status": "active",
        "network": args.network,
        "account": account,
        "coin": coin,
        "raw_coin": args.coin,
        "dex": dex,
        "side": "grid",
        "position_limit_mode": getattr(args, "grid_position_limit_mode", "abs"),
        "min_position_value": decimal_to_plain(Decimal(str(getattr(args, "grid_position_min_value", None) or "0"))),
        "max_position_value": decimal_to_plain(max_position_value),
        "grid_tif": "Alo",
        "min_order_value": decimal_to_plain(grid_min_notional(args)),
        "gap": " ".join(grid_gap_spec(args)),
        "gap_rate": decimal_to_plain(gap_rate),
        "effective_gap_rate": decimal_to_plain(gap_rate),
        "trend": args.trend or "0",
        "avg": str(args.grid_avg) if getattr(args, "grid_avg", None) is not None else None,
        "avg_multiplier": "1",
        "avg_current_value": "0",
        "actual_trend": actual_trend,
        "target_orders_per_side": GRID_TARGET_ORDERS_PER_SIDE,
        "buy_size": decimal_to_plain(buy_size),
        "sell_size": decimal_to_plain(sell_size),
        "base_buy_size": decimal_to_plain(base_buy_size),
        "base_sell_size": decimal_to_plain(base_sell_size),
        "topup_buy_size": decimal_to_plain(base_buy_size),
        "topup_sell_size": decimal_to_plain(base_sell_size),
        "topup_buy_gap": decimal_to_plain(gap_rate),
        "topup_sell_gap": decimal_to_plain(gap_rate),
        "slippage": decimal_to_plain(slippage),
        "sz_decimals": int(asset["szDecimals"]),
        "created_at": now,
        "updated_at": now,
        "last_fill_check_ms": (now - 24 * 60 * 60) * 1000,
        "levels": levels,
        "note": "recovered from open limit orders",
    }


def recover_grid_batch_order(
    args: argparse.Namespace,
    info: Info,
    account: str,
    coin: str,
    dex: str,
    asset: dict[str, Any],
    current_mid: Decimal | None,
    slippage: Decimal,
    price_rate: Decimal | None,
) -> None:
    if current_mid is None:
        raise ValueError(f"No mid price found for {coin}, cannot recover grid")
    existing = grid_batch_indexes(load_server_batch(), args.network, account, coin, {"active", "error"})
    if existing:
        raise ValueError(f"active grid batch already exists for {coin}; use --modify or --cancel grid first")
    max_position_value = Decimal(str(args.grid_position_limit_value))
    gap_rate = resolve_grid_spacing(args, info, account, asset, dex, current_mid)
    orders = select_grid_recovery_orders(
        recoverable_grid_open_orders(info, account, dex, coin, args.network),
        GRID_TARGET_ORDERS_PER_SIDE,
    )
    if not orders:
        print_account_metrics(info, account)
        print_box("Grid Recover", [("coin", coin), ("matched", "0")])
        return
    row = build_recovered_grid_batch_row(args, account, coin, dex, asset, orders, max_position_value, gap_rate, slippage, current_mid)
    if args.dry_run or args.explain:
        print_account_metrics(info, account)
        print_box("Grid Recover", [("dry_run", "1" if args.dry_run else "0"), ("coin", coin), ("matched", str(len(orders)))])
        print_grid_batch_status(row, price_rate)
        return
    append_server_batch(row)
    print_grid_batch_status(row, price_rate)


def mark_cancelled_server_batch_oids(
    network: str,
    account: str,
    oids: set[int],
    note: str,
) -> int:
    if not oids:
        return 0
    batch_rows = load_server_batch()
    now = int(time.time())
    updated = 0
    for row in batch_rows:
        if str(row.get("status")) not in {"active", "error"}:
            continue
        if not batch_row_matches_context(row, network, account):
            continue
        try:
            oid = int(row["oid"])
        except (KeyError, TypeError, ValueError):
            oid = None
        if oid is not None and oid in oids:
            row["status"] = "cancelled"
            row["cancelled_at"] = now
            row["updated_at"] = now
            row["note"] = note
            updated += 1
            continue
        if row.get("type") != "grid":
            continue
        matched = False
        for level in row.get("levels") or []:
            if not isinstance(level, dict):
                continue
            if level.get("side"):
                try:
                    child_oid = int(level["oid"])
                except (KeyError, TypeError, ValueError):
                    continue
                if child_oid in oids:
                    level["status"] = "cancelled"
                    level["cancelled_at"] = now
                    matched = True
                continue
            for side in ("buy", "sell"):
                order = level.get(side)
                if not isinstance(order, dict):
                    continue
                try:
                    child_oid = int(order["oid"])
                except (KeyError, TypeError, ValueError):
                    continue
                if child_oid not in oids:
                    continue
                order["status"] = "cancelled"
                order["cancelled_at"] = now
                matched = True
        if matched:
            row["status"] = "cancelled"
            row["cancelled_at"] = now
            row["updated_at"] = now
            row["note"] = note
            updated += 1
    if updated:
        save_server_batch(batch_rows)
    return updated


def submit_trail_stop_order(
    args: argparse.Namespace,
    exchange: Exchange,
    info: Info,
    account: str,
    coin: str,
    asset: dict[str, Any],
    is_buy: bool,
    amount: Decimal,
    stop_px: Decimal,
    slippage: Decimal,
) -> tuple[int, dict[str, Any]]:
    plan = build_trigger_order_plan(
        coin,
        is_buy,
        amount,
        asset,
        exchange,
        slippage,
        "trail-stop",
        stop_px,
        None,
        args.reduce_only,
        tpsl="sl",
    )
    if args.dry_run:
        return 0, {"dryRun": True, "plan": plan}
    result = exchange.order(
        coin,
        is_buy,
        float(plan["size"]),
        float(plan["limit_px"]),
        plan["order_type"],
        reduce_only=args.reduce_only,
    )
    log_event("trail_stop_order", {"stop_px": decimal_to_plain(stop_px), "result": result})
    if result.get("status") != "ok":
        raise RuntimeError(f"Failed to submit trail stop: {result}")
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    for status in statuses:
        if "error" in status:
            raise RuntimeError(f"Failed to submit trail stop: {status['error']}")
        if "resting" in status:
            return int(status["resting"]["oid"]), plan
        if "filled" in status:
            return int(status["filled"].get("oid", 0)), plan
    raise RuntimeError(f"Trail stop response did not include an order id: {result}")


def print_trail_status(
    coin: str,
    is_buy: bool,
    mid_px: Decimal,
    best_px: Decimal,
    stop_px: Decimal | None,
    oid: int | None,
    status: str,
    price_rate: Decimal | None,
) -> None:
    rows = [
        ("coin", coin),
        ("side", side_code(is_buy)),
        ("midPx", format_price(mid_px, price_rate)),
        ("bestPx", format_price(best_px, price_rate)),
        ("stopPx", "-" if stop_px is None else format_price(stop_px, price_rate)),
        ("oid", "-" if oid is None else str(oid)),
        ("status", status),
    ]
    print_box("Trail", rows)


def run_trailing_order(
    args: argparse.Namespace,
    exchange: Exchange,
    info: Info,
    account: str,
    coin: str,
    dex: str,
    asset: dict[str, Any],
    is_buy: bool,
    amount: Decimal,
    current_mid: Decimal | None,
    slippage: Decimal,
    price_rate: Decimal | None,
) -> None:
    if current_mid is None:
        raise ValueError(f"No mid price found for {coin}, cannot start trail order")

    sz_decimals = int(asset["szDecimals"])
    best_px = rounded_perp_price(current_mid, sz_decimals)
    distance, distance_label = parse_trail_distance(args.trail, best_px)
    distance = rounded_perp_price(distance, sz_decimals)
    stop_px = trail_stop_price(best_px, distance, is_buy, sz_decimals)

    if args.explain or args.dry_run:
        print_trail_status(
            coin,
            is_buy,
            current_mid,
            best_px,
            stop_px,
            None,
            "dry_run" if args.dry_run else "planned",
            price_rate,
        )
        return

    stop_oid, plan = submit_trail_stop_order(args, exchange, info, account, coin, asset, is_buy, amount, stop_px, slippage)
    row = {
        "id": f"{int(time.time())}-{stop_oid}",
        "type": "trail",
        "status": "active",
        "network": args.network,
        "account": account,
        "coin": coin,
        "raw_coin": args.coin,
        "dex": dex,
        "side": side_code(is_buy),
        "is_buy": is_buy,
        "amount": decimal_to_plain(amount),
        "trail": distance_label,
        "trail_distance": decimal_to_plain(distance),
        "best_px": decimal_to_plain(best_px),
        "stop_px": decimal_to_plain(stop_px),
        "oid": stop_oid,
        "reduce_only": args.reduce_only,
        "slippage": decimal_to_plain(slippage),
        "size": decimal_to_plain(Decimal(str(plan["size"]))),
        "sz_decimals": sz_decimals,
        "updated_at": int(time.time()),
        "plan": plan,
    }
    append_server_batch(row)
    print_trail_status(coin, is_buy, current_mid, best_px, stop_px, stop_oid, "batched", price_rate)


def place_protective_tpsl_orders(
    args: argparse.Namespace,
    exchange: Exchange,
    info: Info,
    account: str,
    coin: str,
    dex: str,
    asset: dict[str, Any],
    is_buy: bool,
    amount: Decimal,
    slippage: Decimal,
    max_leverage: int,
    price_rate: Decimal | None,
) -> None:
    position = find_current_position(info, account, coin, dex)
    position_base_px = Decimal(str(position.get("entryPx"))) if position and position.get("entryPx") is not None else None
    position_is_long = not is_buy
    plans: list[dict[str, Any]] = []
    if args.take_profit:
        take_trigger_px, take_limit_px, take_ratio = resolve_tpsl_spec(
            args.take_profit,
            args.take_profit_limit,
            position_base_px,
            "tp",
            position_is_long,
        )
        take_trigger_px = rounded_perp_price(take_trigger_px, int(asset["szDecimals"]))
        if take_limit_px is not None:
            take_limit_px = rounded_perp_price(take_limit_px, int(asset["szDecimals"]))
        validate_tpsl_direction("tp", take_trigger_px, position_base_px, position_is_long)
        plans.append(
            build_trigger_order_plan(
                coin,
                is_buy,
                amount,
                asset,
                exchange,
                slippage,
                "tp",
                take_trigger_px,
                take_limit_px,
                True,
                size_ratio=take_ratio,
            )
        )
    if args.stop_loss:
        stop_trigger_px, stop_limit_px, stop_ratio = resolve_tpsl_spec(
            args.stop_loss,
            args.stop_loss_limit,
            position_base_px,
            "sl",
            position_is_long,
        )
        stop_trigger_px = rounded_perp_price(stop_trigger_px, int(asset["szDecimals"]))
        if stop_limit_px is not None:
            stop_limit_px = rounded_perp_price(stop_limit_px, int(asset["szDecimals"]))
        validate_tpsl_direction("sl", stop_trigger_px, position_base_px, position_is_long)
        plans.append(
            build_trigger_order_plan(
                coin,
                is_buy,
                amount,
                asset,
                exchange,
                slippage,
                "sl",
                stop_trigger_px,
                stop_limit_px,
                True,
                size_ratio=stop_ratio,
            )
        )
    if not plans:
        raise ValueError("at least one of --tp or --sl is required")
    submit_order_plans(
        exchange,
        info,
        account,
        coin,
        max_leverage,
        plans,
        "positionTpsl",
        args,
        price_rate,
        "Protective TP/SL",
    )


def place_scale_orders(
    args: argparse.Namespace,
    exchange: Exchange,
    info: Info,
    account: str,
    coin: str,
    asset: dict[str, Any],
    is_buy: bool,
    amount: Decimal,
    current_mid: Decimal | None,
    max_leverage: int,
    price_rate: Decimal | None,
) -> None:
    scale_count = int(args.scale)
    amount_each = amount / Decimal(scale_count)
    if amount_each < MIN_NOTIONAL:
        raise ValueError(f"Scale amount is too small: each order must be at least {MIN_NOTIONAL} USD")

    sz_decimals = int(asset["szDecimals"])
    prices = scale_prices(Decimal(args.scale_from), Decimal(args.scale_to), scale_count, sz_decimals)
    plans: list[dict[str, Any]] = []
    for index, price in enumerate(prices, start=1):
        min_value_price = min(price, current_mid) if current_mid is not None else price
        size, notional, target_notional, minimum_value_notional = calc_size(
            amount_each,
            price,
            sz_decimals,
            min_value_price,
        )
        plans.append(
            {
                "label": f"{index}/{scale_count}",
                "coin": coin,
                "is_buy": is_buy,
                "size": size,
                "limit_px": price,
                "order_type": {"limit": {"tif": args.tif or "Alo"}},
                "reduce_only": args.reduce_only,
                "mode": "scale",
                "notional": notional,
                "target_notional": target_notional,
                "minimum_value_notional": minimum_value_notional,
                "min_value_price": min_value_price,
            }
        )

    submit_order_plans(
        exchange,
        info,
        account,
        coin,
        max_leverage,
        plans,
        "na",
        args,
        price_rate,
        "Scale Orders",
    )


def place_symmetric_orders(
    args: argparse.Namespace,
    exchange: Exchange,
    info: Info,
    account: str,
    coin: str,
    asset: dict[str, Any],
    amount: Decimal,
    current_mid: Decimal | None,
    slippage: Decimal,
    max_leverage: int,
    price_rate: Decimal | None,
) -> None:
    sz_decimals = int(asset["szDecimals"])
    has_tpsl = bool(args.take_profit or args.stop_loss)
    if args.price:
        base_price = Decimal(args.price)
        price_source = "user center"
    else:
        if current_mid is None:
            raise ValueError(f"No mid price found for {coin}, cannot place symmetric orders")
        base_price = current_mid
        price_source = "current mid"

    base_price = rounded_perp_price(base_price, sz_decimals)
    offset = resolve_symmetric_offset(base_price, args.symmetric_offset)
    buy_price_raw = base_price - offset
    if buy_price_raw <= 0:
        raise ValueError("Symmetric buy price must be positive; use a smaller --offset")
    buy_price = rounded_perp_price(buy_price_raw, sz_decimals)
    sell_price = rounded_perp_price(base_price + offset, sz_decimals)
    if buy_price >= sell_price:
        raise ValueError("Symmetric prices collapse after rounding; use a larger --offset")

    amount_each = amount
    if args.amount_is_total:
        amount_each = amount / Decimal("2")
        if amount_each < MIN_NOTIONAL:
            raise ValueError(f"Symmetric total is too small: each order must be at least {MIN_NOTIONAL} USD")

    plans = [
        build_limit_order_plan(
            coin,
            True,
            amount_each,
            asset,
            buy_price,
            False,
            args.tif,
            current_mid,
            label="buy -offset",
            price_source=f"{price_source} - {args.symmetric_offset}",
        ),
        build_limit_order_plan(
            coin,
            False,
            amount_each,
            asset,
            sell_price,
            False,
            args.tif,
            current_mid,
            label="sell +offset",
            price_source=f"{price_source} + {args.symmetric_offset}",
        ),
    ]
    for plan in plans:
        plan["mode"] = "symmetric"

    grouped_plans: list[list[dict[str, Any]]] = []
    for index, entry_plan in enumerate(plans, start=1):
        group_plans = [entry_plan]
        if has_tpsl:
            child_plans = build_tpsl_child_plans(
                args,
                exchange,
                coin,
                asset,
                bool(entry_plan["is_buy"]),
                entry_plan["size"],
                amount_each,
                slippage,
                Decimal(str(entry_plan["reference_price"])),
            )
            for child in child_plans:
                child["label"] = f"{index}/2 {child['label']}"
            group_plans.extend(child_plans)
        grouped_plans.append(group_plans)

    if args.verbose:
        print("base_price:", decimal_to_plain(base_price))
        print("offset:", decimal_to_plain(offset))
        print("buy_price:", decimal_to_plain(buy_price))
        print("sell_price:", decimal_to_plain(sell_price))

    if args.explain:
        display_plans = plans if not has_tpsl else [plan for group in grouped_plans for plan in group]
        print_explain("Symmetric Orders Bracket" if has_tpsl else "Symmetric Orders", display_plans, args, price_rate)
        return

    if args.dry_run:
        display_plans = plans if not has_tpsl else [plan for group in grouped_plans for plan in group]
        print_account_metrics(info, account)
        print_box("Run", [("dry_run", "1")])
        print_order_plan_table("Symmetric Orders Bracket" if has_tpsl else "Symmetric Orders", display_plans, price_rate)
        return

    if has_tpsl:
        for index, group_plans in enumerate(grouped_plans):
            submit_order_plans(
                exchange,
                info,
                account,
                coin,
                max_leverage,
                group_plans,
                "normalTpsl",
                args,
                price_rate,
                "Symmetric Orders Bracket",
                update_leverage=index == 0,
            )
        return

    submit_order_plans(
        exchange,
        info,
        account,
        coin,
        max_leverage,
        plans,
        "na",
        args,
        price_rate,
        "Symmetric Orders",
    )


def place_ladder_orders(
    args: argparse.Namespace,
    exchange: Exchange,
    info: Info,
    account: str,
    coin: str,
    asset: dict[str, Any],
    is_buy: bool,
    amount: Decimal,
    current_mid: Decimal | None,
    slippage: Decimal,
    max_leverage: int,
    price_rate: Decimal | None,
) -> None:
    sz_decimals = int(asset["szDecimals"])
    has_tpsl = bool(args.take_profit or args.stop_loss)
    ladder_mode = args.ladder_mode
    ladder_step_spec = str(args.ladder_step)
    trigger_mode = None
    trigger_limit_offset: Decimal | None = None
    if args.stop_entry:
        trigger_mode = "stop-entry"
        trigger_start_px, trigger_limit_px = parse_entry_trigger_with_limit(args.stop_entry, args.stop_entry_limit, "stop")
        base_price = rounded_perp_price(trigger_start_px, sz_decimals)
        if trigger_limit_px is not None:
            trigger_limit_offset = rounded_perp_price(trigger_limit_px, sz_decimals) - base_price
        price_source = "stop-entry anchor"
    elif args.take_entry:
        trigger_mode = "take-entry"
        trigger_start_px, trigger_limit_px = parse_entry_trigger_with_limit(args.take_entry, args.take_entry_limit, "take")
        base_price = rounded_perp_price(trigger_start_px, sz_decimals)
        if trigger_limit_px is not None:
            trigger_limit_offset = rounded_perp_price(trigger_limit_px, sz_decimals) - base_price
        price_source = "take-entry anchor"
    elif args.price:
        base_price = Decimal(args.price)
        price_source = "user"
        base_price = rounded_perp_price(base_price, sz_decimals)
    else:
        base_price = same_side_book_price(info, coin, is_buy, args.book_level)
        price_source = f"same-side book level {args.book_level}"
        base_price = rounded_perp_price(base_price, sz_decimals)

    if ladder_mode == "for":
        step = resolve_ladder_step(base_price, ladder_step_spec, "ladder")
        ladder_count = int(args.ladder_count)
        prices = ladder_for_prices(base_price, ladder_count, step, sz_decimals, "ladder")
    elif ladder_mode == "while":
        step = resolve_ladder_step(base_price, ladder_step_spec, "ladder")
        ladder_end_px = rounded_perp_price(Decimal(str(args.ladder_end)), sz_decimals)
        prices = ladder_while_prices(base_price, ladder_end_px, step, sz_decimals, "ladder")
    elif ladder_mode == "count_to_end":
        ladder_count = int(args.ladder_count)
        ladder_end_px = rounded_perp_price(Decimal(str(args.ladder_end)), sz_decimals)
        prices = ladder_count_to_end_prices(base_price, ladder_end_px, ladder_count, sz_decimals, "ladder")
    else:
        raise ValueError("Ladder orders require --for, --while, or --while END --for COUNT syntax")

    amount_each = amount
    if args.amount_is_total:
        amount_each = amount / Decimal(len(prices))
        if amount_each < MIN_NOTIONAL:
            raise ValueError(f"Ladder total is too small: each order must be at least {MIN_NOTIONAL} USD")

    if trigger_mode and has_tpsl:
        raise ValueError(
            "Ladder trigger orders cannot be combined with --tp/--sl in a single submit. "
            "Hyperliquid normalTpsl requires a non-trigger main order. "
            "Use trigger ladders alone, or place TP/SL separately after fill."
        )

    if trigger_mode:
        title = "Ladder Trigger Orders Bracket" if has_tpsl else "Ladder Trigger Orders"
        prefix = f"ladder-{trigger_mode}"
    else:
        title = "Ladder Orders Bracket" if has_tpsl else "Ladder Orders"
        prefix = "ladder-count-to-end" if ladder_mode == "count_to_end" else f"ladder-{ladder_mode}"

    plans: list[dict[str, Any]] = []
    grouped_plans: list[list[dict[str, Any]]] = []
    for index, price in enumerate(prices, start=1):
        if trigger_mode:
            trigger_limit_px = None if trigger_limit_offset is None else rounded_perp_price(price + trigger_limit_offset, sz_decimals)
            entry_plan = build_trigger_order_plan(
                coin,
                is_buy,
                amount_each,
                asset,
                exchange,
                slippage,
                trigger_mode,
                price,
                trigger_limit_px,
                args.reduce_only,
            )
            entry_plan["label"] = f"{index}/{len(prices)} {trigger_mode}"
            entry_plan["price_source"] = f"{price_source} {prefix} {index}/{len(prices)}"
        else:
            entry_plan = build_limit_order_plan(
                coin,
                is_buy,
                amount_each,
                asset,
                price,
                args.reduce_only,
                args.tif,
                current_mid,
                label=f"{index}/{len(prices)}",
                price_source=f"{price_source} {prefix} {index}/{len(prices)}",
            )
        entry_plan["mode"] = prefix
        plans.append(entry_plan)
        group_plans = [entry_plan]
        if has_tpsl:
            child_plans = build_tpsl_child_plans(
                args,
                exchange,
                coin,
                asset,
                is_buy,
                entry_plan["size"],
                amount_each,
                slippage,
                Decimal(str(entry_plan["reference_price"])),
            )
            for child in child_plans:
                child["label"] = f"{index}/{len(prices)} {child['label']}"
            group_plans.extend(child_plans)
        grouped_plans.append(group_plans)

    if args.explain:
        display_plans = plans if not has_tpsl else [plan for group in grouped_plans for plan in group]
        print_explain(title, display_plans, args, price_rate)
        return

    if args.dry_run:
        display_plans = plans if not has_tpsl else [plan for group in grouped_plans for plan in group]
        print_account_metrics(info, account)
        print_box("Run", [("dry_run", "1")])
        print_order_plan_table(title, display_plans, price_rate)
        return

    if not has_tpsl:
        submit_order_plans(
            exchange,
            info,
            account,
            coin,
            max_leverage,
            plans,
            "na",
            args,
            price_rate,
            title,
        )
        return

    for index, group_plans in enumerate(grouped_plans):
        submit_order_plans(
            exchange,
            info,
            account,
            coin,
            max_leverage,
            group_plans,
            "normalTpsl",
            args,
            price_rate,
            title,
            update_leverage=index == 0,
        )


def place_order(args: argparse.Namespace) -> None:
    if args.query:
        if args.grid and args.coin:
            query_grid(args)
            return
        query_account(args)
        return

    info, exchange, account, signer, role = build_clients(args.network, args.timeout, args.coin)
    coin, asset = resolve_perp_asset(info, args.coin)
    dex = coin_dex(coin)
    price_rate = coin_display_rate(args.coin, coin)
    if args.verbose:
        print("network:", args.network)
        print("account:", mask(account))
        print("signer:", mask(signer))
        print("account_role_source:", role)

    if not args.side and args.cancel is None:
        kline_mode = "week" if args.week else "day" if args.day else "hour"
        order_flags_without_side = (
            args.price
            or args.market
            or args.reduce_only
            or args.book_level != 10
            or args.tif is not None
            or args.slippage != DEFAULT_SLIPPAGE
            or args.trail
            or args.stop_entry
            or args.take_entry
            or args.take_profit
            or args.stop_loss
            or args.total_amount
            or args.grid_max
            or args.grid_long
            or args.grid_short
            or args.grid_abs
            or args.grid_min
            or args.grid_modify
            or args.grid_recover
            or args.symmetric_offset
            or args.ladder_mode
            or args.scale
            or args.scale_from
            or args.scale_to
            or args.explain
        )
        if order_flags_without_side:
            raise ValueError("side is required when order options are used")
        print_market_overview(info, account, args.network, args.coin, coin, dex, asset, price_rate, kline_mode)
        return

    if args.cancel is not None:
        cancel_price = Decimal(args.price) if args.price else None
        cancel_order(
            exchange,
            info,
            account,
            args.network,
            coin,
            dex,
            args.cancel,
            args.dry_run,
            cancel_price,
            args.cancel_age_range,
            price_rate,
        )
        return

    amount = Decimal(args.amount)
    if amount <= 0:
        raise ValueError("amount must be positive")
    slippage = parse_slippage(args.slippage)
    has_tpsl = bool(args.take_profit or args.stop_loss)

    mids = info.all_mids(dex)
    current_mid = Decimal(str(mids[coin])) if mids.get(coin) is not None else None
    log_event("all_mids_sample", {"dex": dex or "default", "coin": coin, "mid": mids.get(coin)})
    max_leverage = int(asset["maxLeverage"])

    if args.symmetric:
        place_symmetric_orders(
            args,
            exchange,
            info,
            account,
            coin,
            asset,
            amount,
            current_mid,
            slippage,
            max_leverage,
            price_rate,
        )
        return

    if args.grid:
        if args.grid_recover:
            recover_grid_batch_order(
                args,
                info,
                account,
                coin,
                dex,
                asset,
                current_mid,
                slippage,
                price_rate,
            )
            return
        if args.grid_modify:
            modify_grid_batch_order(
                args,
                exchange,
                info,
                account,
                coin,
                dex,
                asset,
                max_leverage,
                current_mid,
                slippage,
                price_rate,
            )
            return
        plans = build_grid_orders(args, info, account, dex, exchange, coin, asset, amount, current_mid, slippage)
        if args.verbose:
            print("dex:", dex or "default")
            print("current_mid:", mids.get(coin))
            print("max_leverage:", max_leverage)
            print("grid_orders:", len(plans))
        statuses = submit_order_plans(
            exchange,
            info,
            account,
            coin,
            max_leverage,
            plans,
            "na",
            args,
            price_rate,
            "Grid Limit Orders",
        )
        if statuses is not None:
            row = build_grid_batch_row(args, account, coin, dex, asset, plans, statuses, amount, slippage)
            append_server_batch(row)
            print_grid_batch_status(row, price_rate)
        return

    is_buy = parse_side(args.side)

    if args.trail:
        run_trailing_order(
            args,
            exchange,
            info,
            account,
            coin,
            dex,
            asset,
            is_buy,
            amount,
            current_mid,
            slippage,
            price_rate,
        )
        return

    if args.ladder_mode is not None:
        place_ladder_orders(
            args,
            exchange,
            info,
            account,
            coin,
            asset,
            is_buy,
            amount,
            current_mid,
            slippage,
            max_leverage,
            price_rate,
        )
        return

    if args.scale:
        place_scale_orders(
            args,
            exchange,
            info,
            account,
            coin,
            asset,
            is_buy,
            amount,
            current_mid,
            max_leverage,
            price_rate,
        )
        return

    if args.stop_entry:
        stop_trigger_px, stop_limit_px = parse_entry_trigger_with_limit(args.stop_entry, args.stop_entry_limit, "stop")
        entry_trigger_plan = build_stop_entry_order_plan(
            coin,
            is_buy,
            amount,
            asset,
            exchange,
            slippage,
            stop_trigger_px,
            stop_limit_px,
            current_mid,
        )
        if args.take_profit or args.stop_loss:
            child_plans = build_tpsl_child_plans(
                args,
                exchange,
                coin,
                asset,
                is_buy,
                entry_trigger_plan["size"],
                amount,
                slippage,
                Decimal(str(entry_trigger_plan["reference_price"])),
            )
            plans = [entry_trigger_plan, *child_plans]
            if args.verbose:
                print("dex:", dex or "default")
                print("current_mid:", mids.get(coin))
                print("max_leverage:", max_leverage)
                print("stop_entry:", decimal_to_plain(stop_trigger_px))
                if stop_limit_px is not None:
                    print("stop_entry_limit:", decimal_to_plain(stop_limit_px))
                print("bracket_orders:", len(plans))
            submit_order_plans(
                exchange,
                info,
                account,
                coin,
                max_leverage,
                plans,
                "normalTpsl",
                args,
                price_rate,
                "Stop Entry Bracket",
            )
            return
        if args.verbose:
            print("dex:", dex or "default")
            print("current_mid:", mids.get(coin))
            print("max_leverage:", max_leverage)
            print("stop_entry:", decimal_to_plain(stop_trigger_px))
            if stop_limit_px is not None:
                print("stop_entry_limit:", decimal_to_plain(stop_limit_px))
        submit_order_plans(
            exchange,
            info,
            account,
            coin,
            max_leverage,
            [entry_trigger_plan],
            "na",
            args,
            price_rate,
            "Stop Entry",
        )
        return

    if args.take_entry:
        take_trigger_px, take_limit_px = parse_entry_trigger_with_limit(args.take_entry, args.take_entry_limit, "take")
        entry_trigger_plan = build_take_entry_order_plan(
            coin,
            is_buy,
            amount,
            asset,
            exchange,
            slippage,
            take_trigger_px,
            take_limit_px,
            current_mid,
        )
        if args.take_profit or args.stop_loss:
            child_plans = build_tpsl_child_plans(
                args,
                exchange,
                coin,
                asset,
                is_buy,
                entry_trigger_plan["size"],
                amount,
                slippage,
                Decimal(str(entry_trigger_plan["reference_price"])),
            )
            plans = [entry_trigger_plan, *child_plans]
            if args.verbose:
                print("dex:", dex or "default")
                print("current_mid:", mids.get(coin))
                print("max_leverage:", max_leverage)
                print("take_entry:", decimal_to_plain(take_trigger_px))
                if take_limit_px is not None:
                    print("take_entry_limit:", decimal_to_plain(take_limit_px))
                print("bracket_orders:", len(plans))
            submit_order_plans(
                exchange,
                info,
                account,
                coin,
                max_leverage,
                plans,
                "normalTpsl",
                args,
                price_rate,
                "Take Entry Bracket",
            )
            return
        if args.verbose:
            print("dex:", dex or "default")
            print("current_mid:", mids.get(coin))
            print("max_leverage:", max_leverage)
            print("take_entry:", decimal_to_plain(take_trigger_px))
            if take_limit_px is not None:
                print("take_entry_limit:", decimal_to_plain(take_limit_px))
        submit_order_plans(
            exchange,
            info,
            account,
            coin,
            max_leverage,
            [entry_trigger_plan],
            "na",
            args,
            price_rate,
            "Take Entry",
        )
        return

    if args.reduce_only and has_tpsl:
        place_protective_tpsl_orders(
            args,
            exchange,
            info,
            account,
            coin,
            dex,
            asset,
            is_buy,
            amount,
            slippage,
            max_leverage,
            price_rate,
        )
        return

    entry_plan = build_entry_order_plan(
        args,
        info,
        exchange,
        coin,
        asset,
        is_buy,
        amount,
        current_mid,
        slippage,
    )
    if args.take_profit or args.stop_loss:
        child_plans = build_tpsl_child_plans(
            args,
            exchange,
            coin,
            asset,
            is_buy,
            entry_plan["size"],
            amount,
            slippage,
            Decimal(str(entry_plan["reference_price"])),
        )
        plans = [entry_plan, *child_plans]
        if args.verbose:
            print("dex:", dex or "default")
            print("current_mid:", mids.get(coin))
            print("max_leverage:", max_leverage)
            print("bracket_orders:", len(plans))
        submit_order_plans(
            exchange,
            info,
            account,
            coin,
            max_leverage,
            plans,
            "normalTpsl",
            args,
            price_rate,
            "Bracket Orders",
        )
        return

    price = entry_plan["limit_px"]
    price_source = entry_plan["price_source"]
    size = entry_plan["size"]
    notional = entry_plan["notional"]
    target_notional = entry_plan["target_notional"]
    worst_notional = entry_plan["worst_notional"]
    reference_price = entry_plan["reference_price"]
    order_type = entry_plan["order_type"]
    min_value_price = entry_plan["min_value_price"]
    minimum_value_notional = entry_plan["minimum_value_notional"]
    side = side_code(is_buy)
    if args.verbose:
        print("dex:", dex or "default")
        print("price_source:", price_source)
        print("current_mid:", mids.get(coin))
        print("max_leverage:", max_leverage)
        print("market:", int(args.market))
        print("slippage:", decimal_to_plain(slippage))
        print("requested_usd:", decimal_to_plain(amount))
        print("target_usd:", decimal_to_plain(target_notional))
        print("sz_decimals:", asset["szDecimals"])
        print("order_notional:", decimal_to_plain(notional))
        if min_value_price is not None and minimum_value_notional is not None:
            print("min_value_price:", decimal_to_plain(min_value_price))
            print("min_value_notional:", decimal_to_plain(minimum_value_notional))
        print("worst_notional:", decimal_to_plain(worst_notional))
        print("tif:", order_type["limit"]["tif"])
        print("reduce_only:", args.reduce_only)

    if args.explain:
        print_explain("Order", [entry_plan], args, price_rate)
        return

    if args.dry_run:
        print_account_metrics(info, account)
        print_box("Run", [("dry_run", "1")])
        if args.market:
            print_market_order_row(
                coin,
                side,
                reference_price,
                price,
                size,
                slippage,
                notional,
                worst_notional,
                price_rate,
            )
        else:
            print_order_row(coin, side, current_mid, price, notional, price_rate)
        return

    if not args.reduce_only:
        leverage_mode, leverage_result = update_order_leverage(exchange, max_leverage, coin)
        if args.verbose:
            print("leverage_mode:", leverage_mode)
            print("update_leverage_result:", leverage_result)
        if leverage_result.get("status") != "ok":
            raise RuntimeError(f"Failed to update {leverage_mode} leverage; order was not submitted.")

    result = exchange.order(
        coin,
        is_buy,
        float(size),
        float(price),
        order_type,
        reduce_only=args.reduce_only,
    )
    if args.verbose:
        print("order_result:", result)
    log_event("order_result", result)

    clear_info_cache(info)
    print_account_metrics(info, account)
    if result.get("status") != "ok":
        print("error:", result)
        return

    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    for status in statuses:
        if "error" in status:
            print("error:", status["error"])
            continue
        if "resting" in status:
            oid = status["resting"]["oid"]
            open_orders = info.open_orders(account, dex=dex)
            log_event("open_orders_after", open_orders)
            order = next((item for item in open_orders if item.get("oid") == oid), None)
            if order:
                print_order_row(
                    order["coin"],
                    order["side"],
                    current_mid,
                    order["limitPx"],
                    order_amount(order["limitPx"], order.get("origSz", order.get("sz", "0"))),
                    price_rate,
                )
            else:
                print_order_row(coin, side, current_mid, price, notional, price_rate)
            continue
        if "filled" in status:
            print_filled_row(coin, side, status["filled"], size, price_rate)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Place or cancel a small Hyperliquid perp order.")
    parser.add_argument("coin", nargs="?", help="Perp name, e.g. BTC, ETH, SOL, or BTCUSDC. Use query/status to list account state.")
    parser.add_argument(
        "side",
        nargs="?",
        help="buy/long/看多, sell/short/看空, or both/sym/对称. Not needed with --cancel.",
    )
    parser.add_argument("amount", nargs="?", help="USD notional. Default: 10.")
    parser.add_argument("--total", dest="total_amount", help="Total USD notional. Ladder and symmetric orders divide it across legs.")
    parser.add_argument("--max", dest="grid_max", help=argparse.SUPPRESS)
    parser.add_argument(
        "--long",
        dest="grid_long",
        nargs="+",
        help="Grid long-only position value range: MAX or MIN MAX in USD.",
    )
    parser.add_argument(
        "--short",
        dest="grid_short",
        nargs="+",
        help="Grid short-only position value range: MAX or MIN MAX in USD.",
    )
    parser.add_argument("--abs", dest="grid_abs", help="Grid absolute maximum position value in USD.")
    parser.add_argument("--min", dest="grid_min", help="Grid minimum value per child order in USD. Default: exchange minimum.")
    parser.add_argument("--modify", dest="grid_modify", action="store_true", help="Modify an active server grid for this coin.")
    parser.add_argument("--recover", dest="grid_recover", action="store_true", help="Recover a server grid from current open limit orders for this coin.")
    parser.add_argument("--price", help="Limit price. Defaults to same-side book level 10.")
    parser.add_argument("--offset", dest="symmetric_offset", help="Symmetric order distance from base price, e.g. 2%% or 1500.")
    parser.add_argument("--trend", help="Grid quantity tilt. Default: 0. Positive makes buy size larger, negative makes sell size larger, e.g. 10%% or -10%%.")
    parser.add_argument("--avg", dest="grid_avg", help="Grid target position value. Dynamically tilts far-side topup size or gap up to 1.62x; mutually exclusive with --trend.")
    parser.add_argument(
        "--gap",
        nargs="+",
        help="Grid spacing. Default: min price tick percent + effective taker fee + effective maker fee. E.g. 0.15%%.",
    )
    parser.add_argument("--market", action="store_true", help="Submit as a market order using IOC with slippage protection.")
    parser.add_argument("--slippage", default=DEFAULT_SLIPPAGE, help="Market slippage protection. Default: 0.05. Also accepts 5%%.")
    parser.add_argument("--trail", help="Create a server-managed trailing stop distance, e.g. 2%% or 800.")
    parser.add_argument("--level", "--book-level", dest="book_level", type=int, default=10, help="Same-side book level when --price is omitted.")
    parser.add_argument("--tif", choices=["Gtc", "Ioc", "Alo"], help="Time in force. Limit orders default to Alo.")
    parser.add_argument(
        "--reduce-only",
        action="store_true",
        help="Place a reduce-only order. With --tp/--sl, this becomes a protective position TP/SL order.",
    )
    parser.add_argument(
        "--stop-entry",
        "--stop",
        dest="stop_entry",
        help="Stop entry trigger price. Use PRICE, PRICE+OFFSET, PRICE-OFFSET, PRICE+PERCENT, or PRICE-PERCENT. Can also be combined with --tp/--sl.",
    )
    parser.add_argument(
        "--stop-limit",
        dest="stop_entry_limit",
        help="Explicit limit price after --stop. Optional when you use PRICE+OFFSET inline syntax.",
    )
    parser.add_argument(
        "--take-entry",
        "--take",
        dest="take_entry",
        help="Take entry trigger price. Use PRICE, PRICE+OFFSET, PRICE-OFFSET, PRICE+PERCENT, or PRICE-PERCENT. Can also be combined with --tp/--sl.",
    )
    parser.add_argument(
        "--take-limit",
        dest="take_entry_limit",
        help="Explicit limit price after --take. Optional when you use PRICE+OFFSET inline syntax.",
    )
    parser.add_argument(
        "--tp",
        "--take-profit",
        dest="take_profit",
        help="Take-profit trigger price. Use ABS, ABS+OFFSET, or REL%%[+/-OFFSET] from the entry/position price. Unsigned REL%% auto-follows side. Append dRATIO to close only part of the order.",
    )
    parser.add_argument(
        "--sl",
        "--stop-loss",
        dest="stop_loss",
        help="Stop-loss trigger price. Use ABS, ABS+OFFSET, or REL%%[+/-OFFSET] from the entry/position price. Unsigned REL%% auto-follows side. Append dRATIO to close only part of the order.",
    )
    parser.add_argument(
        "--tp-limit",
        dest="take_profit_limit",
        help="Explicit limit price for --tp. Inline +/- syntax can be used instead.",
    )
    parser.add_argument(
        "--sl-limit",
        dest="stop_loss_limit",
        help="Explicit limit price for --sl. Inline +/- syntax can be used instead.",
    )
    parser.add_argument("--scale", type=int, help="Split total USD notional into this many limit orders.")
    parser.add_argument("--from", dest="scale_from", help="First scale order price.")
    parser.add_argument("--to", dest="scale_to", help="Last scale order price.")
    parser.add_argument(
        "--for",
        dest="ladder_for",
        nargs="+",
        metavar=("COUNT", "STEP"),
        help="Place COUNT ladder orders. Use --for COUNT STEP, or combine --while END --for COUNT to auto-calculate the step.",
    )
    parser.add_argument(
        "--while",
        dest="ladder_while",
        nargs="+",
        metavar=("END", "STEP"),
        help="Place ladder orders until END. Use --while END STEP, or combine --while END --for COUNT to auto-calculate the step.",
    )
    parser.add_argument(
        "--range",
        dest="range_spec",
        nargs="+",
        metavar="VALUE",
        help="Price-anchored ladder shorthand. Use --range START END STEP, or combine --range START END --for COUNT.",
    )
    kline_group = parser.add_mutually_exclusive_group()
    kline_group.add_argument("--day", action="store_true", help="Show the last 30 daily candles in market overview mode.")
    kline_group.add_argument("--week", action="store_true", help="Show the last 52 weekly candles in market overview mode.")
    parser.add_argument(
        "--cancel",
        nargs="?",
        const="all",
        help="Cancel instead of placing an order. Omit value to cancel all, pass OID, or use up/down/buy/sell/tp/sl/trail/grid/hour/day/week. Add --price with up/down, or --range with hour/day/week.",
    )
    parser.add_argument("--network", choices=["mainnet", "testnet"], default="mainnet", help="Default: mainnet.")
    parser.add_argument("--timeout", type=float, default=20, help="HTTP timeout in seconds. Default: 20.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; do not send cancel/order requests.")
    parser.add_argument("--explain", action="store_true", help="Explain parsed order plans without submitting.")
    parser.add_argument("--query", "--status", action="store_true", help="Query all current positions and open orders.")
    parser.add_argument("--verbose", action="store_true", help="Print diagnostic details.")
    parser.add_argument("--submit", action="store_true", help=argparse.SUPPRESS)
    raw_argv = sys.argv[1:]
    has_cancel_age_filter = any(
        token.strip().lower() in CANCEL_AGE_FILTERS
        and index > 0
        and raw_argv[index - 1].strip().lower() == "--cancel"
        for index, token in enumerate(raw_argv)
    ) or any(
        token.strip().lower().startswith("--cancel=")
        and token.split("=", 1)[1].strip().lower() in CANCEL_AGE_FILTERS
        for token in raw_argv
    )
    if any(token.strip().lower() in GRID_SIDE_ALIASES for token in raw_argv):
        cli_argv = normalize_signed_option_values(protect_grid_range_values(raw_argv))
    elif has_cancel_age_filter:
        cli_argv = normalize_signed_option_values(raw_argv)
    else:
        cli_argv = normalize_signed_option_values(protect_ladder_step_values(raw_argv))
    args = parser.parse_intermixed_args(cli_argv)
    query_words = {"query", "status", "positions", "orders", "持仓", "订单", "查询"}
    if args.coin and args.side is None and args.coin.strip().lower() in query_words:
        args.query = True
        args.coin = ""
    if args.cancel is not None:
        cancel_arg = args.cancel.strip().lower()
        if cancel_arg in CANCEL_FILTERS:
            args.cancel = cancel_arg
        else:
            try:
                int(args.cancel)
            except ValueError:
                parser.error("--cancel value must be an OID or one of: up, down, buy, sell, tp, sl, trail, grid, hour, day, week")
        if args.price:
            if args.cancel not in {"up", "down"}:
                parser.error("--price can only be used with --cancel up or --cancel down")
            try:
                cancel_price = Decimal(args.price)
            except InvalidOperation:
                parser.error("--price must be a valid decimal")
            if not cancel_price.is_finite() or cancel_price <= 0:
                parser.error("--price must be positive")
    args.cancel_age_range = None
    if args.cancel is not None and args.range_spec:
        if args.cancel not in CANCEL_AGE_FILTERS:
            parser.error("--range can only be used with --cancel hour, --cancel day, or --cancel week")
        try:
            args.cancel_age_range = parse_cancel_age_range(args.range_spec)
        except ValueError as exc:
            parser.error(str(exc))
        args.range_spec = [decimal_to_plain(args.cancel_age_range[0])]
        if args.cancel_age_range[1] is not None:
            args.range_spec.append(decimal_to_plain(args.cancel_age_range[1]))
    args.ladder_mode = None
    args.ladder_count = None
    args.ladder_end = None
    args.ladder_step = None
    args.symmetric = False
    args.grid = False
    args.amount_is_total = False
    if args.side is not None and args.side.strip().lower() in GRID_SIDE_ALIASES:
        args.side = "grid"
        args.grid = True
    combined_count_to_end = bool(args.ladder_for) and bool(args.ladder_while) and not args.range_spec
    combined_range_count_to_end = bool(args.ladder_for) and bool(args.range_spec) and not args.ladder_while and args.cancel is None
    if args.grid and (args.ladder_for or args.ladder_while):
        parser.error("grid orders cannot be combined with --for/--while")
    if args.ladder_for and args.ladder_while and args.range_spec:
        parser.error("--for, --while, and --range cannot all be combined")
    if combined_count_to_end and (len(args.ladder_for) != 1 or len(args.ladder_while) != 1):
        parser.error("combine --while END --for COUNT only when both options have one value")
    if combined_range_count_to_end and (len(args.ladder_for) != 1 or len(args.range_spec) != 2):
        parser.error("combine --range START END --for COUNT only when --range has START END and --for has COUNT")
    explicit_ladder_count = (
        1
        if combined_count_to_end or combined_range_count_to_end
        else bool(args.ladder_for) + bool(args.ladder_while) + (bool(args.range_spec) and not args.grid and args.cancel is None)
    )
    if explicit_ladder_count > 1:
        parser.error("--for, --while, and --range are mutually exclusive")
    if args.total_amount is not None:
        if args.amount is not None:
            parser.error("positional amount cannot be combined with --total")
        args.amount = args.total_amount
        args.amount_is_total = True
    else:
        args.amount = args.amount or "10"
    if args.side is not None:
        normalized_side = args.side.strip().lower()
        if normalized_side in GRID_SIDE_ALIASES:
            args.side = "grid"
            args.grid = True
        elif normalized_side in SYMMETRIC_SIDE_ALIASES:
            args.side = "both"
            args.symmetric = True
        else:
            try:
                is_buy = parse_side(args.side)
            except ValueError as exc:
                parser.error(str(exc))
            args.side = "buy" if is_buy else "sell"
    if combined_count_to_end:
        end_text = args.ladder_while[0]
        count_text = args.ladder_for[0]
        try:
            args.ladder_count = int(count_text)
        except ValueError:
            parser.error("--for COUNT must be an integer")
        if args.ladder_count < 2:
            parser.error("--for COUNT must be >= 2")
        try:
            args.ladder_end = Decimal(end_text)
        except InvalidOperation:
            parser.error("--while END must be a positive number")
        if args.ladder_end <= 0:
            parser.error("--while END must be positive")
        args.ladder_mode = "count_to_end"
    elif combined_range_count_to_end:
        start_text, end_text = [unprotect_grid_range_value(value) for value in args.range_spec]
        count_text = args.ladder_for[0]
        if args.price:
            parser.error("--range cannot be combined with --price")
        try:
            args.ladder_count = int(count_text)
        except ValueError:
            parser.error("--for COUNT must be an integer")
        if args.ladder_count < 2:
            parser.error("--for COUNT must be >= 2")
        try:
            start_px = Decimal(start_text)
            args.ladder_end = Decimal(end_text)
        except InvalidOperation:
            parser.error("--range START and END must be positive numbers")
        if start_px <= 0 or args.ladder_end <= 0:
            parser.error("--range START and END must be positive")
        args.price = decimal_to_plain(start_px)
        args.ladder_mode = "count_to_end"
        args.range_spec = [decimal_to_plain(start_px), decimal_to_plain(args.ladder_end), f"for {args.ladder_count}"]
    elif args.ladder_for:
        if len(args.ladder_for) != 2:
            parser.error("--for requires COUNT STEP unless combined as --while END --for COUNT or --range START END --for COUNT")
        count_text, step_text = args.ladder_for
        try:
            args.ladder_count = int(count_text)
        except ValueError:
            parser.error("--for COUNT must be an integer")
        if args.ladder_count < 2:
            parser.error("--for COUNT must be >= 2")
        args.ladder_mode = "for"
        args.ladder_step = unprotect_ladder_step_value(step_text)
    elif args.ladder_while:
        if len(args.ladder_while) != 2:
            parser.error("--while requires END STEP unless combined as --while END --for COUNT")
        end_text, step_text = args.ladder_while
        try:
            args.ladder_end = Decimal(end_text)
        except InvalidOperation:
            parser.error("--while END must be a positive number")
        if args.ladder_end <= 0:
            parser.error("--while END must be positive")
        args.ladder_mode = "while"
        args.ladder_step = unprotect_ladder_step_value(step_text)
    if args.range_spec and args.grid:
        parser.error("grid no longer uses --range; use --gap with one of --long, --short, or --abs")
    elif args.range_spec and not combined_range_count_to_end and args.cancel is None:
        if len(args.range_spec) != 3:
            parser.error("--range requires START END STEP unless combined as --range START END --for COUNT")
        start_text, end_text, step_text = args.range_spec
        if args.price:
            parser.error("--range cannot be combined with --price")
        try:
            start_px = Decimal(start_text)
            args.ladder_end = Decimal(end_text)
        except InvalidOperation:
            parser.error("--range START and END must be positive numbers")
        if start_px <= 0 or args.ladder_end <= 0:
            parser.error("--range START and END must be positive")
        args.price = decimal_to_plain(start_px)
        args.ladder_mode = "while"
        args.ladder_step = unprotect_ladder_step_value(step_text)
        args.range_spec = [decimal_to_plain(start_px), decimal_to_plain(args.ladder_end), args.ladder_step]
    if args.symmetric_offset and not args.symmetric:
        parser.error("--offset requires side both/sym/对称")
    if (args.trend or args.grid_avg is not None or args.gap or args.grid_min) and not args.grid:
        parser.error("--trend, --avg, --gap, and --min require side grid")
    if args.grid:
        if args.trend is not None and args.grid_avg is not None:
            parser.error("--avg and --trend are mutually exclusive")
        if args.grid_modify and args.grid_recover:
            parser.error("grid --modify and --recover are mutually exclusive")
        if args.total_amount is not None:
            parser.error("grid no longer uses --total; use one of --long, --short, or --abs")
        if args.amount and args.amount != "10":
            parser.error("grid no longer uses positional amount; use one of --long, --short, or --abs")
        if args.grid_max is not None:
            parser.error("grid no longer uses --max; use one of --long, --short, or --abs")
        selected_grid_limits = [
            value for value in (args.grid_long, args.grid_short, args.grid_abs) if value is not None
        ]
        if len(selected_grid_limits) > 1:
            parser.error("grid accepts only one of --long, --short, or --abs")
        if not args.query and not args.grid_modify and not selected_grid_limits:
            parser.error("grid orders require one of --long, --short, or --abs")
        if args.grid_recover and not selected_grid_limits:
            parser.error("grid --recover requires one of --long, --short, or --abs")
        try:
            if args.gap:
                parse_grid_gap(args.gap)
            parse_percent_decimal(args.trend or "0", "--trend", allow_signed=True)
            policy, min_limit_value, limit_value = grid_limit_arg(args)
            args.grid_position_limit_mode = policy
            args.grid_position_min_value = min_limit_value
            args.grid_position_limit_value = limit_value
            if limit_value is not None:
                grid_min_limit = Decimal(str(min_limit_value or "0"))
                grid_limit = Decimal(limit_value)
                if not grid_min_limit.is_finite() or grid_min_limit < 0:
                    parser.error(f"--{policy} minimum must be zero or positive")
                if not grid_limit.is_finite() or grid_limit <= 0:
                    parser.error(f"--{policy} must be positive")
                if grid_min_limit >= grid_limit:
                    parser.error(f"--{policy} requires MIN < MAX")
                if args.grid_avg is not None:
                    avg_value = Decimal(str(args.grid_avg))
                    if not avg_value.is_finite():
                        parser.error("--avg must be a finite number")
                    avg_lower, avg_upper = grid_avg_bounds(policy, grid_min_limit, grid_limit)
                    if avg_value < avg_lower or avg_value > avg_upper:
                        parser.error(
                            f"--avg must be between {decimal_to_plain(avg_lower)} and {decimal_to_plain(avg_upper)} for --{policy}"
                        )
            if args.grid_min is not None:
                grid_min_notional(args)
        except ValueError as exc:
            parser.error(str(exc))
        except InvalidOperation:
            parser.error("grid limit, --avg, and --min must be valid numbers")
        args.amount = args.grid_position_limit_value or "10"
    if args.symmetric:
        if not args.symmetric_offset:
            parser.error("symmetric orders require --offset")
        try:
            resolve_symmetric_offset(Decimal("100"), args.symmetric_offset)
        except ValueError as exc:
            parser.error(str(exc))
    if not args.query and not args.coin:
        parser.error("coin is required unless query/status or --query is used")
    if args.trail:
        try:
            parse_trail_distance(args.trail, Decimal("100"))
        except ValueError as exc:
            parser.error(str(exc))
        if args.price:
            parser.error("--trail cannot be combined with --price; it uses the current mid price")
    has_tpsl = bool(args.take_profit or args.stop_loss)
    if args.market and args.price and not args.trail:
        parser.error("--market cannot be used with --price")
    if args.market and args.cancel is not None:
        parser.error("--market cannot be used with --cancel")
    if args.explain and args.cancel is not None:
        parser.error("--explain cannot be used with --cancel")
    if args.total_amount is not None and args.cancel is not None:
        parser.error("--total cannot be used with --cancel")
    if args.ladder_mode is not None and args.market:
        parser.error("Ladder orders cannot use --market")
    if args.trail:
        if args.cancel is not None:
            parser.error("--trail cannot be combined with --cancel")
        if args.symmetric or args.grid or args.ladder_mode is not None or args.scale is not None:
            parser.error("--trail cannot be combined with symmetric, grid, ladder, or scale orders")
        if args.stop_entry or args.take_entry or has_tpsl:
            parser.error("--trail cannot be combined with --stop/--take or --tp/--sl")
        if args.book_level != 10 or args.tif is not None:
            parser.error("--trail cannot be combined with --level or --tif")
    if args.stop_entry:
        try:
            parse_entry_trigger_with_limit(args.stop_entry, args.stop_entry_limit, "stop")
        except ValueError as exc:
            parser.error(str(exc))
    elif args.stop_entry_limit:
        parser.error("--stop-limit requires --stop/--stop-entry")
    if args.take_entry:
        try:
            parse_entry_trigger_with_limit(args.take_entry, args.take_entry_limit, "take")
        except ValueError as exc:
            parser.error(str(exc))
    elif args.take_entry_limit:
        parser.error("--take-limit requires --take/--take-entry")
    if args.stop_entry and args.reduce_only and args.ladder_mode is None:
        parser.error("--stop-entry cannot be used with --reduce-only")
    if args.take_entry and args.reduce_only and args.ladder_mode is None:
        parser.error("--take-entry cannot be used with --reduce-only")
    if args.stop_entry and args.ladder_mode is None and (args.price or args.market or args.book_level != 10 or args.tif is not None):
        parser.error("--stop-entry cannot be combined with --price, --market, --level, or --tif")
    if args.take_entry and args.ladder_mode is None and (args.price or args.market or args.book_level != 10 or args.tif is not None):
        parser.error("--take-entry cannot be combined with --price, --market, --level, or --tif")
    if (args.take_profit_limit and not args.take_profit) or (args.stop_loss_limit and not args.stop_loss):
        parser.error("--tp-limit requires --tp, and --sl-limit requires --sl")
    if args.stop_entry and args.take_entry:
        parser.error("--stop-entry and --take-entry are mutually exclusive")
    if args.symmetric:
        if args.cancel is not None:
            parser.error("symmetric orders cannot be combined with --cancel")
        if args.market:
            parser.error("symmetric orders cannot use --market")
        if args.book_level != 10:
            parser.error("symmetric orders cannot use --level; use --price to set the center price")
        if args.reduce_only:
            parser.error("symmetric orders cannot use --reduce-only")
        if args.stop_entry or args.take_entry:
            parser.error("symmetric orders cannot be combined with --stop/--take")
        if args.ladder_mode is not None:
            parser.error("symmetric orders cannot be combined with --for/--while/--range")
        if args.scale is not None or args.scale_from or args.scale_to:
            parser.error("symmetric orders cannot be combined with --scale")
    if args.ladder_mode is not None and args.scale is not None:
        parser.error("Ladder orders cannot be combined with --scale")
    if args.range_spec and not args.grid and (args.stop_entry or args.take_entry):
        parser.error("--range cannot be combined with --stop/--take; use --stop/--take with --while instead")
    if args.ladder_mode is not None and (args.stop_entry or args.take_entry) and has_tpsl:
        parser.error("Ladder trigger orders cannot combine --stop/--take with --tp/--sl")
    if args.ladder_mode is not None and has_tpsl and args.reduce_only:
        parser.error("Ladder orders cannot combine --reduce-only with --tp/--sl")
    if args.scale is not None:
        if args.scale < 2:
            parser.error("--scale must be >= 2")
        if not args.scale_from or not args.scale_to:
            parser.error("--scale requires --from and --to")
        if args.price or args.market or args.book_level != 10:
            parser.error("--scale cannot be combined with --price, --market, or --level")
        if has_tpsl or args.stop_entry or args.take_entry:
            parser.error("--scale cannot be combined with --tp/--sl, --stop-entry, or --take-entry")
    elif args.scale_from or args.scale_to:
        parser.error("--from/--to require --scale")
    if args.grid:
        if args.cancel is not None:
            parser.error("grid orders cannot be combined with --cancel")
        if args.market or args.price or args.book_level != 10 or args.tif is not None:
            parser.error("grid orders cannot be combined with --market, --price, --level, or --tif")
        if args.reduce_only:
            parser.error("grid orders cannot use --reduce-only")
        if args.stop_entry or args.take_entry or has_tpsl:
            parser.error("grid orders cannot be combined with --stop/--take or --tp/--sl")
        if args.symmetric or args.symmetric_offset:
            parser.error("grid orders cannot be combined with symmetric order options")
        if args.ladder_mode is not None or args.scale is not None or args.scale_from or args.scale_to:
            parser.error("grid orders cannot be combined with ladder or scale options")
    if has_tpsl and args.cancel is not None:
        parser.error("--tp/--sl cannot be combined with --cancel")
    if args.stop_entry and args.cancel is not None:
        parser.error("--stop-entry cannot be combined with --cancel")
    if args.take_entry and args.cancel is not None:
        parser.error("--take-entry cannot be combined with --cancel")
    return args


def main() -> None:
    global LOGGER
    LOGGER = RunLogger(sys.argv)
    LOGGER.init()
    args = parse_args()
    log_event("args", vars(args))
    try:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            if args_need_server_batch_lock(args):
                with server_batch_lock():
                    place_order(args)
            else:
                place_order(args)
        output = buffer.getvalue()
        log_event("stdout", output)
        print(output, end="")
    except Exception as exc:
        log_event("exception", {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()})
        print("error:", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
