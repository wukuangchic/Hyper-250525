"""Order parsing, sizing, and price rounding helpers."""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any

from simple_hyper.formatting import decimal_to_display, decimal_to_plain, format_percent


MIN_NOTIONAL = Decimal("10")


def parse_side(side: str) -> bool:
    normalized = side.strip().lower()
    if normalized == "buy":
        return True
    if normalized == "sell":
        return False
    raise ValueError(f"Unknown side: {side}. Use buy or sell.")


def normalize_coin_input(raw_coin: str) -> list[str]:
    coin = canonical_coin_input(raw_coin)
    return [coin, coin.upper()]


def canonical_coin_input(raw_coin: str) -> str:
    coin = raw_coin.strip()
    if ":" in coin:
        dex, name = coin.split(":", 1)
        return f"{dex.lower()}:{name.upper()}"
    return coin


def coin_dex(raw_coin: str) -> str:
    coin = canonical_coin_input(raw_coin)
    return coin.split(":", 1)[0] if ":" in coin else ""


def coin_display_rate(raw_coin: str, resolved_coin: str) -> Decimal | None:
    return None


def resolve_perp_asset(info: Any, raw_coin: str) -> tuple[str, dict[str, Any]]:
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
    if size_ratio <= 0 or size_ratio > 2:
        raise ValueError(f"{label} size ratio must be greater than 0 and at most 2")
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


def ladder_count_to_end_prices(start: Decimal, end: Decimal, count: int, sz_decimals: int, label: str) -> list[Decimal]:
    if count < 2:
        raise ValueError(f"{label} count must be >= 2")
    if start <= 0 or end <= 0:
        raise ValueError(f"{label} start and end prices must be positive")
    if start == end:
        raise ValueError(f"{label} start and end prices must be different")
    step = (end - start) / Decimal(count - 1)
    prices = [rounded_perp_price(start + step * Decimal(index), sz_decimals) for index in range(count)]
    price_keys = {decimal_to_plain(price) for price in prices}
    if len(price_keys) != len(prices):
        raise ValueError(f"{label} prices collapse to duplicates after rounding; widen the range or reduce count")
    return prices


def ladder_for_prices(start: Decimal, count: int, step: Decimal, sz_decimals: int, label: str) -> list[Decimal]:
    if count < 2:
        raise ValueError(f"{label} count must be >= 2")
    if start <= 0:
        raise ValueError(f"{label} start price must be positive")
    if step == 0:
        raise ValueError(f"{label} step must be non-zero")

    prices: list[Decimal] = []
    seen: set[str] = set()
    current = start
    for _ in range(count):
        price = rounded_perp_price(current, sz_decimals)
        price_key = decimal_to_plain(price)
        if price_key in seen:
            raise ValueError(f"{label} prices collapse to duplicates after rounding; widen the ladder or use a larger step")
        seen.add(price_key)
        prices.append(price)
        current = current + step
    return prices


def resolve_ladder_step(base_px: Decimal, step_spec: str, label: str) -> Decimal:
    text = step_spec.strip()
    if not text or text[0] not in "+-":
        raise ValueError(f"Invalid {label} step: {step_spec}. Use +STEP, -STEP, +PERCENT, or -PERCENT.")

    sign = text[0]
    magnitude_text = text[1:]
    if not magnitude_text:
        raise ValueError(f"Invalid {label} step: {step_spec}. Use +STEP, -STEP, +PERCENT, or -PERCENT.")

    if magnitude_text.endswith("%"):
        step = base_px * (Decimal(magnitude_text[:-1]) / Decimal("100"))
    else:
        step = Decimal(magnitude_text)
    if step <= 0:
        raise ValueError(f"{label} step must be positive")

    return step if sign == "+" else -step


def ladder_while_prices(start: Decimal, end: Decimal, step: Decimal, sz_decimals: int, label: str) -> list[Decimal]:
    if step == 0:
        raise ValueError(f"{label} step must be non-zero")
    if start <= 0 or end <= 0:
        raise ValueError(f"{label} start and end prices must be positive")
    if step > 0 and end < start:
        raise ValueError(f"{label} end price must be at or above the start price for a positive step")
    if step < 0 and end > start:
        raise ValueError(f"{label} end price must be at or below the start price for a negative step")

    prices: list[Decimal] = []
    seen: set[str] = set()
    current = start
    while True:
        price = rounded_perp_price(current, sz_decimals)
        price_key = decimal_to_plain(price)
        if price_key in seen:
            raise ValueError(f"{label} prices collapse to duplicates after rounding; widen the ladder or use a larger step")
        seen.add(price_key)
        prices.append(price)

        next_raw = current + step
        if step > 0:
            if next_raw > end:
                break
        else:
            if next_raw < end:
                break
        current = next_raw
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
    if ratio <= 0 or ratio > 2:
        raise ValueError(f"{label} must be greater than 0 and at most 2")
    return ratio


def tpsl_relative_sign(label: str, position_is_long: bool) -> str:
    if label == "tp":
        return "+" if position_is_long else "-"
    return "-" if position_is_long else "+"


def parse_tpsl_spec(
    value: str,
    base_px: Decimal | None,
    label: str,
    position_is_long: bool | None = None,
) -> tuple[Decimal, Decimal | None, Decimal]:
    text = value.strip().replace(" ", "")
    match = TPSL_SPEC_RE.fullmatch(text)
    if not match:
        raise ValueError(
            f"Invalid {label} value: {value}. Use PRICE, PRICE+OFFSET, PRICE-OFFSET, PRICE+PERCENT, PRICE-PERCENT, "
            "or REL%[+/-OFFSET] when a reference price is available. Append dRATIO to close only part of the order."
        )

    raw_trigger_sign = match.group("trigger_sign")
    trigger_sign = raw_trigger_sign or "+"
    trigger_text = match.group("trigger")
    if match.group("trigger_pct"):
        if base_px is None:
            raise ValueError(f"{label} relative trigger prices require a reference price")
        if not raw_trigger_sign and position_is_long is not None:
            trigger_sign = tpsl_relative_sign(label, position_is_long)
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
    position_is_long: bool | None = None,
) -> tuple[Decimal, Decimal | None, Decimal]:
    trigger_px, inline_limit_px, size_ratio = parse_tpsl_spec(value, base_px, label, position_is_long)
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
    "--total",
    "--offset",
    "--trend",
    "--avg",
    "--gap",
    "--range",
    "--for",
    "--while",
    "--cancel",
    "--network",
    "--timeout",
}

SIGNED_STEP_OPTION_OFFSETS = {"--for": 2, "--while": 2, "--range": 3}


def protect_ladder_step_values(argv: list[str]) -> list[str]:
    protected: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        step_offset = SIGNED_STEP_OPTION_OFFSETS.get(token)
        if step_offset is not None and index + step_offset < len(argv):
            protected.extend(argv[index : index + step_offset])
            step = argv[index + step_offset]
            if step.startswith("-") and step not in VALUE_OPTION_STRINGS:
                protected.append(f"={step}")
            else:
                protected.append(step)
            index += step_offset + 1
            continue
        protected.append(token)
        index += 1
    return protected


def unprotect_ladder_step_value(value: str) -> str:
    if value.startswith("=-") or value.startswith("=+"):
        return value[1:]
    return value


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
