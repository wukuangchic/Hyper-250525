#!/usr/bin/env python3
"""Print Hyperliquid perp metadata from info.meta()["universe"]."""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path


def ensure_local_venv() -> None:
    project_dir = Path(__file__).resolve().parent
    venv_dir = project_dir / ".venv"
    venv_python = venv_dir / "bin" / "python"
    if not venv_python.exists():
        return
    if Path(sys.prefix).resolve() == venv_dir.resolve():
        return
    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])


ensure_local_venv()

from hyperliquid.info import Info
from hyperliquid.utils import constants

from coin_aliases import load_coin_aliases


COIN_ALIASES = load_coin_aliases()


def canonical_coin_input(raw_coin: str) -> str:
    coin = raw_coin.strip()
    alias = COIN_ALIASES.get(coin.upper())
    if alias:
        return alias
    if ":" in coin:
        dex, name = coin.split(":", 1)
        return f"{dex.lower()}:{name.upper()}"
    return coin


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Show Hyperliquid perp meta()["universe"] as a table.')
    parser.add_argument("keyword", nargs="?", help="Optional coin/code filter, e.g. BTC, ETH, HYPE.")
    parser.add_argument("--dex", help='Builder DEX name, e.g. "xyz". Also inferred from keyword like xyz:GOLD.')
    parser.add_argument("--network", choices=["mainnet", "testnet"], default="mainnet", help="Default: mainnet.")
    parser.add_argument("--csv", action="store_true", help="Output CSV instead of a text table.")
    parser.add_argument("--limit", type=int, help="Limit number of rows.")
    parser.add_argument("--timeout", type=float, default=20, help="HTTP timeout in seconds. Default: 20.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_url = constants.TESTNET_API_URL if args.network == "testnet" else constants.MAINNET_API_URL
    info = Info(base_url, skip_ws=True, timeout=args.timeout)
    rows = []
    keyword_value = canonical_coin_input(args.keyword) if args.keyword else None
    inferred_dex = keyword_value.split(":", 1)[0] if keyword_value and ":" in keyword_value else None
    dex = args.dex or inferred_dex or ""
    keyword = keyword_value.upper() if keyword_value else None

    offset = 0
    if dex:
        dex_names = [item["name"] for item in info.perp_dexs()[1:]]
        if dex not in dex_names:
            raise ValueError(f"Unknown builder DEX: {dex}. Available: {', '.join(dex_names)}")
        offset = 110000 + dex_names.index(dex) * 10000

    for index, asset in enumerate(info.meta(dex=dex)["universe"]):
        name = asset.get("name", "")
        if keyword and keyword not in name.upper():
            continue
        rows.append(
            {
                "asset_id": offset + index,
                "dex": dex or "default",
                "name": name,
                "szDecimals": asset.get("szDecimals", ""),
                "maxLeverage": asset.get("maxLeverage", ""),
                "marginTableId": asset.get("marginTableId", ""),
                "isDelisted": asset.get("isDelisted", ""),
            }
        )

    if args.limit is not None:
        rows = rows[: args.limit]

    fields = ["asset_id", "dex", "name", "szDecimals", "maxLeverage", "marginTableId", "isDelisted"]
    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        return

    print("\t".join(fields))
    for row in rows:
        print("\t".join(str(row[field]) for field in fields))


if __name__ == "__main__":
    main()
