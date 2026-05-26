#!/usr/bin/env python3
"""Small Hyperliquid perp order helper.

Examples:
  ./hl_order.py query
  ./hl_order.py BTC
  ./hl_order.py BTC buy
  ./hl_order.py BTC buy 10 --market
  ./hl_order.py BTC buy --price 75000
  ./hl_order.py BTC sell 25 --price 80000
  ./hl_order.py BTC sell 25 --stop 70000
  ./hl_order.py BTC sell 25 --stop 70000+50
  ./hl_order.py BTC buy 25 --take 70000
  ./hl_order.py BTC buy 25 --take 70000+0.2%
  ./hl_order.py BTC buy --tp 2%+0.1% --sl -2%-0.1%
  ./hl_order.py BTC --cancel
  ./hl_order.py BTC --cancel 123456789
  ./hl_order.py BTC buy --dry-run
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
import time
import traceback
import unicodedata
from threading import Lock
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from pathlib import Path
from typing import Any, Optional


def ensure_local_venv() -> None:
    project_dir = Path(__file__).resolve().parent
    venv_dir = project_dir / ".venv"
    candidates = [
        venv_dir / "bin" / "python",
        venv_dir / "Scripts" / "python.exe",
    ]
    venv_python = next((path for path in candidates if path.exists()), None)
    if venv_python is None:
        return
    if Path(sys.prefix).resolve() == venv_dir.resolve():
        return
    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])


ensure_local_venv()

import eth_account

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from coin_aliases import coin_alias_key, load_coin_aliases, load_coin_alias_rates


MIN_NOTIONAL = Decimal("10")
ISOLATED_FALLBACK_LEVERAGE = 5
KLINE_CHART_HEIGHT = 13
KLINE_MODES = {
    "hour": {
        "interval": "1h",
        "candles": 24,
        "lookback_days": 2,
        "market_title": "Market 24h",
        "title": "Kline 24h",
    },
    "day": {
        "interval": "1d",
        "candles": 30,
        "lookback_days": 45,
        "market_title": "Market 30d",
        "title": "Kline 30d",
    },
    "week": {
        "interval": "1w",
        "candles": 52,
        "lookback_days": 400,
        "market_title": "Market 52w",
        "title": "Kline 52w",
    },
}
COIN_ALIASES = load_coin_aliases()
COIN_ALIAS_RATES = load_coin_alias_rates()


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
        value = loader()
        with self._lock:
            self._cache[key] = value
        return value

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

    def user_fills(self, account: str) -> Any:
        return self._cached(("user_fills", account), lambda: self._info.user_fills(account))

    def user_fills_by_time(
        self, account: str, start_time: int, end_time: Optional[int] = None, aggregate_by_time: Optional[bool] = False
    ) -> Any:
        return self._cached(
            ("user_fills_by_time", account, start_time, end_time, aggregate_by_time),
            lambda: self._info.user_fills_by_time(account, start_time, end_time, aggregate_by_time),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._info, name)


def clear_info_cache(info: Any) -> None:
    clear = getattr(info, "clear_cache", None)
    if callable(clear):
        clear()


def load_dotenv(path: str = ".env") -> dict[str, str]:
    env_path = Path(path)
    values: dict[str, str] = {}
    if env_path.exists():
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")

    # Also accept uppercase env-style names if the file ever changes.
    normalized = {key.lower(): value for key, value in values.items()}
    for key in ("account_address", "secret_key"):
        for env_key in (key, key.upper()):
            env_value = os.environ.get(env_key)
            if env_value:
                normalized[key] = env_value
                break
    if not normalized.get("account_address") or not normalized.get("secret_key"):
        raise FileNotFoundError(f"Missing credentials: set {env_path} or account_address/secret_key environment variables")
    return normalized


def mask(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}" if address else ""


def decimal_to_plain(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def decimal_to_display(value: Decimal | str) -> str:
    rounded = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rounded:,.2f}"


def format_price(value: Decimal | str, rate: Decimal | None = None) -> str:
    price = Decimal(str(value))
    text = decimal_to_display(price)
    if rate is None:
        return text
    return f"{text} ({decimal_to_display(price / rate)})"


def format_optional_price(value: Any, rate: Decimal | None = None) -> str:
    decimal = decimal_or_none(value)
    if decimal is None:
        return "n/a"
    return format_price(decimal, rate)


def format_percent(value: Optional[Decimal]) -> str:
    if value is None:
        return "n/a"
    return f"{decimal_to_display(value * Decimal('100'))}%"


def format_signed_percent(value: Decimal) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{decimal_to_display(value * Decimal('100'))}%"


def format_signed_decimal(value: Decimal) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{decimal_to_display(value)}"


def format_leverage(value: Optional[Decimal]) -> str:
    if value is None:
        return "n/a"
    return f"{decimal_to_display(value)}x"


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def format_optional_decimal(value: Any) -> str:
    decimal = decimal_or_none(value)
    if decimal is None:
        return "n/a"
    return decimal_to_display(decimal)


def format_optional_quantity(value: Any) -> str:
    decimal = decimal_or_none(value)
    if decimal is None:
        return "n/a"
    return decimal_to_plain(decimal)


def order_amount(limit_px: Any, size: Any) -> Decimal:
    return Decimal(str(limit_px)) * Decimal(str(size))


def format_optional_percent(value: Any) -> str:
    decimal = decimal_or_none(value)
    if decimal is None:
        return "n/a"
    return format_percent(decimal)


def format_timestamp_ms(value: Any) -> str:
    if value in (None, ""):
        return "n/a"
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def format_short_timestamp_ms(value: Any) -> str:
    if value in (None, ""):
        return "n/a"
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")


def visible_width(text: str) -> int:
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def pad_visible(text: str, width: int) -> str:
    return text + " " * max(width - visible_width(text), 0)


def print_box(title: str, rows: list[tuple[str, str]]) -> None:
    content_width = max([visible_width(title), *(visible_width(f"{key}: {value}") for key, value in rows)], default=0)
    width = max(content_width + 2, 26)
    print(f"+- {title} " + "-" * max(width - visible_width(title) - 3, 0) + "+")
    for key, value in rows:
        text = f"{key}: {value}"
        print(f"| {pad_visible(text, width)} |")
    print("+" + "-" * (width + 2) + "+")


def print_section_title(title: str) -> None:
    width = max(visible_width(title) + 2, 26)
    print(f"+- {title} " + "-" * max(width - visible_width(title) - 3, 0) + "+")


def print_table(
    title: str,
    rows: list[dict[str, str]],
    columns: list[tuple[str, str]],
    show_count: bool = True,
) -> None:
    if show_count:
        print_box(title, [("count", str(len(rows)))])
    else:
        print_section_title(title)
    if not rows:
        return

    widths = []
    for key, label in columns:
        width = visible_width(label)
        for row in rows:
            width = max(width, visible_width(row.get(key, "")))
        widths.append(width)

    separator = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    header = "|" + "|".join(f" {pad_visible(label, width)} " for (_, label), width in zip(columns, widths)) + "|"
    print(separator)
    print(header)
    print(separator)
    for row in rows:
        print("|" + "|".join(f" {pad_visible(row.get(key, ''), width)} " for (key, _), width in zip(columns, widths)) + "|")
    print(separator)


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


def print_text_box(title: str, lines: list[str], bottom_border_overlay: str | None = None) -> None:
    width = max([visible_width(title), *(visible_width(line) for line in lines)], default=0)
    width = max(width, 26)
    print(f"+- {title} " + "-" * max(width - visible_width(title) - 3, 0) + "+")
    for line in lines:
        print(f"| {pad_visible(line, width)} |")
    bottom_border = ["+"] + ["-"] * (width + 2) + ["+"]
    if bottom_border_overlay:
        for index, char in enumerate(bottom_border_overlay[: width + 2]):
            if char != " ":
                bottom_border[1 + index] = char
    print("".join(bottom_border))


def price_to_chart_row(price: Decimal, high: Decimal, low: Decimal, height: int) -> int:
    if high == low:
        return height // 2
    relative = (high - price) / (high - low)
    row = int((relative * Decimal(height - 1)).to_integral_value(rounding=ROUND_HALF_UP))
    return max(0, min(height - 1, row))


def candle_start_utc(candle: dict[str, Any]) -> datetime:
    return datetime.fromtimestamp(int(candle["t"]) / 1000, tz=timezone.utc)


def candle_end_utc(candle: dict[str, Any]) -> datetime:
    return datetime.fromtimestamp(int(candle["T"]) / 1000, tz=timezone.utc)


def kline_marker_for_candle(candle: dict[str, Any], mode: str) -> str:
    start = candle_start_utc(candle)
    if mode == "hour":
        if start.hour == 0:
            return "0"
        if start.hour == 12:
            return "+"
        return " "
    if mode == "day":
        if start.day in {1, 10}:
            return "1"
        if start.day == 20:
            return "2"
        if start.day == 30:
            return "3"
        return " "
    if mode == "week":
        month_start = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
        if month_start < start:
            if start.month == 12:
                month_start = datetime(start.year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                month_start = datetime(start.year, start.month + 1, 1, tzinfo=timezone.utc)
        return str(month_start.month % 10) if month_start <= candle_end_utc(candle) else " "
    return " "


def render_kline_chart(
    candles: list[dict[str, Any]],
    latest_price: Decimal | None = None,
    mode: str = "hour",
) -> tuple[list[str], str | None]:
    mode_config = KLINE_MODES.get(mode, KLINE_MODES["hour"])
    chart_candles = [dict(candle) for candle in candles[-mode_config["candles"] :]]
    if not chart_candles:
        return ["no candle data"], None

    if latest_price is not None:
        last = chart_candles[-1]
        last["c"] = str(latest_price)
        last["h"] = str(max(Decimal(str(last["h"])), latest_price))
        last["l"] = str(min(Decimal(str(last["l"])), latest_price))

    highs = [Decimal(str(candle["h"])) for candle in chart_candles]
    lows = [Decimal(str(candle["l"])) for candle in chart_candles]
    high = max(highs)
    low = min(lows)
    labels = [""] * KLINE_CHART_HEIGHT
    labels[0] = decimal_to_display(high)
    labels[-1] = decimal_to_display(low)
    label_width = max(visible_width(label) for label in labels)

    rows = []
    for row in range(KLINE_CHART_HEIGHT):
        marks = []
        for candle in chart_candles:
            open_price = Decimal(str(candle["o"]))
            close_price = Decimal(str(candle["c"]))
            high_price = Decimal(str(candle["h"]))
            low_price = Decimal(str(candle["l"]))
            high_row = price_to_chart_row(high_price, high, low, KLINE_CHART_HEIGHT)
            low_row = price_to_chart_row(low_price, high, low, KLINE_CHART_HEIGHT)
            open_row = price_to_chart_row(open_price, high, low, KLINE_CHART_HEIGHT)
            close_row = price_to_chart_row(close_price, high, low, KLINE_CHART_HEIGHT)
            body_top = min(open_row, close_row)
            body_bottom = max(open_row, close_row)
            wick_top = min(high_row, low_row)
            wick_bottom = max(high_row, low_row)
            if body_top <= row <= body_bottom:
                mark = "□" if close_price > open_price else "■" if close_price < open_price else "─"
            elif wick_top <= row <= wick_bottom:
                mark = "│"
            else:
                mark = " "
            marks.append(mark)
        rows.append(f"{pad_visible(labels[row], label_width)} │ {''.join(marks)}")

    content_width = max(visible_width(line) for line in rows)
    bottom_border_overlay = [" "] * (content_width + 2)
    for index, candle in enumerate(chart_candles):
        marker = kline_marker_for_candle(candle, mode)
        if marker != " ":
            bottom_border_overlay[label_width + 4 + index] = marker
    return rows, "".join(bottom_border_overlay)


def parse_side(side: str) -> bool:
    normalized = side.strip().lower()
    long_aliases = {"b", "buy", "long", "up", "多", "买", "看多", "做多"}
    short_aliases = {"s", "sell", "short", "down", "空", "卖", "看空", "做空"}
    if normalized in long_aliases:
        return True
    if normalized in short_aliases:
        return False
    raise ValueError(f"Unknown side: {side}. Use buy/long/看多 or sell/short/看空.")


def normalize_coin_input(raw_coin: str) -> list[str]:
    coin = canonical_coin_input(raw_coin)
    upper = coin.upper().replace("-", "").replace("/", "")
    candidates = [coin, coin.upper(), upper]
    if upper.endswith("USDC"):
        candidates.append(upper[: -len("USDC")])
    if upper.endswith("USD"):
        candidates.append(upper[: -len("USD")])
    if upper.endswith("PERP"):
        candidates.append(upper[: -len("PERP")])
    return candidates


def canonical_coin_input(raw_coin: str) -> str:
    coin = raw_coin.strip()
    alias = COIN_ALIASES.get(coin_alias_key(coin))
    if alias:
        return alias
    if ":" in coin:
        dex, name = coin.split(":", 1)
        return f"{dex.lower()}:{name.upper()}"
    return coin


def coin_dex(raw_coin: str) -> str:
    coin = canonical_coin_input(raw_coin)
    return coin.split(":", 1)[0] if ":" in coin else ""


def coin_display_rate(raw_coin: str, resolved_coin: str) -> Decimal | None:
    for coin in (raw_coin, canonical_coin_input(raw_coin), resolved_coin):
        rate = COIN_ALIAS_RATES.get(coin_alias_key(coin))
        if rate is not None:
            return rate
    return None


def resolve_perp_asset(info: Info, raw_coin: str) -> tuple[str, dict[str, Any]]:
    dex = coin_dex(raw_coin)
    canonical = canonical_coin_input(raw_coin)
    meta = info.meta(dex=dex)
    by_upper = {asset["name"].upper(): asset for asset in meta["universe"]}

    for candidate in normalize_coin_input(canonical):
        asset = by_upper.get(candidate.upper())
        if asset:
            return asset["name"], asset

    available = ", ".join(asset["name"] for asset in meta["universe"][:20])
    raise ValueError(f"Unknown perp coin: {raw_coin}. First available coins: {available} ...")


def round_to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    units = (value / step).to_integral_value(rounding=rounding)
    return units * step


def calc_size(
    amount_usd: Decimal,
    price: Decimal,
    sz_decimals: int,
    min_value_price: Decimal | None = None,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    target_notional = max(amount_usd, MIN_NOTIONAL)
    step = Decimal(1).scaleb(-sz_decimals)
    sizing_price = min(price, min_value_price) if min_value_price is not None else price

    raw_size = target_notional / sizing_price
    size = round_to_step(raw_size, step, ROUND_DOWN)

    # Rounding down can make a 10 USD order become 9.75 USD. In that case,
    # move one size step in the opposite direction: round up until it is enough.
    if size <= 0 or size * sizing_price < target_notional:
        size = round_to_step(raw_size, step, ROUND_UP)

    notional = size * price
    minimum_value_notional = size * sizing_price
    if minimum_value_notional < MIN_NOTIONAL:
        raise ValueError(
            f"Calculated notional is still below {MIN_NOTIONAL}: "
            f"size={decimal_to_plain(size)}, price={decimal_to_plain(sizing_price)}, "
            f"notional={decimal_to_plain(minimum_value_notional)}"
        )
    return size, notional, target_notional, minimum_value_notional


def calc_market_size(
    amount_usd: Decimal,
    reference_price: Decimal,
    worst_price: Decimal,
    sz_decimals: int,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if reference_price <= 0 or worst_price <= 0:
        raise ValueError("Market reference price must be positive")

    target_notional = max(amount_usd, MIN_NOTIONAL)
    step = Decimal(1).scaleb(-sz_decimals)
    raw_size = target_notional / reference_price
    size = round_to_step(raw_size, step, ROUND_DOWN)
    if size <= 0 or size * reference_price < target_notional:
        size = round_to_step(raw_size, step, ROUND_UP)

    if size * worst_price < MIN_NOTIONAL:
        size = round_to_step(MIN_NOTIONAL / worst_price, step, ROUND_UP)

    reference_notional = size * reference_price
    worst_notional = size * worst_price
    if worst_notional < MIN_NOTIONAL:
        raise ValueError(
            f"Calculated market notional is still below {MIN_NOTIONAL}: "
            f"size={decimal_to_plain(size)}, worst_price={decimal_to_plain(worst_price)}, "
            f"notional={decimal_to_plain(worst_notional)}"
        )
    return size, reference_notional, target_notional, worst_notional


def side_code(is_buy: bool) -> str:
    return "B" if is_buy else "A"


def rounded_perp_price(price: Decimal, sz_decimals: int) -> Decimal:
    """Match the SDK's perp price rounding rules before submitting an order."""
    if price <= 0:
        raise ValueError("price must be positive")
    if price > Decimal("100000"):
        rounded = Decimal(str(round(float(price))))
    else:
        decimal_places = max(0, 6 - sz_decimals)
        rounded = Decimal(str(round(float(f"{float(price):.5g}"), decimal_places)))
    if rounded <= 0:
        raise ValueError("price must be positive")
    return rounded


