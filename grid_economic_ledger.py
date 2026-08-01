#!/usr/bin/env python3
"""Build an append-only economic ledger for live Grid chains.

This process is intentionally observation-only.  It reads the worker action
audit, persisted Grid state, and Hyperliquid public info endpoints.  It never
constructs an Exchange client and cannot submit, modify, or cancel orders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import requests

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_DIR = Path(os.environ.get("SIMPLE_HYPER_STATE_DIR", PROJECT_DIR))
DEFAULT_AUDIT_PATH = DEFAULT_STATE_DIR / "logs" / "trail-action-audit.jsonl"
DEFAULT_BATCH_PATH = DEFAULT_STATE_DIR / "server_batch.json"
DEFAULT_DB_PATH = DEFAULT_STATE_DIR / "grid-economic-ledger.sqlite3"
DEFAULT_SUMMARY_PATH = DEFAULT_STATE_DIR / "logs" / "grid-economic-ledger-summary.json"
HL_API_URL = "https://api.hyperliquid.xyz/info"
FILL_PAGE_SIZE = 2_000
FUNDING_PAGE_SIZE = 500
OVERLAP_MS = 10 * 60 * 1_000
HTTP_RETRIES = 6


def decimal_value(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def load_account(env_path: Path) -> str:
    account = os.environ.get("account_address", "").strip()
    if not account and env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "account_address":
                account = value.strip().strip("'\"")
                break
    if not account:
        raise RuntimeError(f"account_address is missing from {env_path}")
    return account


def request_info(payload: dict[str, Any]) -> Any:
    last_error: Exception | None = None
    for attempt in range(HTTP_RETRIES):
        try:
            response = requests.post(
                HL_API_URL,
                json=payload,
                headers={"User-Agent": "simple-hyper-economic-ledger/1"},
                timeout=30,
            )
            if response.status_code == 429:
                time.sleep(min(10, 2 + attempt))
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < HTTP_RETRIES:
                time.sleep(min(10, 2 + attempt))
    raise RuntimeError(f"Hyperliquid info request failed: {type(last_error).__name__}: {last_error}")


def resolve_main_account(account: str) -> str:
    role = request_info({"type": "userRole", "user": account})
    if isinstance(role, dict) and role.get("role") == "agent":
        data = role.get("data") or {}
        if isinstance(data, dict) and data.get("user"):
            return str(data["user"])
    return account


def parse_time_ms(value: str | None, default_ms: int) -> int:
    if not value:
        return default_ms
    text = value.strip()
    if text.isdigit():
        parsed = int(text)
        return parsed if parsed > 10_000_000_000 else parsed * 1_000
    parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
    return int(parsed_dt.timestamp() * 1_000)


def paged_history(kind: str, account: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    page_size = FILL_PAGE_SIZE if kind == "userFillsByTime" else FUNDING_PAGE_SIZE
    cursor = start_ms
    previous_cursor = -1
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    while cursor <= end_ms:
        payload: dict[str, Any] = {
            "type": kind,
            "user": account,
            "startTime": cursor,
            "endTime": end_ms,
        }
        if kind == "userFillsByTime":
            payload["aggregateByTime"] = False
        page = request_info(payload)
        if not isinstance(page, list) or not page:
            break
        max_time = cursor
        for row in page:
            if not isinstance(row, dict):
                continue
            max_time = max(max_time, int(row.get("time") or 0))
            fingerprint = json.dumps(row, sort_keys=True, separators=(",", ":"))
            if fingerprint not in seen:
                seen.add(fingerprint)
                rows.append(row)
        if len(page) < page_size or max_time >= end_ms:
            break
        if max_time == cursor and previous_cursor == cursor:
            raise RuntimeError(f"{kind} pagination stalled at {cursor}")
        previous_cursor = cursor
        cursor = max_time
    rows.sort(key=lambda row: int(row.get("time") or 0))
    return rows


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS actions (
            source_offset INTEGER PRIMARY KEY,
            ts INTEGER NOT NULL,
            action TEXT NOT NULL,
            coin TEXT,
            oid TEXT,
            economic_chain_id TEXT,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS actions_chain_idx
            ON actions(economic_chain_id, ts);
        CREATE INDEX IF NOT EXISTS actions_oid_idx
            ON actions(oid);
        CREATE TABLE IF NOT EXISTS order_map (
            oid TEXT PRIMARY KEY,
            economic_chain_id TEXT,
            origin_action TEXT,
            first_seen_ts INTEGER NOT NULL,
            last_seen_ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS order_map_chain_idx
            ON order_map(economic_chain_id);
        CREATE TABLE IF NOT EXISTS fills (
            fill_key TEXT PRIMARY KEY,
            time_ms INTEGER NOT NULL,
            coin TEXT NOT NULL,
            oid TEXT,
            tid TEXT,
            side TEXT,
            direction TEXT,
            px TEXT NOT NULL,
            sz TEXT NOT NULL,
            fee TEXT NOT NULL,
            closed_pnl TEXT NOT NULL,
            crossed INTEGER NOT NULL,
            economic_chain_id TEXT,
            origin_action TEXT,
            raw_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS fills_time_idx ON fills(time_ms);
        CREATE INDEX IF NOT EXISTS fills_chain_idx
            ON fills(economic_chain_id, time_ms);
        CREATE INDEX IF NOT EXISTS fills_oid_idx ON fills(oid);
        CREATE TABLE IF NOT EXISTS fill_chain_allocations (
            source_oid TEXT NOT NULL,
            economic_chain_id TEXT NOT NULL,
            allocation_size TEXT NOT NULL,
            child_oid TEXT,
            birth_slot TEXT,
            created_ts INTEGER NOT NULL,
            PRIMARY KEY(source_oid, economic_chain_id)
        );
        CREATE INDEX IF NOT EXISTS fill_chain_allocations_chain_idx
            ON fill_chain_allocations(economic_chain_id);
        CREATE TABLE IF NOT EXISTS funding (
            funding_key TEXT PRIMARY KEY,
            time_ms INTEGER NOT NULL,
            coin TEXT NOT NULL,
            usdc TEXT NOT NULL,
            rate TEXT,
            raw_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS funding_time_idx ON funding(time_ms);
        """
    )
    return connection


