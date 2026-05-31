"""Text kline chart rendering."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from simple_hyper.formatting import decimal_to_display, pad_visible, visible_width


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