def scale_order_size(base_size: Decimal, size_ratio: Decimal, sz_decimals: int, min_value_price: Decimal, label: str) -> Decimal:
    if size_ratio <= 0 or size_ratio > 1:
        raise ValueError(f"{label} size ratio must be greater than 0 and at most 1")
    if size_ratio == 1:
        return base_size

    step = Decimal(1).scaleb(-sz_decimals)
    size = round_to_step(base_size * size_ratio, step, ROUND_DOWN)
    if size <= 0:
        raise ValueError(f"{label} size is too small after applying the >{format_percent(size_ratio)} suffix")
    if size * min_value_price < MIN_NOTIONAL:
        raise ValueError(
            f"{label} size after applying the >{format_percent(size_ratio)} suffix must still be at least "
            f"{MIN_NOTIONAL} USD notional"
        )
    return size


def scale_prices(start: Decimal, end: Decimal, count: int, sz_decimals: int) -> list[Decimal]:
    if count < 2:
        raise ValueError("--scale must be >= 2")
    if start <= 0 or end <= 0:
        raise ValueError("--from and --to prices must be positive")
    step = (end - start) / Decimal(count - 1)
    prices = [rounded_perp_price(start + step * Decimal(index), sz_decimals) for index in range(count)]
    price_keys = {decimal_to_plain(price) for price in prices}
    if len(price_keys) != len(prices):
        raise ValueError("Scale prices collapse to duplicates after rounding; widen the range or reduce --scale")
    return prices


