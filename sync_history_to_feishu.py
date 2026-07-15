#!/usr/bin/env python3
"""Idempotently sync Hyperliquid fills and funding history to Feishu Base."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

import requests

PROJECT_DIR = Path(__file__).resolve().parent
BASE_TOKEN = "JpGfblnloal7fNsRGfocfHNDnsd"
TRADES_TABLE_ID = "tblNp0SCcZkyk5Sf"
FUNDING_TABLE_ID = "tbl3ER99oeYGE2kI"
POSITIONS_TABLE_ID = "tblk0aXeGF0TtJ4p"
HL_API_URL = "https://api.hyperliquid.xyz/info"
FEISHU_API_ROOT = "https://open.feishu.cn/open-apis"
DEFAULT_START_MS = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
FEISHU_BATCH_SIZE = 500
RECENT_RECORD_LIMIT = 1
DEFAULT_OVERLAP_HOURS = 24
DEFAULT_MAX_RECORDS = 50_000


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("account_address", "FEISHU_APP_ID", "FEISHU_APP_SECRET"):
        values[key] = os.environ.get(key, values.get(key, ""))
    missing = [key for key in ("account_address", "FEISHU_APP_ID", "FEISHU_APP_SECRET") if not values.get(key)]
    if missing:
        raise RuntimeError(f"Missing required .env values: {', '.join(missing)}")
    return values


def request_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None,
                 token: str | None = None, timeout: int = 30) -> dict[str, Any] | list[Any]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.request(method, url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        body = getattr(exc.response, "text", "")
        raise RuntimeError(f"HTTP request failed for {url}: {exc}; {body}") from exc
    if isinstance(result, dict) and result.get("code") not in (None, 0):
        raise RuntimeError(f"API error from {url}: {json.dumps(result, ensure_ascii=False)}")
    return result


def get_feishu_token(app_id: str, app_secret: str) -> str:
    result = request_json(f"{FEISHU_API_ROOT}/auth/v3/tenant_access_token/internal", method="POST",
                          payload={"app_id": app_id, "app_secret": app_secret})
    assert isinstance(result, dict)
    return str(result["tenant_access_token"])


def hyperliquid(payload: dict[str, Any]) -> list[dict[str, Any]] | dict[str, Any]:
    result = request_json(HL_API_URL, method="POST", payload=payload)
    if not isinstance(result, (list, dict)):
        raise RuntimeError(f"Unexpected Hyperliquid response: {type(result).__name__}")
    return result


def resolve_main_account(account: str) -> str:
    role = hyperliquid({"type": "userRole", "user": account})
    if isinstance(role, dict) and role.get("role") == "agent":
        return str((role.get("data") or {})["user"])
    return account


def paged_history(kind: str, account: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    page_limit = 2000 if kind == "userFillsByTime" else 500
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = start_ms
    previous_cursor = -1
    while cursor <= end_ms:
        payload: dict[str, Any] = {"type": kind, "user": account, "startTime": cursor, "endTime": end_ms}
        if kind == "userFillsByTime":
            payload["aggregateByTime"] = False
        page = hyperliquid(payload)
        if not isinstance(page, list) or not page:
            break
        max_time = cursor
        for row in page:
            max_time = max(max_time, int(row.get("time", 0)))
            fingerprint = json.dumps(row, sort_keys=True, separators=(",", ":"))
            if fingerprint not in seen:
                seen.add(fingerprint)
                rows.append(row)
        if len(page) < page_limit or max_time >= end_ms:
            break
        # The API page can end in the middle of several fills sharing one
        # millisecond. Repeat the boundary millisecond and deduplicate rows;
        # advancing by +1 would silently lose the remaining fills.
        if max_time == cursor and cursor == previous_cursor:
            raise RuntimeError(f"Hyperliquid {kind} pagination stalled at {cursor}")
        previous_cursor = cursor
        cursor = max_time
    rows.sort(key=lambda item: int(item.get("time", 0)))
    return rows


def number(value: Any) -> int | float:
    parsed = Decimal(str(value or "0"))
    return int(parsed) if parsed == parsed.to_integral_value() else float(parsed)


def trade_fields(row: dict[str, Any], coin_options: set[str]) -> dict[str, Any]:
    px, sz = Decimal(str(row.get("px", 0))), Decimal(str(row.get("sz", 0)))
    raw_coin = str(row.get("coin", ""))
    timestamp_ms = int(row["time"])
    return {"timestamp_ms": timestamp_ms,
            "coin": display_trade_coin(raw_coin, coin_options), "dir": str(row.get("dir", "")),
            "px": number(px), "ntl": number(px * sz), "sz": number(sz), "fee": number(row.get("fee", 0)),
            "closedPnl": number(row.get("closedPnl", 0)),
            "oid": str(row.get("oid", "")), "tid": str(row.get("tid", ""))}


def funding_fields(row: dict[str, Any]) -> dict[str, Any]:
    delta = row.get("delta") or {}
    size = Decimal(str(delta.get("szi", 0)))
    timestamp_ms = int(row["time"])
    return {"timestamp_ms": timestamp_ms,
            "coin": str(delta.get("coin", "")), "sz": number(abs(size)),
            "side": "Long" if size > 0 else "Short" if size < 0 else "Flat",
            "payment": number(delta.get("usdc", 0)), "rate": number(delta.get("fundingRate", 0))}


def recent_records(token: str, table_id: str, field_names: Iterable[str]) -> list[dict[str, Any]]:
    url = (f"{FEISHU_API_ROOT}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/search"
           f"?page_size={RECENT_RECORD_LIMIT}")
    result = request_json(url, method="POST", token=token,
                          payload={"field_names": list(field_names), "sort": [{"field_name": "timestamp_ms", "desc": True}]})
    assert isinstance(result, dict)
    return list((result.get("data") or {}).get("items") or [])


def duplicate_search(token: str, table_id: str) -> dict[str, Any]:
    url = f"{FEISHU_API_ROOT}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/search?page_size=500"
    def search(operator: str, value: list[str]) -> dict[str, Any] | list[Any]:
        return request_json(url, method="POST", token=token, payload={
            "field_names": ["唯一值"],
            "filter": {"conjunction": "and", "conditions": [
                {"field_name": "唯一值", "operator": operator, "value": value}
            ]},
        })
    try:
        result = search("is", ["FALSE"])
    except RuntimeError as exc:
        # The funding table currently exposes formula FALSE as an empty select option.
        if table_id != FUNDING_TABLE_ID or "InvalidFilter" not in str(exc):
            raise
        result = search("isEmpty", [])
    assert isinstance(result, dict)
    return result.get("data") or {}


def delete_duplicates(token: str, table_id: str, dry_run: bool) -> int:
    data = duplicate_search(token, table_id)
    total = int(data.get("total") or 0)
    if dry_run:
        return total
    deleted = 0
    url = f"{FEISHU_API_ROOT}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/batch_delete"
    while data.get("items"):
        record_ids = [str(item["record_id"]) for item in data["items"]]
        request_json(url, method="POST", token=token, payload={"records": record_ids})
        deleted += len(record_ids)
        print(f"deleted duplicates from {table_id}: {deleted}/{total}", file=sys.stderr)
        data = duplicate_search(token, table_id)
    return deleted


def trade_coin_options(token: str) -> set[str]:
    url = f"{FEISHU_API_ROOT}/bitable/v1/apps/{BASE_TOKEN}/tables/{TRADES_TABLE_ID}/fields?page_size=100"
    result = request_json(url, token=token)
    assert isinstance(result, dict)
    for field in (result.get("data") or {}).get("items") or []:
        if field.get("field_name") == "coin":
            return {str(option["name"]) for option in (field.get("property") or {}).get("options") or []}
    raise RuntimeError("Trade table coin field was not found")


def display_trade_coin(raw_coin: str, options: set[str]) -> str:
    if raw_coin == "@107" and "HYPE/USDC" in options:
        return "HYPE/USDC"
    if ":" in raw_coin:
        dex, symbol = raw_coin.split(":", 1)
        aliases = {"JPY": "USDJPY", "SP500": "S&P500", "SPX": "S&P500", "USTECH": "QQQ"}
        candidate = f"{aliases.get(symbol, symbol)} ({dex})"
        return candidate
    if raw_coin in options:
        return raw_coin
    # Delisted assets may only remain as immutable exchange IDs such as #6760.
    return raw_coin


def fetch_current_positions(account: str, coin_options: set[str]) -> list[dict[str, Any]]:
    dex_rows = hyperliquid({"type": "perpDexs"})
    dex_names = [""]
    if isinstance(dex_rows, list):
        dex_names.extend(str(row["name"]) for row in dex_rows if isinstance(row, dict) and row.get("name"))
    def fetch_state(dex: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "clearinghouseState", "user": account}
        if dex:
            payload["dex"] = dex
        state = hyperliquid(payload)
        if not isinstance(state, dict):
            raise RuntimeError(f"Unexpected clearinghouseState response for dex {dex!r}")
        return state

    with ThreadPoolExecutor(max_workers=min(12, len(dex_names))) as executor:
        states = list(executor.map(fetch_state, dex_names))
    positions: list[dict[str, Any]] = []
    for state in states:
        for wrapper in state.get("assetPositions") or []:
            position = wrapper.get("position") or {}
            size = Decimal(str(position.get("szi", 0)))
            if size == 0:
                continue
            raw_coin = str(position.get("coin", ""))
            position_value = Decimal(str(position.get("positionValue", 0)))
            mark_px = abs(position_value / size)
            positions.append({
                "coin": display_trade_coin(raw_coin, coin_options),
                "szi": number(size),
                "markPx": number(mark_px),
            })
    positions.sort(key=lambda row: str(row["coin"]))
    return positions


def replace_current_positions(token: str, positions: list[dict[str, Any]], dry_run: bool) -> dict[str, int]:
    list_url = f"{FEISHU_API_ROOT}/bitable/v1/apps/{BASE_TOKEN}/tables/{POSITIONS_TABLE_ID}/records?page_size=500"
    existing_result = request_json(list_url, token=token)
    assert isinstance(existing_result, dict)
    existing = list((existing_result.get("data") or {}).get("items") or [])
    existing_count = int((existing_result.get("data") or {}).get("total") or len(existing))
    if dry_run:
        return {"cleared": existing_count, "created": len(positions)}
    delete_url = f"{FEISHU_API_ROOT}/bitable/v1/apps/{BASE_TOKEN}/tables/{POSITIONS_TABLE_ID}/records/batch_delete"
    while existing:
        request_json(delete_url, method="POST", token=token,
                     payload={"records": [str(item["record_id"]) for item in existing]})
        existing_result = request_json(list_url, token=token)
        assert isinstance(existing_result, dict)
        existing = list((existing_result.get("data") or {}).get("items") or [])
    created = batch_create(token, POSITIONS_TABLE_ID, positions, False)
    return {"cleared": existing_count, "created": created}


def latest_time(records: list[dict[str, Any]]) -> int | None:
    times = [int((record.get("fields") or {}).get("timestamp_ms", 0)) for record in records]
    return max(times) if times else None


def chunks(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(rows), size):
        yield rows[index:index + size]


def batch_create(token: str, table_id: str, rows: list[dict[str, Any]], dry_run: bool) -> int:
    if dry_run or not rows:
        return len(rows)
    url = f"{FEISHU_API_ROOT}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/batch_create"
    created = 0
    for batch in chunks(rows, FEISHU_BATCH_SIZE):
        request_json(url, method="POST", token=token, payload={"records": [{"fields": row} for row in batch]})
        created += len(batch)
    return created


def trim_oldest_for_capacity(token: str, table_id: str, incoming: int, max_records: int,
                             dry_run: bool, simulated_removed: int = 0) -> int:
    if max_records < 1:
        raise ValueError("--max-records must be at least 1")
    if incoming > max_records:
        raise RuntimeError(f"Incoming batch has {incoming} rows, exceeding table cap {max_records}")
    search_url = (f"{FEISHU_API_ROOT}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/search"
                  "?page_size=500")
    payload = {
        "field_names": ["timestamp_ms"],
        "sort": [{"field_name": "timestamp_ms", "desc": False}],
    }
    result = request_json(search_url, method="POST", token=token, payload=payload)
    assert isinstance(result, dict)
    data = result.get("data") or {}
    total = max(0, int(data.get("total") or 0) - simulated_removed)
    excess = max(0, total + incoming - max_records)
    if dry_run or excess == 0:
        return excess
    delete_url = f"{FEISHU_API_ROOT}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/batch_delete"
    deleted = 0
    while deleted < excess:
        if deleted:
            result = request_json(search_url, method="POST", token=token, payload=payload)
            assert isinstance(result, dict)
            data = result.get("data") or {}
        items = list(data.get("items") or [])[:excess - deleted]
        if not items:
            raise RuntimeError(f"Unable to find {excess - deleted} old records to delete from {table_id}")
        record_ids = [str(item["record_id"]) for item in items]
        request_json(delete_url, method="POST", token=token, payload={"records": record_ids})
        deleted += len(record_ids)
        print(f"deleted oldest from {table_id}: {deleted}/{excess}", file=sys.stderr)
    return deleted


def backfill_timestamp_ms(token: str, table_id: str) -> int:
    search_url = (f"{FEISHU_API_ROOT}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/search"
                  "?page_size=500")
    update_url = f"{FEISHU_API_ROOT}/bitable/v1/apps/{BASE_TOKEN}/tables/{table_id}/records/batch_update"
    updated = 0
    while True:
        result = request_json(search_url, method="POST", token=token, payload={
            "field_names": ["time", "timestamp_ms"],
            "filter": {"conjunction": "and", "conditions": [
                {"field_name": "timestamp_ms", "operator": "isEmpty", "value": []}
            ]},
        })
        assert isinstance(result, dict)
        items = list((result.get("data") or {}).get("items") or [])
        records = [
            {"record_id": item["record_id"], "fields": {"timestamp_ms": int(item["fields"]["time"])}}
            for item in items if (item.get("fields") or {}).get("time") is not None
        ]
        if not records:
            return updated
        request_json(update_url, method="POST", token=token, payload={"records": records})
        updated += len(records)
        print(f"backfilled timestamp_ms in {table_id}: {updated}", file=sys.stderr)


def parse_start(value: str) -> int:
    if value.isdigit():
        return int(value)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Hyperliquid histories to Feishu Base")
    parser.add_argument("--start", default=str(DEFAULT_START_MS), help="Start timestamp (ms) or ISO date; default 2023-01-01 UTC")
    parser.add_argument("--overlap-hours", type=float, default=DEFAULT_OVERLAP_HOURS,
                        help="Hours to refetch before the latest table time; default 24")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and compare without writing")
    parser.add_argument("--backfill-timestamps-only", action="store_true",
                        help="Fill empty timestamp_ms from time in both tables, then exit")
    parser.add_argument("--full-refresh", action="store_true",
                        help="Append all exchange history from --start; keep existing Base records")
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS,
                        help="Per-table record cap; delete oldest before create (default 50000)")
    args = parser.parse_args()
    env = load_env(PROJECT_DIR / ".env")
    token = get_feishu_token(env["FEISHU_APP_ID"], env["FEISHU_APP_SECRET"])
    if args.backfill_timestamps_only:
        result = {
            "backfilled": {
                "trades": backfill_timestamp_ms(token, TRADES_TABLE_ID),
                "funding": backfill_timestamp_ms(token, FUNDING_TABLE_ID),
            }
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    account = resolve_main_account(env["account_address"])
    start_ms, end_ms = parse_start(args.start), int(time.time() * 1000)
    deleted_trades = 0 if args.full_refresh else delete_duplicates(token, TRADES_TABLE_ID, args.dry_run)
    deleted_funding = 0 if args.full_refresh else delete_duplicates(token, FUNDING_TABLE_ID, args.dry_run)
    trade_names = ("timestamp_ms", "coin", "dir", "px", "ntl", "sz", "fee", "closedPnl", "oid", "tid")
    funding_names = ("timestamp_ms", "coin", "sz", "side", "payment", "rate")
    existing_trades = recent_records(token, TRADES_TABLE_ID, trade_names)
    existing_funding = recent_records(token, FUNDING_TABLE_ID, funding_names)
    newest_trade = latest_time(existing_trades)
    newest_funding = latest_time(existing_funding)
    overlap_ms = max(0, int(args.overlap_hours * 60 * 60 * 1000))
    trade_start = start_ms if args.full_refresh else (max(start_ms, newest_trade - overlap_ms) if newest_trade is not None else start_ms)
    funding_start = start_ms if args.full_refresh else (max(start_ms, newest_funding - overlap_ms) if newest_funding is not None else start_ms)
    options = trade_coin_options(token)
    positions = fetch_current_positions(account, options)
    trades = [trade_fields(row, options) for row in paged_history("userFillsByTime", account, trade_start, end_ms)]
    funding = [funding_fields(row) for row in paged_history("userFunding", account, funding_start, end_ms)]
    trimmed_trades = trim_oldest_for_capacity(
        token, TRADES_TABLE_ID, len(trades), args.max_records, args.dry_run,
        deleted_trades if args.dry_run else 0,
    )
    trimmed_funding = trim_oldest_for_capacity(
        token, FUNDING_TABLE_ID, len(funding), args.max_records, args.dry_run,
        deleted_funding if args.dry_run else 0,
    )
    label = "would_create" if args.dry_run else "created"
    delete_label = "would_delete_duplicates" if args.dry_run else "deleted_duplicates"
    summary = {"account": f"{account[:6]}...{account[-4:]}",
               delete_label: {"trades": deleted_trades, "funding": deleted_funding},
               "full_refresh": args.full_refresh,
               "overlap_hours": args.overlap_hours,
               "max_records": args.max_records,
               "deleted_oldest" if not args.dry_run else "would_delete_oldest": {
                   "trades": trimmed_trades, "funding": trimmed_funding,
               },
               "fetched": {"trades": len(trades), "funding": len(funding)},
               label: {"trades": batch_create(token, TRADES_TABLE_ID, trades, args.dry_run),
                       "funding": batch_create(token, FUNDING_TABLE_ID, funding, args.dry_run)}}
    summary["positions"] = replace_current_positions(token, positions, args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