def meta_get(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else None


def meta_set(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        """
        INSERT INTO meta(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def result_oids(record: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in ("oid", "market_oid"):
        if record.get(key) is not None:
            found.append(str(record[key]))
    result = record.get("result")
    if not isinstance(result, dict):
        return list(dict.fromkeys(found))
    response = result.get("response")
    if not isinstance(response, dict):
        return list(dict.fromkeys(found))
    data = response.get("data")
    if not isinstance(data, dict):
        return list(dict.fromkeys(found))
    statuses = data.get("statuses")
    if not isinstance(statuses, list):
        return list(dict.fromkeys(found))
    for status in statuses:
        if not isinstance(status, dict):
            continue
        for value in status.values():
            if isinstance(value, dict) and value.get("oid") is not None:
                found.append(str(value["oid"]))
    return list(dict.fromkeys(found))


def map_order(
    connection: sqlite3.Connection,
    oid: str,
    chain_id: str | None,
    action: str | None,
    ts: int,
) -> None:
    old = connection.execute(
        """
        SELECT economic_chain_id, origin_action, first_seen_ts, last_seen_ts
        FROM order_map
        WHERE oid = ?
        """,
        (oid,),
    ).fetchone()
    if old is None:
        connection.execute(
            """
            INSERT INTO order_map(
                oid, economic_chain_id, origin_action, first_seen_ts, last_seen_ts
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (oid, chain_id, action, ts, ts),
        )
        return
    resolved_chain = str(old["economic_chain_id"] or "") or chain_id
    old_action = str(old["origin_action"] or "")
    resolved_action = old_action or action
    if action in {
        "panic_reduce_submit",
        "limit_chase_market_submit",
        "p10_market_submit",
        "grid_order_submit",
    }:
        resolved_action = action
        if chain_id:
            resolved_chain = chain_id
    connection.execute(
        """
        UPDATE order_map
        SET economic_chain_id = ?, origin_action = ?, last_seen_ts = ?
        WHERE oid = ?
        """,
        (resolved_chain or None, resolved_action, max(ts, int(old["last_seen_ts"])), oid),
    )


def ingest_action_record(
    connection: sqlite3.Connection,
    offset: int,
    record: dict[str, Any],
) -> None:
    ts = int(record.get("ts") or 0)
    action = str(record.get("action") or "unknown")
    coin = str(record.get("coin") or "")
    chain_id = str(
        record.get("economic_chain_id")
        or record.get("birth_intent_cloid")
        or ""
    ).strip() or None
    oids = result_oids(record)
    primary_oid = oids[0] if oids else None
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    connection.execute(
        """
        INSERT OR IGNORE INTO actions(
            source_offset, ts, action, coin, oid, economic_chain_id, payload_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (offset, ts, action, coin, primary_oid, chain_id, payload),
    )
    for oid in oids:
        map_order(connection, oid, chain_id, action, ts)
    if action == "grid_birth_materialized":
        children = record.get("children")
        if isinstance(children, list):
            allocations: list[tuple[str, str, str | None, str | None]] = []
            for child in children:
                if not isinstance(child, dict):
                    continue
                child_chain_id = str(child.get("economic_chain_id") or chain_id or "").strip()
                child_oid = str(child["oid"]) if child.get("oid") is not None else None
                if child_oid is not None:
                    map_order(
                        connection,
                        child_oid,
                        child_chain_id or None,
                        "grid_order_submit",
                        ts,
                    )
                allocation_size = decimal_value(child.get("size"))
                if child_chain_id and allocation_size > 0:
                    allocations.append(
                        (
                            child_chain_id,
                            decimal_text(allocation_size),
                            child_oid,
                            str(child.get("birth_slot") or "") or None,
                        )
                    )
            source_oid = record.get("market_oid")
            distinct_chains = {item[0] for item in allocations}
            if source_oid is not None and len(distinct_chains) > 1:
                for child_chain_id, allocation_size, child_oid, birth_slot in allocations:
                    connection.execute(
                        """
                        INSERT INTO fill_chain_allocations(
                            source_oid, economic_chain_id, allocation_size,
                            child_oid, birth_slot, created_ts
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_oid, economic_chain_id) DO UPDATE SET
                            allocation_size = excluded.allocation_size,
                            child_oid = excluded.child_oid,
                            birth_slot = excluded.birth_slot,
                            created_ts = excluded.created_ts
                        """,
                        (
                            str(source_oid), child_chain_id, allocation_size,
                            child_oid, birth_slot, ts,
                        ),
                    )


def ingest_audit(connection: sqlite3.Connection, path: Path, reset: bool = False) -> int:
    if not path.exists():
        return 0
    stat = path.stat()
    stored_inode = meta_get(connection, "audit_inode")
    stored_offset = int(meta_get(connection, "audit_offset") or 0)
    if reset or stored_inode != str(stat.st_ino) or stored_offset > stat.st_size:
        stored_offset = 0
        if reset:
            connection.execute("DELETE FROM actions")
            connection.execute("DELETE FROM order_map")
            connection.execute("DELETE FROM fill_chain_allocations")
    inserted = 0
    with path.open("rb") as handle:
        handle.seek(stored_offset)
        while True:
            offset = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            before = connection.total_changes
            ingest_action_record(connection, offset, record)
            if connection.total_changes > before:
                inserted += 1
        final_offset = handle.tell()
    meta_set(connection, "audit_inode", stat.st_ino)
    meta_set(connection, "audit_offset", final_offset)
    return inserted


def ingest_state(connection: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        return 0
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        return 0
    mapped = 0
    now = int(time.time())
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "grid":
            continue
        for entry in row.get("levels") or []:
            if not isinstance(entry, dict) or entry.get("oid") is None:
                continue
            chain_id = str(
                entry.get("economic_chain_id")
                or entry.get("birth_intent_cloid")
                or ""
            ).strip() or None
            map_order(connection, str(entry["oid"]), chain_id, "grid_state", now)
            mapped += 1
    return mapped


def fill_key(row: dict[str, Any]) -> str:
    tid = str(row.get("tid") or "").strip()
    if tid:
        return f"tid:{tid}"
    raw = json.dumps(row, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ingest_fills(connection: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    inserted = 0
    for row in rows:
        oid = str(row.get("oid") or "")
        mapping = connection.execute(
            "SELECT economic_chain_id, origin_action FROM order_map WHERE oid = ?",
            (oid,),
        ).fetchone()
        chain_id = str(mapping["economic_chain_id"] or "") if mapping is not None else ""
        origin = str(mapping["origin_action"] or "") if mapping is not None else ""
        before = connection.total_changes
        connection.execute(
            """
            INSERT OR IGNORE INTO fills(
                fill_key, time_ms, coin, oid, tid, side, direction, px, sz,
                fee, closed_pnl, crossed, economic_chain_id, origin_action, raw_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill_key(row),
                int(row.get("time") or 0),
                str(row.get("coin") or ""),
                oid or None,
                str(row.get("tid") or "") or None,
                str(row.get("side") or ""),
                str(row.get("dir") or ""),
                decimal_text(decimal_value(row.get("px"))),
                decimal_text(decimal_value(row.get("sz"))),
                decimal_text(decimal_value(row.get("fee"))),
                decimal_text(decimal_value(row.get("closedPnl"))),
                1 if bool(row.get("crossed")) else 0,
                chain_id or None,
                origin or None,
                json.dumps(row, ensure_ascii=False, sort_keys=True, default=str),
            ),
        )
        if connection.total_changes > before:
            inserted += 1
    return inserted


def funding_key(row: dict[str, Any]) -> str:
    delta = row.get("delta") or {}
    raw_key = "|".join(
        [
            str(row.get("time") or ""),
            str(row.get("hash") or ""),
            str(delta.get("coin") or ""),
            str(delta.get("usdc") or ""),
        ]
    )
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def ingest_funding(connection: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    inserted = 0
    for row in rows:
        delta = row.get("delta") or {}
        if not isinstance(delta, dict):
            continue
        before = connection.total_changes
        connection.execute(
            """
            INSERT OR IGNORE INTO funding(
                funding_key, time_ms, coin, usdc, rate, raw_json
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                funding_key(row),
                int(row.get("time") or 0),
                str(delta.get("coin") or ""),
                decimal_text(decimal_value(delta.get("usdc"))),
                decimal_text(decimal_value(delta.get("fundingRate"))),
                json.dumps(row, ensure_ascii=False, sort_keys=True, default=str),
            ),
        )
        if connection.total_changes > before:
            inserted += 1
    return inserted


def refresh_fill_links(connection: sqlite3.Connection) -> int:
    before = connection.total_changes
    connection.execute(
        """
        UPDATE fills
        SET economic_chain_id = (
                SELECT order_map.economic_chain_id
                FROM order_map
                WHERE order_map.oid = fills.oid
            ),
            origin_action = (
                SELECT order_map.origin_action
                FROM order_map
                WHERE order_map.oid = fills.oid
            )
        WHERE EXISTS (
            SELECT 1 FROM order_map WHERE order_map.oid = fills.oid
        )
        """
    )
    return connection.total_changes - before


def chain_summaries(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    allocations_by_oid: dict[str, list[sqlite3.Row]] = defaultdict(list)
    allocation_table_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'fill_chain_allocations'
        """
    ).fetchone() is not None
    if allocation_table_exists:
        for allocation in connection.execute(
            """
            SELECT source_oid, economic_chain_id, allocation_size
            FROM fill_chain_allocations
            ORDER BY source_oid, economic_chain_id
            """
        ):
            allocations_by_oid[str(allocation["source_oid"])].append(allocation)
    allocation_filter = (
        """
           OR EXISTS (
                SELECT 1 FROM fill_chain_allocations
                WHERE fill_chain_allocations.source_oid = fills.oid
           )
        """
        if allocation_table_exists
        else ""
    )
    rows = connection.execute(
        f"""
        SELECT economic_chain_id, origin_action, time_ms, side, px, sz, fee,
               closed_pnl, coin, oid
        FROM fills
        WHERE (economic_chain_id IS NOT NULL AND economic_chain_id != '')
        {allocation_filter}
        ORDER BY economic_chain_id, time_ms, fill_key
        """
    )
    for row in rows:
        oid = str(row["oid"] or "")
        allocations = allocations_by_oid.get(oid, [])
        pieces: list[tuple[str, Decimal]] = []
        if allocations:
            weights = [decimal_value(item["allocation_size"]) for item in allocations]
            total_weight = sum(weights, Decimal("0"))
            allocated_ratio = Decimal("0")
            for index, (allocation, weight) in enumerate(zip(allocations, weights)):
                ratio = (
                    Decimal("1") - allocated_ratio
                    if index + 1 == len(allocations)
                    else weight / total_weight
                )
                allocated_ratio += ratio
                pieces.append((str(allocation["economic_chain_id"]), ratio))
        else:
            chain_id = str(row["economic_chain_id"] or "").strip()
            if chain_id:
                pieces.append((chain_id, Decimal("1")))

        for chain_id, ratio in pieces:
            item = grouped.setdefault(
                chain_id,
                {
                    "economic_chain_id": chain_id,
                    "coins": set(),
                    "origins": set(),
                    "fill_count": 0,
                    "first_fill_ms": int(row["time_ms"]),
                    "last_fill_ms": int(row["time_ms"]),
                    "net_size": Decimal("0"),
                    "cash_flow": Decimal("0"),
                    "fees": Decimal("0"),
                    "closed_pnl": Decimal("0"),
                    "oids": set(),
                },
            )
            side = str(row["side"] or "").upper()
            size = decimal_value(row["sz"]) * ratio
            price = decimal_value(row["px"])
            signed_size = size if side == "B" else -size if side == "A" else Decimal("0")
            cash = -(signed_size * price)
            item["coins"].add(str(row["coin"]))
            if row["origin_action"]:
                item["origins"].add(str(row["origin_action"]))
            if row["oid"]:
                item["oids"].add(str(row["oid"]))
            item["fill_count"] += 1
            item["first_fill_ms"] = min(item["first_fill_ms"], int(row["time_ms"]))
            item["last_fill_ms"] = max(item["last_fill_ms"], int(row["time_ms"]))
            item["net_size"] += signed_size
            item["cash_flow"] += cash
            item["fees"] += decimal_value(row["fee"]) * ratio
            item["closed_pnl"] += decimal_value(row["closed_pnl"]) * ratio

    summaries: list[dict[str, Any]] = []
    for item in grouped.values():
        flat = abs(item["net_size"]) <= Decimal("0.000000000001")
        raw_cash_after_fees = item["cash_flow"] - item["fees"]
        recognized_profit = raw_cash_after_fees if flat else Decimal("0")
        summaries.append(
            {
                "economic_chain_id": item["economic_chain_id"],
                "coins": sorted(item["coins"]),
                "origins": sorted(item["origins"]),
                "fill_count": item["fill_count"],
                "first_fill_ms": item["first_fill_ms"],
                "last_fill_ms": item["last_fill_ms"],
                "net_size": decimal_text(item["net_size"]),
                "cash_flow": decimal_text(item["cash_flow"]),
                "fees": decimal_text(item["fees"]),
                "incremental_cash_after_fees": decimal_text(recognized_profit),
                "unclosed_cash_flow_after_fees": decimal_text(
                    Decimal("0") if flat else raw_cash_after_fees
                ),
                "exchange_closed_pnl": decimal_text(item["closed_pnl"]),
                "flat": flat,
                "profit_recognized": flat,
                "oid_count": len(item["oids"]),
            }
        )
    return sorted(summaries, key=lambda item: (item["last_fill_ms"], item["economic_chain_id"]))


def build_summary(
    connection: sqlite3.Connection,
    *,
    audit_inserted: int,
    state_mapped: int,
    fills_inserted: int,
    funding_inserted: int,
    links_refreshed: int,
) -> dict[str, Any]:
    fills = connection.execute(
        """
        SELECT time_ms, fee, closed_pnl, crossed, economic_chain_id, origin_action,
               px, sz
        FROM fills
        """
    ).fetchall()
    funding_rows = connection.execute("SELECT usdc FROM funding").fetchall()
    chains = chain_summaries(connection)
    liquidity: dict[str, dict[str, Decimal | int]] = {
        "maker": {"fills": 0, "turnover": Decimal("0"), "closed_pnl": Decimal("0"), "fees": Decimal("0")},
        "taker": {"fills": 0, "turnover": Decimal("0"), "closed_pnl": Decimal("0"), "fees": Decimal("0")},
    }
    origins: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"fills": 0, "turnover": Decimal("0"), "closed_pnl": Decimal("0"), "fees": Decimal("0")}
    )
    linked = 0
    first_ms: int | None = None
    last_ms: int | None = None
    total_closed_pnl = Decimal("0")
    total_fees = Decimal("0")
    for row in fills:
        first_ms = int(row["time_ms"]) if first_ms is None else min(first_ms, int(row["time_ms"]))
        last_ms = int(row["time_ms"]) if last_ms is None else max(last_ms, int(row["time_ms"]))
        fee = decimal_value(row["fee"])
        pnl = decimal_value(row["closed_pnl"])
        turnover = decimal_value(row["px"]) * decimal_value(row["sz"])
        total_closed_pnl += pnl
        total_fees += fee
        bucket = liquidity["taker" if int(row["crossed"]) else "maker"]
        bucket["fills"] = int(bucket["fills"]) + 1
        bucket["turnover"] = decimal_value(bucket["turnover"]) + turnover
        bucket["closed_pnl"] = decimal_value(bucket["closed_pnl"]) + pnl
        bucket["fees"] = decimal_value(bucket["fees"]) + fee
        origin = str(row["origin_action"] or "unmapped")
        origin_bucket = origins[origin]
        origin_bucket["fills"] = int(origin_bucket["fills"]) + 1
        origin_bucket["turnover"] = decimal_value(origin_bucket["turnover"]) + turnover
        origin_bucket["closed_pnl"] = decimal_value(origin_bucket["closed_pnl"]) + pnl
        origin_bucket["fees"] = decimal_value(origin_bucket["fees"]) + fee
        if row["economic_chain_id"]:
            linked += 1
    total_funding = sum((decimal_value(row["usdc"]) for row in funding_rows), Decimal("0"))
    flat_chains = [chain for chain in chains if chain["flat"]]
    open_chains = [chain for chain in chains if not chain["flat"]]
    flat_cash = sum(
        (decimal_value(chain["incremental_cash_after_fees"]) for chain in flat_chains),
        Decimal("0"),
    )

    def numeric_bucket(bucket: dict[str, Decimal | int]) -> dict[str, Any]:
        closed = decimal_value(bucket["closed_pnl"])
        fees = decimal_value(bucket["fees"])
        return {
            "fills": int(bucket["fills"]),
            "turnover": decimal_text(decimal_value(bucket["turnover"])),
            "closed_pnl": decimal_text(closed),
            "fees": decimal_text(fees),
            "net_before_funding": decimal_text(closed - fees),
        }

    action_counts = {
        str(row["action"]): int(row["count"])
        for row in connection.execute(
            "SELECT action, COUNT(*) AS count FROM actions GROUP BY action ORDER BY count DESC"
        )
    }
    return {
        "schema_version": 1,
        "generated_at": int(time.time()),
        "coverage": {
            "first_fill_ms": first_ms,
            "last_fill_ms": last_ms,
            "fills": len(fills),
            "funding_rows": len(funding_rows),
            "linked_fills": linked,
            "unlinked_fills": len(fills) - linked,
            "linked_rate": round(linked / len(fills), 6) if fills else 0,
        },
        "ingestion": {
            "audit_records_added": audit_inserted,
            "state_oids_seen": state_mapped,
            "fills_added": fills_inserted,
            "funding_added": funding_inserted,
            "fill_links_refreshed": links_refreshed,
        },
        "account_pnl": {
            "closed_pnl": decimal_text(total_closed_pnl),
            "fees": decimal_text(total_fees),
            "funding": decimal_text(total_funding),
            "net_realized": decimal_text(total_closed_pnl - total_fees + total_funding),
        },
        "liquidity": {name: numeric_bucket(bucket) for name, bucket in liquidity.items()},
        "origins": {
            name: numeric_bucket(bucket)
            for name, bucket in sorted(origins.items())
        },
        "chains": {
            "total": len(chains),
            "flat": len(flat_chains),
            "open": len(open_chains),
            "flat_incremental_cash_after_fees": decimal_text(flat_cash),
            "recent": chains[-100:],
        },
        "actions": action_counts,
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-path", type=Path, default=PROJECT_DIR / ".env")
    parser.add_argument("--audit-path", type=Path, default=DEFAULT_AUDIT_PATH)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_BATCH_PATH)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--summary-path", type=Path, default=DEFAULT_SUMMARY_PATH)
    parser.add_argument(
        "--start",
        help="Initial history start (ISO-8601, epoch seconds, or epoch milliseconds)",
    )
    parser.add_argument("--no-fetch", action="store_true", help="Only ingest local audit/state")
    parser.add_argument(
        "--rebuild-audit",
        action="store_true",
        help="Re-read the action audit from byte zero without deleting fills",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, Any]:
    connection = connect_db(args.db_path)
    try:
        audit_inserted = ingest_audit(connection, args.audit_path, reset=args.rebuild_audit)
        state_mapped = ingest_state(connection, args.state_path)
        fills_inserted = 0
        funding_inserted = 0
        if not args.no_fetch:
            now_ms = int(time.time() * 1_000)
            default_start = now_ms - 7 * 24 * 60 * 60 * 1_000
            configured_start = parse_time_ms(args.start, default_start)
            fill_cursor = int(meta_get(connection, "fills_cursor_ms") or configured_start)
            funding_cursor = int(meta_get(connection, "funding_cursor_ms") or configured_start)
            account = resolve_main_account(load_account(args.env_path))
            fills = paged_history(
                "userFillsByTime",
                account,
                max(configured_start, fill_cursor - OVERLAP_MS),
                now_ms,
            )
            funding = paged_history(
                "userFunding",
                account,
                max(configured_start, funding_cursor - OVERLAP_MS),
                now_ms,
            )
            fills_inserted = ingest_fills(connection, fills)
            funding_inserted = ingest_funding(connection, funding)
            meta_set(
                connection,
                "fills_cursor_ms",
                max([fill_cursor, *[int(row.get("time") or 0) for row in fills]]),
            )
            meta_set(
                connection,
                "funding_cursor_ms",
                max([funding_cursor, *[int(row.get("time") or 0) for row in funding]]),
            )
        links_refreshed = refresh_fill_links(connection)
        summary = build_summary(
            connection,
            audit_inserted=audit_inserted,
            state_mapped=state_mapped,
            fills_inserted=fills_inserted,
            funding_inserted=funding_inserted,
            links_refreshed=links_refreshed,
        )
        write_json_atomic(args.summary_path, summary)
        connection.commit()
        return summary
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = run(args)
    except Exception as exc:
        print(f"grid-economic-ledger: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    coverage = summary["coverage"]
    chains = summary["chains"]
    pnl = summary["account_pnl"]
    print(
        "grid-economic-ledger: "
        f"fills={coverage['fills']} linked={coverage['linked_fills']} "
        f"chains={chains['total']} flat={chains['flat']} open={chains['open']} "
        f"net_realized={pnl['net_realized']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