def parse_slippage(value: str) -> Decimal:
    text = value.strip()
    if text.endswith("%"):
        slippage = Decimal(text[:-1]) / Decimal("100")
    else:
        slippage = Decimal(text)
    if slippage < 0 or slippage >= 1:
        raise ValueError("--slippage must be >= 0 and < 1, e.g. 0.05 or 5%")
    return slippage


ENTRY_TRIGGER_SPEC_RE = re.compile(r"^\s*(?P<trigger>\d+(?:\.\d+)?)(?:(?P<op>[+-])(?P<adjust>\d+(?:\.\d+)?%?))?\s*$")


def parse_entry_trigger_spec(value: str, label: str) -> tuple[Decimal, Decimal | None]:
    text = value.strip().replace(" ", "")
    match = ENTRY_TRIGGER_SPEC_RE.fullmatch(text)
    if not match:
        raise ValueError(
            f"Invalid {label} value: {value}. Use PRICE, PRICE+OFFSET, PRICE-OFFSET, PRICE+PERCENT, or PRICE-PERCENT."
        )

    trigger_px = Decimal(match.group("trigger"))
    if trigger_px <= 0:
        raise ValueError(f"{label} trigger price must be positive")

    adjust_text = match.group("adjust")
    if adjust_text is None:
        return trigger_px, None

    sign = Decimal("1") if match.group("op") == "+" else Decimal("-1")
    if adjust_text.endswith("%"):
        adjust = trigger_px * (Decimal(adjust_text[:-1]) / Decimal("100"))
    else:
        adjust = Decimal(adjust_text)
    limit_px = trigger_px + sign * adjust
    if limit_px <= 0:
        raise ValueError(f"{label} limit price must be positive")
    return trigger_px, limit_px


