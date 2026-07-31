import json
import tempfile
import unittest
from pathlib import Path

from grid_economic_ledger import (
    chain_summaries,
    connect_db,
    ingest_action_record,
    ingest_audit,
    ingest_fills,
    ingest_state,
    refresh_fill_links,
    result_oids,
)


class GridEconomicLedgerTests(unittest.TestCase):
    def test_reconciled_market_oid_is_discovered(self) -> None:
        self.assertEqual(
            result_oids({"market_oid": 10, "children": [{"oid": 11}]}),
            ["10"],
        )

    def test_market_and_reverse_fill_form_one_flat_profitable_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_db(Path(directory) / "ledger.sqlite3")
            try:
                ingest_action_record(
                    connection,
                    0,
                    {
                        "ts": 100,
                        "action": "panic_reduce_submit",
                        "coin": "XYZ",
                        "economic_chain_id": "chain-1",
                        "result": {
                            "response": {
                                "data": {"statuses": [{"filled": {"oid": 11}}]}
                            }
                        },
                    },
                )
                ingest_action_record(
                    connection,
                    1,
                    {
                        "ts": 101,
                        "action": "grid_birth_materialized",
                        "coin": "XYZ",
                        "economic_chain_id": "chain-1",
                        "children": [{"oid": 12}],
                    },
                )
                self.assertEqual(
                    ingest_fills(
                        connection,
                        [
                            {
                                "time": 1000,
                                "coin": "XYZ",
                                "oid": 11,
                                "tid": 1,
                                "side": "A",
                                "dir": "Close Long",
                                "px": "110",
                                "sz": "1",
                                "fee": "0.1",
                                "closedPnl": "-50",
                                "crossed": True,
                            },
                            {
                                "time": 2000,
                                "coin": "XYZ",
                                "oid": 12,
                                "tid": 2,
                                "side": "B",
                                "dir": "Open Long",
                                "px": "100",
                                "sz": "1",
                                "fee": "0.1",
                                "closedPnl": "0",
                                "crossed": False,
                            },
                        ],
                    ),
                    2,
                )
                chain = chain_summaries(connection)[0]
                self.assertTrue(chain["flat"])
                self.assertEqual(chain["net_size"], "0")
                self.assertEqual(chain["cash_flow"], "10")
                self.assertEqual(chain["incremental_cash_after_fees"], "9.8")
                self.assertEqual(chain["exchange_closed_pnl"], "-50")
                self.assertEqual(
                    chain["origins"],
                    ["grid_order_submit", "panic_reduce_submit"],
                )
            finally:
                connection.close()

    def test_open_chain_is_not_flat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_db(Path(directory) / "ledger.sqlite3")
            try:
                ingest_action_record(
                    connection,
                    0,
                    {
                        "ts": 100,
                        "action": "limit_chase_market_submit",
                        "economic_chain_id": "chain-open",
                        "result": {
                            "response": {
                                "data": {"statuses": [{"filled": {"oid": 21}}]}
                            }
                        },
                    },
                )
                ingest_fills(
                    connection,
                    [
                        {
                            "time": 1000,
                            "coin": "XYZ",
                            "oid": 21,
                            "tid": 3,
                            "side": "A",
                            "px": "110",
                            "sz": "1",
                            "fee": "0.1",
                            "closedPnl": "0",
                            "crossed": True,
                        }
                    ],
                )
                chain = chain_summaries(connection)[0]
                self.assertFalse(chain["flat"])
                self.assertEqual(chain["net_size"], "-1")
            finally:
                connection.close()

    def test_audit_cursor_is_incremental_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_path = root / "audit.jsonl"
            connection = connect_db(root / "ledger.sqlite3")
            try:
                first = {
                    "ts": 1,
                    "action": "grid_order_submit",
                    "economic_chain_id": "c1",
                    "result": {
                        "response": {
                            "data": {"statuses": [{"resting": {"oid": 31}}]}
                        }
                    },
                }
                second = {
                    "ts": 2,
                    "action": "grid_order_submit",
                    "economic_chain_id": "c2",
                    "result": {
                        "response": {
                            "data": {"statuses": [{"resting": {"oid": 32}}]}
                        }
                    },
                }
                audit_path.write_text(json.dumps(first) + "\n", encoding="utf-8")
                self.assertEqual(ingest_audit(connection, audit_path), 1)
                self.assertEqual(ingest_audit(connection, audit_path), 0)
                with audit_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(second) + "\n")
                self.assertEqual(ingest_audit(connection, audit_path), 1)
                count = connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
                self.assertEqual(count, 2)
            finally:
                connection.close()

    def test_state_mapping_backfills_existing_fill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = connect_db(root / "ledger.sqlite3")
            state_path = root / "server_batch.json"
            try:
                ingest_fills(
                    connection,
                    [
                        {
                            "time": 1000,
                            "coin": "XYZ",
                            "oid": 41,
                            "tid": 4,
                            "side": "B",
                            "px": "100",
                            "sz": "1",
                            "fee": "0",
                            "closedPnl": "0",
                            "crossed": False,
                        }
                    ],
                )
                state_path.write_text(
                    json.dumps(
                        [
                            {
                                "type": "grid",
                                "levels": [
                                    {"oid": 41, "economic_chain_id": "from-state"}
                                ],
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(ingest_state(connection, state_path), 1)
                self.assertEqual(refresh_fill_links(connection), 1)
                row = connection.execute(
                    "SELECT economic_chain_id FROM fills WHERE oid = '41'"
                ).fetchone()
                self.assertEqual(row["economic_chain_id"], "from-state")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
