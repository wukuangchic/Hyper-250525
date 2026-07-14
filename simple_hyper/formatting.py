"""Console formatting helpers for Simple-Hyper output."""

from __future__ import annotations

import unicodedata
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional


def decimal_to_plain(value: Decimal | str) -> str:
    text = format(Decimal(str(value)), "f")
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
    return datetime.fromtimestamp(int(value) / 1000).strftime("%Y-%m-%d %H:%M:%S")


def format_short_timestamp_ms(value: Any) -> str:
    if value in (None, ""):
        return "n/a"
    return datetime.fromtimestamp(int(value) / 1000).strftime("%m-%d %H:%M")


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