def parse_entry_trigger_with_limit(value: str, explicit_limit: str | None, label: str) -> tuple[Decimal, Decimal | None]:
    trigger_px, inline_limit_px = parse_entry_trigger_spec(value, label)
    if explicit_limit is not None and inline_limit_px is not None:
        raise ValueError(f"--{label}-limit cannot be combined with inline +/- syntax in --{label}")
    if explicit_limit is None:
        return trigger_px, inline_limit_px

    limit_px = Decimal(explicit_limit)
    if limit_px <= 0:
        raise ValueError(f"--{label}-limit must be positive")
    return trigger_px, limit_px


TPSL_SPEC_RE = re.compile(
    r"^\s*(?P<trigger_sign>[+-]?)(?P<trigger>\d+(?:\.\d+)?)(?P<trigger_pct>%?)"
    r"(?:(?P<limit_sign>[+-])(?P<limit>\d+(?:\.\d+)?%?))?"
    r"(?:(?P<ratio_sep>d)(?P<ratio>\d+(?:\.\d+)?%?))?\s*$"
)


def apply_signed_offset(base_px: Decimal, sign: str, value_text: str, label: str) -> Decimal:
    if value_text.endswith("%"):
        offset = Decimal(value_text[:-1]) / Decimal("100")
        if sign == "-":
            offset = -offset
        result = base_px * (Decimal("1") + offset)
    else:
        delta = Decimal(value_text)
        result = base_px + (delta if sign == "+" else -delta)
    if result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


