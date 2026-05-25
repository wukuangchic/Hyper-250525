#!/usr/bin/env python3
"""Load local shorthand names for Hyperliquid perp symbols."""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path


ALIAS_FILE = Path(__file__).resolve().with_name("coin_aliases.csv")


def coin_alias_key(value: str) -> str:
    return value.strip().upper()


def load_coin_aliases(path: Path = ALIAS_FILE) -> dict[str, str]:
    if not path.exists():
        return {}

    aliases: dict[str, str] = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            alias = (row.get("alias") or "").strip()
            target = (row.get("target") or "").strip()
            if not alias or alias.startswith("#") or not target:
                continue
            aliases[coin_alias_key(alias)] = target
    return aliases


def load_coin_alias_rates(path: Path = ALIAS_FILE) -> dict[str, Decimal]:
    if not path.exists():
        return {}

    rates: dict[str, Decimal] = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            alias = (row.get("alias") or "").strip()
            target = (row.get("target") or "").strip()
            rate_text = (row.get("rate") or "").strip()
            if not alias or alias.startswith("#") or not target or not rate_text:
                continue
            try:
                rate = Decimal(rate_text)
            except InvalidOperation as exc:
                raise ValueError(f"Invalid rate for {alias}: {rate_text}") from exc
            if rate <= 0:
                continue
            rates[coin_alias_key(alias)] = rate
            rates[coin_alias_key(target)] = rate
    return rates
