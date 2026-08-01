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
                self.assertEqual(chain["unclosed_cash_flow_after_fees"], "0")
                self.assertTrue(chain["profit_recognized"])
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
                self.assertEqual(chain["incremental_cash_after_fees"], "0")
                self.assertEqual(chain["unclosed_cash_flow_after_fees"], "109.9")
                self.assertFalse(chain["profit_recognized"])
            finally:
                connection.close()

    def test_split_birth_allocates_market_fill_and_profit_to_each_branch(self) -> None:
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
                        "economic_chain_id": "root",
                        "result": {
                            "response": {
                                "data": {"statuses": [{"filled": {"oid": 41}}]}
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
                        "market_oid": 41,
                        "economic_chain_id": "root",
                        "economic_chain_branch_version": 1,
                        "children": [
                            {
                                "oid": 42,
                                "size": "0.4",
                                "birth_slot": "near",
                                "economic_chain_id": "root_1",
                            },
                            {
                                "oid": 43,
                                "size": "0.6",
                                "birth_slot": "far",
                                "economic_chain_id": "root_2",
                            },
                        ],
                    },
                )
                ingest_fills(
                    connection,
                    [
                        {
                            "time": 1000,
                            "coin": "XYZ",
                            "oid": 41,
                            "tid": 11,
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
                            "oid": 42,
                            "tid": 12,
                            "side": "B",
                            "dir": "Open Long",
                            "px": "100",
                            "sz": "0.4",
                            "fee": "0.04",
                            "closedPnl": "0",
                            "crossed": False,
                        },
                        {
                            "time": 3000,
                            "coin": "XYZ",
                            "oid": 43,
                            "tid": 13,
                            "side": "B",
                            "dir": "Open Long",
                            "px": "90",
                            "sz": "0.6",
                            "fee": "0.06",
                            "closedPnl": "0",
                            "crossed": False,
                        },
                    ],
                )

                summaries = {
                    item["economic_chain_id"]: item
                    for item in chain_summaries(connection)
                }
                self.assertEqual(set(summaries), {"root_1", "root_2"})
                self.assertTrue(summaries["root_1"]["flat"])
                self.assertEqual(summaries["root_1"]["cash_flow"], "4.0")
                self.assertEqual(
                    summaries["root_1"]["incremental_cash_after_fees"],
                    "3.92",
                )
                self.assertEqual(summaries["root_1"]["exchange_closed_pnl"], "-20.0")
                self.assertTrue(summaries["root_2"]["flat"])
                self.assertEqual(summaries["root_2"]["cash_flow"], "12.0")
                self.assertEqual(
                    summaries["root_2"]["incremental_cash_after_fees"],
                    "11.88",
                )
                self.assertEqual(summaries["root_2"]["exchange_closed_pnl"], "-30.0")
                mappings = {
                    row["oid"]: row["economic_chain_id"]
                    for row in connection.execute(
                        "SELECT oid, economic_chain_id FROM order_map WHERE oid IN ('42', '43')"
                    )
                }
                self.assertEqual(mappings, {"42": "root_1", "43": "root_2"})
            finally:
                connection.close()

    def test_p10_market_submit_maps_fill_to_existing_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_db(Path(directory) / "ledger.sqlite3")
            try:
                ingest_action_record(
                    connection,
                    0,
                    {
                        "ts": 100,
                        "action": "p10_market_submit",
                        "economic_chain_id": "chain-p10",
                        "result": {
                            "response": {
                                "data": {"statuses": [{"filled": {"oid": 25}}]}
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
                            "oid": 25,
                            "tid": 4,
                            "side": "B",
                            "px": "60",
                            "sz": "1",
                            "fee": "0.1",
                            "closedPnl": "0",
                            "crossed": True,
                        }
                    ],
                )
                chain = chain_summaries(connection)[0]
                self.assertEqual(chain["economic_chain_id"], "chain-p10")
                self.assertEqual(chain["origins"], ["p10_market_submit"])
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