def parse_size_ratio(value: str, label: str) -> Decimal:
    text = value.strip()
    if text.endswith("%"):
        ratio = Decimal(text[:-1]) / Decimal("100")
    else:
        ratio = Decimal(text)
    if ratio <= 0 or ratio > 1:
        raise ValueError(f"{label} must be greater than 0 and at most 1")
    return ratio


def parse_tpsl_spec(value: str, base_px: Decimal | None, label: str) -> tuple[Decimal, Decimal | None, Decimal]:
    text = value.strip().replace(" ", "")
    match = TPSL_SPEC_RE.fullmatch(text)
    if not match:
        raise ValueError(
            f"Invalid {label} value: {value}. Use PRICE, PRICE+OFFSET, PRICE-OFFSET, PRICE+PERCENT, PRICE-PERCENT, "
            "or REL%[+/-OFFSET] when a reference price is available. Append dRATIO to close only part of the order."
        )

    trigger_sign = match.group("trigger_sign") or "+"
    trigger_text = match.group("trigger")
    if match.group("trigger_pct"):
        if base_px is None:
            raise ValueError(f"{label} relative trigger prices require a reference price")
        trigger_pct = Decimal(trigger_text) / Decimal("100")
        if trigger_sign == "-":
            trigger_pct = -trigger_pct
        trigger_px = base_px * (Decimal("1") + trigger_pct)
    else:
        trigger_px = Decimal(trigger_text)
        if trigger_sign == "-":
            trigger_px = -trigger_px

    if trigger_px <= 0:
        raise ValueError(f"{label} trigger price must be positive")

    inline_limit_px: Decimal | None = None
    limit_text = match.group("limit")
    if limit_text is not None:
        inline_limit_px = apply_signed_offset(trigger_px, match.group("limit_sign") or "+", limit_text, f"{label} limit price")
    size_ratio = Decimal("1")
    ratio_text = match.group("ratio")
    if ratio_text is not None:
        size_ratio = parse_size_ratio(ratio_text, f"{label} size ratio")
    return trigger_px, inline_limit_px, size_ratio


def resolve_tpsl_spec(
    value: str,
    explicit_limit: str | None,
    base_px: Decimal | None,
    label: str,
) -> tuple[Decimal, Decimal | None, Decimal]:
    trigger_px, inline_limit_px, size_ratio = parse_tpsl_spec(value, base_px, label)
    if explicit_limit is not None and inline_limit_px is not None:
        raise ValueError(f"--{label}-limit cannot be combined with inline +/- syntax in --{label}")
    if explicit_limit is not None:
        limit_px = Decimal(explicit_limit)
        if limit_px <= 0:
            raise ValueError(f"--{label}-limit must be positive")
        return trigger_px, limit_px, size_ratio
    return trigger_px, inline_limit_px, size_ratio


def validate_tpsl_direction(
    label: str,
    trigger_px: Decimal,
    base_px: Decimal | None,
    position_is_long: bool,
) -> None:
    if base_px is None:
        return
    if label == "tp":
        if position_is_long and trigger_px <= base_px:
            raise ValueError(
                f"Take-profit for long positions must trigger above the reference price ({decimal_to_display(base_px)})"
            )
        if not position_is_long and trigger_px >= base_px:
            raise ValueError(
                f"Take-profit for short positions must trigger below the reference price ({decimal_to_display(base_px)})"
            )
        return
    if position_is_long and trigger_px >= base_px:
        raise ValueError(
            f"Stop-loss for long positions must trigger below the reference price ({decimal_to_display(base_px)})"
        )
    if not position_is_long and trigger_px <= base_px:
        raise ValueError(
            f"Stop-loss for short positions must trigger above the reference price ({decimal_to_display(base_px)})"
        )


VALUE_OPTION_STRINGS = {
    "--price",
    "--slippage",
    "--level",
    "--book-level",
    "--tif",
    "--stop-entry",
    "--stop",
    "--stop-limit",
    "--take-entry",
    "--take",
    "--take-limit",
    "--tp",
    "--take-profit",
    "--sl",
    "--stop-loss",
    "--tp-limit",
    "--sl-limit",
    "--scale",
    "--from",
    "--to",
    "--cancel",
    "--network",
    "--timeout",
}


def normalize_signed_option_values(argv: list[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in VALUE_OPTION_STRINGS and index + 1 < len(argv):
            next_token = argv[index + 1]
            if next_token.startswith("-") and next_token not in VALUE_OPTION_STRINGS:
                normalized.append(f"{token}={next_token}")
                index += 2
                continue
        normalized.append(token)
        index += 1
    return normalized


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


def build_clients(network: str, timeout: float, raw_coin: str) -> tuple[Info, Exchange, str, str, dict[str, Any]]:
    env = load_dotenv()
    secret_key = env.get("secret_key")
    account_address = env.get("account_address")
    if not secret_key or not account_address:
        raise ValueError(".env must contain secret_key and account_address")

    wallet = eth_account.Account.from_key(secret_key)
    base_url = constants.TESTNET_API_URL if network == "testnet" else constants.MAINNET_API_URL
    dex = coin_dex(raw_coin)
    perp_dexs = ["", dex] if dex else None
    raw_info = Info(base_url, skip_ws=True, timeout=timeout, perp_dexs=perp_dexs)
    info: CachedInfo = CachedInfo(raw_info)
    main_account, role = resolve_account(info, account_address, wallet.address)
    exchange = Exchange(wallet, base_url, account_address=main_account, timeout=timeout, perp_dexs=perp_dexs)
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


def unified_account_metrics(info: Info, account: str) -> tuple[Optional[Decimal], Optional[Decimal]]:
    spot_state = info.spot_user_state(account)
    log_event("spot_state", spot_state)
    spot_totals = {
        int(balance["token"]): Decimal(str(balance.get("total", "0")))
        for balance in spot_state.get("balances", [])
    }

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
    }
    log_event("unified_metrics", metrics)
    return unified_ratio, unified_leverage


def print_account_metrics(info: Info, account: str) -> None:
    unified_ratio, unified_leverage = unified_account_metrics(info, account)
    print_box(
        "Account",
        [
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


def print_market_overview(
    info: Info,
    account: str,
    raw_coin: str,
    coin: str,
    dex: str,
    price_rate: Decimal | None,
    kline_mode: str = "hour",
) -> None:
    mode_config = KLINE_MODES.get(kline_mode, KLINE_MODES["hour"])
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - mode_config["lookback_days"] * 24 * 60 * 60 * 1000
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
    if not candles:
        raise ValueError(f"No candle data found for {raw_coin}")

    chart_candles = candles[-mode_config["candles"] :]
    open_price = Decimal(str(chart_candles[0]["o"]))
    latest_price = Decimal(str(chart_candles[-1]["c"]))
    mids = info.all_mids(dex)
    mid = mids.get(coin)
    if mid is not None:
        latest_price = Decimal(str(mid))
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

    print_box(
        mode_config["market_title"],
        [
            ("coin", coin),
            ("trend", f"{trend} {format_signed_decimal(change)} ({format_signed_percent(change_percent)})"),
            ("latest", format_price(latest_price, price_rate)),
            ("turnover", decimal_to_display(notional_volume)),
        ],
    )
    chart_lines, chart_overlay = render_kline_chart(chart_candles, latest_price, kline_mode)
    print_text_box(mode_config["title"], chart_lines, chart_overlay)

    position = find_current_position(info, account, coin, dex)
    if position is not None:
        print_box(
            "Position",
            [
                ("side", format_position_side(Decimal(str(position.get("szi", "0"))))),
                ("entryPx", format_optional_decimal(position.get("entryPx"))),
                ("nPnl", format_optional_decimal(position.get("unrealizedPnl"))),
                ("value", format_optional_decimal(position.get("positionValue"))),
                ("leverage", format_position_leverage(position)),
            ],
        )
    open_orders = collect_open_orders_for_coin(info, account, coin, dex)
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
                ("sz", "sz"),
                ("oid", "oid"),
                ("time", "time"),
            ],
            show_count=False,
        )
    print_recent_history(info, account, coin=coin)


def collect_account_positions_and_orders(info: Info, account: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    positions: list[dict[str, str]] = []
    orders: list[dict[str, str]] = []
    seen_order_keys: set[tuple[str, int]] = set()
    dex_names = all_dex_names(info)
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
            positions.append(
                {
                    "dex": dex_name,
                    "coin": str(position.get("coin", "")),
                    "side": format_position_side(size),
                    "szi": decimal_to_plain(size),
                    "entryPx": format_optional_decimal(position.get("entryPx")),
                    "value": format_optional_decimal(position.get("positionValue")),
                    "nPnl": format_optional_decimal(position.get("unrealizedPnl")),
                    "roe": format_optional_percent(position.get("returnOnEquity")),
                    "liqPx": format_optional_decimal(position.get("liquidationPx")),
                    "lev": format_position_leverage(position),
                }
            )

        open_orders = collect_frontend_open_orders(info, account, dex)
        for order in open_orders:
            oid = int(order["oid"])
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
                    "sz": format_optional_quantity(order.get("sz", order.get("origSz"))),
                    "oid": str(oid),
                    "time": format_timestamp_ms(order.get("timestamp")),
                }
            )

    positions.sort(key=lambda row: (row["dex"], row["coin"], row["side"]))
    orders.sort(key=lambda row: (row["dex"], row["coin"], row["oid"]))
    return positions, orders


def collect_open_orders_for_coin(info: Info, account: str, coin: str, dex: str) -> list[dict[str, str]]:
    open_orders = collect_frontend_open_orders(info, account, dex)
    rows: list[dict[str, str]] = []

    for order in open_orders:
        if not position_matches_coin(str(order.get("coin", "")), coin):
            continue
        rows.append(
            {
                "coin": str(order.get("coin", "")),
                "side": str(order.get("side", "")),
                "type": format_open_order_type(order),
                "triggerPx": format_open_order_trigger_price(order),
                "limitPx": format_optional_decimal(order.get("limitPx")),
                "sz": format_optional_quantity(order.get("sz", order.get("origSz"))),
                "oid": str(order.get("oid", "")),
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
        fills = fetch_user_fills_window(info, account, start_ms, now_ms)
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
                        "closedPnl": format_optional_decimal(fill.get("closedPnl")),
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
            ("closedPnl", "closedPnl"),
        ],
        show_count=False,
    )


def query_account(args: argparse.Namespace) -> None:
    info, _exchange, account, signer, role = build_clients(args.network, args.timeout, "")
    if args.verbose:
        print("network:", args.network)
        print("account:", mask(account))
        print("signer:", mask(signer))
        print("account_role_source:", role)

    print_account_metrics(info, account)
    positions, orders = collect_account_positions_and_orders(info, account)
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
            ("nPnl", "nPnl"),
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
            ("sz", "sz"),
            ("oid", "oid"),
            ("time", "time"),
        ],
        show_count=False,
    )
    print_recent_history(info, account, show_empty=True)


def cancel_order(
    exchange: Exchange,
    info: Info,
    account: str,
    coin: str,
    dex: str,
    cancel_arg: str,
    dry_run: bool,
    price_rate: Decimal | None = None,
) -> None:
    open_orders = info.open_orders(account, dex=dex)
    log_event("open_orders_before", open_orders)

    if cancel_arg == "all":
        matching_orders = [order for order in open_orders if order.get("coin") == coin]
    else:
        oid = int(cancel_arg)
        matching_orders = [order for order in open_orders if order.get("coin") == coin and order.get("oid") == oid]

    if not matching_orders:
        print_account_metrics(info, account)
        print_box("Cancel", [("coin", coin), ("cancelled", "0")])
        return

    cancel_requests = [{"coin": coin, "oid": int(order["oid"])} for order in matching_orders]
    if dry_run:
        print_account_metrics(info, account)
        print_box("Run", [("dry_run", "1")])
        for order in matching_orders:
            print_order_row(
                order["coin"],
                order["side"],
                None,
                order["limitPx"],
                order_amount(order["limitPx"], order.get("origSz", order.get("sz", "0"))),
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

    clear_info_cache(info)
    print_account_metrics(info, account)
    print_box("Cancel", [("coin", coin), ("cancelled", str(len(matching_orders)))])
    for order in matching_orders:
        print_order_row(
            order["coin"],
            order["side"],
            None,
            order["limitPx"],
            order_amount(order["limitPx"], order.get("origSz", order.get("sz", "0"))),
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
        price = rounded_perp_price(price, sz_decimals)
        min_value_price = min(price, current_mid) if current_mid is not None else price
        size, notional, target_notional, minimum_value_notional = calc_size(
            amount,
            price,
            sz_decimals,
            min_value_price,
        )
        reference_price = price
        worst_notional = notional
        order_type = {"limit": {"tif": args.tif or "Alo"}}
        mode = "limit"

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
) -> None:
    if args.dry_run:
        print_account_metrics(info, account)
        print_box("Run", [("dry_run", "1")])
        print_order_plan_table(title, plans, price_rate)
        return

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
        return

    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    print_order_plan_table(title, plans, price_rate, statuses)


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


def place_order(args: argparse.Namespace) -> None:
    if args.query:
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
            or args.slippage != str(Exchange.DEFAULT_SLIPPAGE)
            or args.stop_entry
            or args.take_entry
            or args.take_profit
            or args.stop_loss
            or args.scale
            or args.scale_from
            or args.scale_to
        )
        if order_flags_without_side:
            raise ValueError("side is required when order options are used")
        print_market_overview(info, account, args.coin, coin, dex, price_rate, kline_mode)
        return

    if args.cancel is not None:
        cancel_order(exchange, info, account, coin, dex, args.cancel, args.dry_run, price_rate)
        return

    is_buy = parse_side(args.side)
    amount = Decimal(args.amount)
    if amount <= 0:
        raise ValueError("amount must be positive")
    slippage = parse_slippage(args.slippage)
    has_tpsl = bool(args.take_profit or args.stop_loss)

    mids = info.all_mids(dex)
    current_mid = Decimal(str(mids[coin])) if mids.get(coin) is not None else None
    log_event("all_mids_sample", {"dex": dex or "default", "coin": coin, "mid": mids.get(coin)})
    max_leverage = int(asset["maxLeverage"])

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
    parser.add_argument("side", nargs="?", help="buy/long/看多 or sell/short/看空. Not needed with --cancel.")
    parser.add_argument("amount", nargs="?", default="10", help="USD notional. Default: 10.")
    parser.add_argument("--price", help="Limit price. Defaults to same-side book level 10.")
    parser.add_argument("--market", action="store_true", help="Submit as a market order using IOC with slippage protection.")
    parser.add_argument("--slippage", default=str(Exchange.DEFAULT_SLIPPAGE), help="Market slippage protection. Default: 0.05. Also accepts 5%%.")
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
        help="Take-profit trigger price. Use ABS, ABS+OFFSET, or REL%%[+/-OFFSET] from the entry/position price. Append dRATIO to close only part of the order.",
    )
    parser.add_argument(
        "--sl",
        "--stop-loss",
        dest="stop_loss",
        help="Stop-loss trigger price. Use ABS, ABS+OFFSET, or REL%%[+/-OFFSET] from the entry/position price. Append dRATIO to close only part of the order.",
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
    kline_group = parser.add_mutually_exclusive_group()
    kline_group.add_argument("--day", action="store_true", help="Show the last 30 daily candles in market overview mode.")
    kline_group.add_argument("--week", action="store_true", help="Show the last 52 weekly candles in market overview mode.")
    parser.add_argument(
        "--cancel",
        nargs="?",
        const="all",
        help="Cancel instead of placing an order. Omit OID to cancel all open orders for this coin.",
    )
    parser.add_argument("--network", choices=["mainnet", "testnet"], default="mainnet", help="Default: mainnet.")
    parser.add_argument("--timeout", type=float, default=20, help="HTTP timeout in seconds. Default: 20.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; do not send cancel/order requests.")
    parser.add_argument("--query", "--status", action="store_true", help="Query all current positions and open orders.")
    parser.add_argument("--verbose", action="store_true", help="Print diagnostic details.")
    parser.add_argument("--submit", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(normalize_signed_option_values(sys.argv[1:]))
    query_words = {"query", "status", "positions", "orders", "持仓", "订单", "查询"}
    if args.coin and args.side is None and args.coin.strip().lower() in query_words:
        args.query = True
        args.coin = ""
    if not args.query and not args.coin:
        parser.error("coin is required unless query/status or --query is used")
    if args.market and args.price:
        parser.error("--market cannot be used with --price")
    if args.market and args.cancel is not None:
        parser.error("--market cannot be used with --cancel")
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
    if args.stop_entry and args.reduce_only:
        parser.error("--stop-entry cannot be used with --reduce-only")
    if args.take_entry and args.reduce_only:
        parser.error("--take-entry cannot be used with --reduce-only")
    if args.stop_entry and (args.price or args.market or args.book_level != 10 or args.tif is not None):
        parser.error("--stop-entry cannot be combined with --price, --market, --level, or --tif")
    if args.take_entry and (args.price or args.market or args.book_level != 10 or args.tif is not None):
        parser.error("--take-entry cannot be combined with --price, --market, --level, or --tif")
    if (args.take_profit_limit and not args.take_profit) or (args.stop_loss_limit and not args.stop_loss):
        parser.error("--tp-limit requires --tp, and --sl-limit requires --sl")
    has_tpsl = bool(args.take_profit or args.stop_loss)
    if args.stop_entry and args.take_entry:
        parser.error("--stop-entry and --take-entry are mutually exclusive")
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
