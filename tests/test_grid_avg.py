import unittest
from argparse import Namespace
from decimal import Decimal

from hl_order import (
    asset_requires_isolated_margin,
    build_grid_batch_row,
    build_grid_orders,
    grid_avg_bounds,
    grid_avg_multiplier,
    grid_avg_size_pair,
    grid_avg_topup_params,
    format_grid_detail_rows,
    grid_query_avg_summary,
    refresh_grid_row_strategy_params,
)
from trail_worker import (
    active_grid_oids,
    build_grid_panic_reduce_order,
    clear_grid_side_cap_entries,
    apply_grid_add_risk_brake,
    dense_grid_entries,
    defer_paused_grid_restore_if_crossing,
    grid_panic_ratio,
    grid_active_cap_restore_allowed,
    grid_active_cap_pause_candidates,
    grid_margin_gap_multiplier,
    grid_order_entry,
    grid_risk_density_pause_candidates,
    grid_risk_density_restore_allowed,
    move_grid_order_away_from_active,
    near_grid_orders_if_stale,
    next_depth_order,
    prune_add_risk_brake_state,
    preserve_replacement_order,
    prune_grid_levels,
    regrid_dense_entries,
    replacement_order_from_fill,
    skip_stale_grid_recovery,
    skip_unknown_oid_grid_recovery,
    submit_grid_order_entry,
    trim_excess_grid_entries,
)


class GridAvgTests(unittest.TestCase):
    def test_grid_detail_rows_sort_all_sides_by_price_desc(self) -> None:
        row = {
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99", "size": "1"},
                {"side": "sell", "status": "active", "oid": 2, "price": "105", "size": "1"},
                {"side": "sell", "status": "active", "oid": 3, "price": "103", "size": "1"},
                {"side": "buy", "status": "active", "oid": 4, "price": "101", "size": "1"},
            ]
        }

        rows = format_grid_detail_rows(row, {1, 2, 3, 4})

        self.assertEqual([item["price"] for item in rows], ["105.00", "103.00", "101.00", "99.00"])

    def test_grid_detail_rows_insert_mid_marker_by_price(self) -> None:
        row = {
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99", "size": "1"},
                {"side": "sell", "status": "active", "oid": 2, "price": "105", "size": "1"},
                {"side": "sell", "status": "active", "oid": 3, "price": "101", "size": "1"},
            ]
        }

        rows = format_grid_detail_rows(row, {1, 2, 3}, Decimal("100"))

        self.assertEqual(
            [(item["status"], item["price"]) for item in rows],
            [
                ("active", "105.00"),
                ("active", "101.00"),
                ("mid", "--- 100.00 ---"),
                ("active", "99.00"),
            ],
        )

    def test_grid_detail_rows_show_only_pending_filled_replacement_state(self) -> None:
        row = {
            "levels": [
                {
                    "side": "buy",
                    "status": "filled",
                    "oid": 1,
                    "price": "99",
                    "size": "1",
                    "replacement_pending": True,
                },
                {
                    "side": "sell",
                    "status": "filled",
                    "oid": 2,
                    "price": "101",
                    "size": "1",
                    "replacement_pending": False,
                    "replacement_processed_at": 123,
                },
            ]
        }

        rows = format_grid_detail_rows(row, set())

        self.assertEqual([item["status"] for item in rows], ["filled_pending"])

    def test_panic_ratio_short_uses_liq_above_mid_and_buy_below_mid(self) -> None:
        row = {
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "63", "size": "1"},
                {"side": "buy", "status": "active", "oid": 2, "price": "60", "size": "1"},
            ]
        }

        ratio = grid_panic_ratio(row, Decimal("-4"), Decimal("64"), Decimal("72"))

        self.assertEqual(ratio, Decimal("8"))

    def test_panic_ratio_long_uses_liq_below_mid_and_sell_above_mid(self) -> None:
        row = {
            "levels": [
                {"side": "sell", "status": "active", "oid": 1, "price": "105", "size": "1"},
                {"side": "sell", "status": "active", "oid": 2, "price": "110", "size": "1"},
            ]
        }

        ratio = grid_panic_ratio(row, Decimal("4"), Decimal("100"), Decimal("60"))

        self.assertEqual(ratio, Decimal("8"))

    def test_panic_ratio_ignores_inverted_price_geometry(self) -> None:
        short_row = {"levels": [{"side": "buy", "status": "active", "oid": 1, "price": "65", "size": "1"}]}
        long_row = {"levels": [{"side": "sell", "status": "active", "oid": 1, "price": "99", "size": "1"}]}

        self.assertIsNone(grid_panic_ratio(short_row, Decimal("-4"), Decimal("64"), Decimal("72")))
        self.assertIsNone(grid_panic_ratio(long_row, Decimal("4"), Decimal("100"), Decimal("60")))

    def test_panic_reduce_order_uses_base_size_ioc_and_reduce_only(self) -> None:
        class FakeExchange:
            def _slippage_price(self, coin, is_buy, slippage, reference_price):
                self.args = (coin, is_buy, Decimal(str(slippage)), Decimal(str(reference_price)))
                return reference_price * (1.001 if is_buy else 0.999)

        row = {
            "base_buy_size": "0.16",
            "base_sell_size": "0.16",
            "slippage": "0.001",
            "sz_decimals": 2,
        }

        order = build_grid_panic_reduce_order(
            FakeExchange(),
            row,
            "HYPE",
            {"szDecimals": 2},
            Decimal("64"),
            Decimal("-4.28"),
        )

        self.assertIsNotNone(order)
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["size"], "0.16")
        self.assertTrue(order["reduce_only"])
        self.assertEqual(order["plan"]["order_type"], {"limit": {"tif": "Ioc"}})
        self.assertTrue(order["plan"]["reduce_only"])

    def test_add_risk_brake_cancels_nearest_same_side_after_two_open_fills(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.cancelled = []

            def bulk_cancel(self, requests):
                self.cancelled.extend(requests)
                return {"status": "ok"}

        row = {
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99", "size": "1", "is_buy": True},
                {"side": "buy", "status": "active", "oid": 2, "price": "98", "size": "1", "is_buy": True},
                {"side": "sell", "status": "active", "oid": 3, "price": "101", "size": "1", "is_buy": False},
                {"side": "buy", "status": "filled", "oid": 10, "is_buy": True, "fill": {"time": 1000, "dir": "Open Long", "oid": 10}},
                {"side": "buy", "status": "filled", "oid": 11, "is_buy": True, "fill": {"time": 2000, "dir": "Open Long", "oid": 11}},
            ]
        }

        cancelled = apply_grid_add_risk_brake(FakeExchange(), "BTC", row, row["levels"][-2:], Decimal("1"), 123)

        self.assertEqual(cancelled, 1)
        self.assertEqual(row["levels"][0]["status"], "brake_near_add_risk")
        self.assertEqual(row["levels"][1]["status"], "active")
        self.assertEqual(row["last_add_risk_brake_pair"], "10:11")
        self.assertEqual(row["add_risk_brakes"][-1]["cancelled_oid"], 1)

    def test_add_risk_brake_does_not_repeat_same_latest_pair(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.cancelled = []

            def bulk_cancel(self, requests):
                self.cancelled.extend(requests)
                return {"status": "ok"}

        row = {
            "last_add_risk_brake_pair": "10:11",
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99", "size": "1", "is_buy": True},
                {"side": "buy", "status": "filled", "oid": 10, "is_buy": True, "fill": {"time": 1000, "dir": "Open Long", "oid": 10}},
                {"side": "buy", "status": "filled", "oid": 11, "is_buy": True, "fill": {"time": 2000, "dir": "Open Long", "oid": 11}},
            ],
        }
        exchange = FakeExchange()

        cancelled = apply_grid_add_risk_brake(exchange, "BTC", row, row["levels"][-1:], Decimal("1"), 123)

        self.assertEqual(cancelled, 0)
        self.assertEqual(exchange.cancelled, [])
        self.assertEqual(row["levels"][0]["status"], "active")

    def test_add_risk_brake_resets_on_reducing_fill(self) -> None:
        class FakeExchange:
            def bulk_cancel(self, requests):
                raise AssertionError("should not cancel on interrupted streak")

        row = {
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99", "size": "1", "is_buy": True},
                {"side": "buy", "status": "filled", "oid": 10, "is_buy": True, "fill": {"time": 1000, "dir": "Open Long", "oid": 10}},
                {"side": "sell", "status": "filled", "oid": 11, "is_buy": False, "fill": {"time": 2000, "dir": "Close Long", "oid": 11}},
                {"side": "buy", "status": "filled", "oid": 12, "is_buy": True, "fill": {"time": 3000, "dir": "Open Long", "oid": 12}},
            ]
        }

        cancelled = apply_grid_add_risk_brake(FakeExchange(), "BTC", row, row["levels"][-1:], Decimal("1"), 123)

        self.assertEqual(cancelled, 0)
        self.assertEqual(row["levels"][0]["status"], "active")
        self.assertNotIn("last_add_risk_brake_pair", row)

    def test_add_risk_brake_cancel_failure_does_not_raise(self) -> None:
        class FakeExchange:
            def bulk_cancel(self, requests):
                return {"status": "err", "response": "order was already filled"}

        row = {
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99", "size": "1", "is_buy": True},
                {"side": "buy", "status": "filled", "oid": 10, "is_buy": True, "fill": {"time": 1000, "dir": "Open Long", "oid": 10}},
                {"side": "buy", "status": "filled", "oid": 11, "is_buy": True, "fill": {"time": 2000, "dir": "Open Long", "oid": 11}},
            ]
        }

        cancelled = apply_grid_add_risk_brake(FakeExchange(), "BTC", row, row["levels"][-1:], Decimal("1"), 123)

        self.assertEqual(cancelled, 0)
        self.assertEqual(row["levels"][0]["status"], "active")
        self.assertEqual(row["levels"][0]["brake_cancel_failed_at"], 123)
        self.assertEqual(row["last_add_risk_brake_pair"], "10:11")
        self.assertEqual(row["add_risk_brakes"][-1]["status"], "cancel_failed")
        self.assertIn("order was already filled", row["add_risk_brakes"][-1]["error"])

    def test_add_risk_brake_state_prunes_old_pair_and_history(self) -> None:
        row = {
            "last_add_risk_brake_pair": "10:11",
            "last_add_risk_brake_at": 100,
            "add_risk_streak": {"side": "buy", "count": 1},
            "add_risk_brakes": [
                {"at": 100, "status": "cancelled"},
                {"at": 604900, "status": "cancelled"},
            ],
        }

        changed = prune_add_risk_brake_state(row, 604901)

        self.assertTrue(changed)
        self.assertNotIn("last_add_risk_brake_pair", row)
        self.assertNotIn("last_add_risk_brake_at", row)
        self.assertNotIn("add_risk_streak", row)
        self.assertEqual(row["add_risk_brakes"], [{"at": 604900, "status": "cancelled"}])

    def test_stale_recovery_skips_buy_that_would_cross_current_ask(self) -> None:
        entry = {
            "side": "buy",
            "status": "recovery_deferred",
            "oid": 123,
            "price": "61639",
            "limit_px": "61639",
        }

        skipped = skip_stale_grid_recovery(
            entry,
            123,
            1000,
            Decimal("60794.5"),
            Decimal("60794"),
            Decimal("60795"),
        )

        self.assertTrue(skipped)
        self.assertEqual(entry["status"], "skipped_stale_recovery")
        self.assertIsNone(entry["oid"])
        self.assertEqual(entry["stale_recovery_oid"], 123)
        self.assertEqual(entry["stale_recovery_price"], "61639")
        self.assertEqual(entry["stale_recovery_best_ask"], "60795")

    def test_stale_recovery_skips_sell_that_would_cross_current_bid(self) -> None:
        entry = {"side": "sell", "status": "recovery_deferred", "oid": 456, "price": "99"}

        skipped = skip_stale_grid_recovery(
            entry,
            456,
            1000,
            Decimal("100.5"),
            Decimal("100"),
            Decimal("101"),
        )

        self.assertTrue(skipped)
        self.assertEqual(entry["status"], "skipped_stale_recovery")
        self.assertIsNone(entry["oid"])
        self.assertEqual(entry["stale_recovery_best_bid"], "100")

    def test_stale_recovery_allows_resting_price(self) -> None:
        entry = {"side": "buy", "status": "recovery_deferred", "oid": 789, "price": "99"}

        skipped = skip_stale_grid_recovery(
            entry,
            789,
            1000,
            Decimal("100.5"),
            Decimal("100"),
            Decimal("101"),
        )

        self.assertFalse(skipped)
        self.assertEqual(entry["status"], "recovery_deferred")
        self.assertEqual(entry["oid"], 789)

    def test_paused_restore_deferred_when_price_would_cross_market(self) -> None:
        entry = {"side": "buy", "status": "paused_replacement", "oid": None, "price": "101"}

        deferred = defer_paused_grid_restore_if_crossing(
            entry,
            123,
            Decimal("100"),
            Decimal("99"),
            Decimal("100.5"),
        )

        self.assertTrue(deferred)
        self.assertEqual(entry["status"], "paused_replacement")
        self.assertEqual(entry["restore_deferred_reason"], "would_cross_market")
        self.assertEqual(entry["restore_deferred_price"], "101")
        self.assertEqual(entry["restore_deferred_best_ask"], "100.5")

    def test_paused_restore_not_deferred_when_price_would_rest(self) -> None:
        entry = {"side": "buy", "status": "paused_replacement", "oid": None, "price": "99"}

        deferred = defer_paused_grid_restore_if_crossing(
            entry,
            123,
            Decimal("100"),
            Decimal("99"),
            Decimal("100.5"),
        )

        self.assertFalse(deferred)
        self.assertNotIn("restore_deferred_reason", entry)

    def test_unknown_oid_recovery_skips_old_missing_order(self) -> None:
        entry = {
            "side": "buy",
            "status": "recovery_deferred",
            "oid": 123,
            "price": "99",
            "submitted_at": 1000,
        }

        skipped = skip_unknown_oid_grid_recovery(entry, 123, 1000 + 31 * 60, {"status": "unknownOid"})

        self.assertTrue(skipped)
        self.assertEqual(entry["status"], "skipped_unknown_oid")
        self.assertIsNone(entry["oid"])
        self.assertEqual(entry["unknown_oid"], 123)
        self.assertEqual(entry["unknown_oid_age_seconds"], 31 * 60)

    def test_unknown_oid_recovery_allows_fresh_missing_order(self) -> None:
        entry = {
            "side": "buy",
            "status": "recovery_deferred",
            "oid": 123,
            "price": "99",
            "submitted_at": 1000,
        }

        skipped = skip_unknown_oid_grid_recovery(entry, 123, 1000 + 5 * 60, {"status": "unknownOid"})

        self.assertFalse(skipped)
        self.assertEqual(entry["status"], "recovery_deferred")
        self.assertEqual(entry["oid"], 123)

    def test_unknown_oid_recovery_ignores_known_exchange_status(self) -> None:
        entry = {
            "side": "buy",
            "status": "recovery_deferred",
            "oid": 123,
            "price": "99",
            "submitted_at": 1000,
        }

        skipped = skip_unknown_oid_grid_recovery(
            entry,
            123,
            1000 + 31 * 60,
            {"order": {"status": "open"}},
        )

        self.assertFalse(skipped)
        self.assertEqual(entry["status"], "recovery_deferred")
        self.assertEqual(entry["oid"], 123)

    def test_margin_gap_multiplier_starts_at_ninety_and_rises_toward_hard_stop(self) -> None:
        self.assertEqual(grid_margin_gap_multiplier(None), Decimal("1"))
        self.assertEqual(grid_margin_gap_multiplier(Decimal("0.90")), Decimal("1"))
        self.assertEqual(grid_margin_gap_multiplier(Decimal("0.70")), Decimal("1"))
        self.assertEqual(grid_margin_gap_multiplier(Decimal("0.80")).quantize(Decimal("0.001")), Decimal("1.693"))

    def test_margin_gap_multiplier_only_widens_add_risk_far_topup(self) -> None:
        row = {
            "gap_rate": "0.01",
            "min_order_value": "1",
            "sz_decimals": 3,
            "base_buy_size": "0.100",
            "base_sell_size": "0.100",
            "topup_buy_size": "0.100",
            "topup_sell_size": "0.100",
            "topup_buy_gap": "0.01",
            "topup_sell_gap": "0.01",
            "margin_gap_multiplier": "2",
            "levels": [
                {
                    "side": "buy",
                    "status": "active",
                    "oid": 1,
                    "is_buy": True,
                    "price": "89",
                    "size": "0.100",
                },
                {
                    "side": "sell",
                    "status": "active",
                    "oid": 2,
                    "is_buy": False,
                    "price": "110",
                    "size": "0.100",
                },
            ],
        }
        asset = {"szDecimals": 3}

        add_risk_topup = next_depth_order(
            row,
            "BTC",
            asset,
            "buy",
            Decimal("100"),
            Decimal("1"),
            Decimal("100"),
            Decimal("250"),
            "abs",
        )
        self.assertIsNotNone(add_risk_topup)
        self.assertEqual(add_risk_topup["price"], "87.22")
        self.assertEqual(add_risk_topup["plan"]["grid_gap"], Decimal("0.02"))

        reduce_risk_topup = next_depth_order(
            row,
            "BTC",
            asset,
            "sell",
            Decimal("100"),
            Decimal("1"),
            Decimal("100"),
            Decimal("250"),
            "abs",
        )
        self.assertIsNotNone(reduce_risk_topup)
        self.assertEqual(reduce_risk_topup["price"], "111.1")
        self.assertEqual(reduce_risk_topup["plan"]["grid_gap"], Decimal("0.01"))

    def test_risk_density_pauses_logarithmically_across_add_risk_orders(self) -> None:
        row = {
            "gap_rate": "0.01",
            "avg_multiplier": "2",
            "avg_favored_side": "sell",
            "levels": [
                {
                    "side": "buy",
                    "status": "active",
                    "oid": oid,
                    "is_buy": True,
                    "price": str(100 - oid),
                    "size": "1",
                }
                for oid in range(20)
            ],
        }

        candidates, allowed, multiplier = grid_risk_density_pause_candidates(
            row,
            "buy",
            Decimal("1"),
            10,
            Decimal("1"),
        )

        self.assertEqual(allowed, 5)
        self.assertEqual(multiplier, Decimal("2"))
        paused_oids = [entry["oid"] for entry in candidates]
        self.assertEqual(paused_oids, [2, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18])

    def test_active_cap_pauses_logarithmically_beyond_thirty_two_active_orders(self) -> None:
        row = {
            "levels": [
                {
                    "side": "sell",
                    "status": "active",
                    "oid": oid,
                    "is_buy": False,
                    "price": str(100 + oid),
                    "size": "1",
                }
                for oid in range(40)
            ],
        }

        candidates, allowed = grid_active_cap_pause_candidates(row, "sell")

        self.assertEqual(allowed, 32)
        self.assertEqual([entry["oid"] for entry in candidates], [29, 30, 32, 33, 34, 36, 37, 38])

    def test_active_cap_keeps_replacement_before_regular_orders(self) -> None:
        row = {
            "levels": [
                {
                    "side": "sell",
                    "status": "active",
                    "oid": oid,
                    "is_buy": False,
                    "price": str(100 + oid),
                    "size": "1",
                }
                for oid in range(32)
            ]
        }
        row["levels"].append(
            {
                "side": "sell",
                "status": "active",
                "oid": 999,
                "is_buy": False,
                "price": "200",
                "size": "1",
                "replacement_order": True,
            }
        )

        candidates, allowed = grid_active_cap_pause_candidates(row, "sell")

        self.assertEqual(allowed, 32)
        self.assertEqual([entry["oid"] for entry in candidates], [30])

    def test_risk_density_restore_uses_logarithmic_distribution(self) -> None:
        row = {
            "gap_rate": "0.01",
            "avg_multiplier": "2",
            "avg_favored_side": "sell",
            "levels": [
                {
                    "side": "buy",
                    "status": "active" if oid in {0, 1, 3} else "paused_risk_density",
                    "oid": oid if oid in {0, 1, 3} else None,
                    "is_buy": True,
                    "price": str(100 - oid),
                    "size": "1",
                }
                for oid in range(20)
            ],
        }
        should_wait = row["levels"][2]
        should_restore = row["levels"][8]

        self.assertFalse(
            grid_risk_density_restore_allowed(row, should_wait, "buy", Decimal("1"), 10, Decimal("1"))
        )
        self.assertTrue(
            grid_risk_density_restore_allowed(row, should_restore, "buy", Decimal("1"), 10, Decimal("1"))
        )

    def test_active_cap_restore_uses_logarithmic_distribution(self) -> None:
        keep_active = set(range(29)) | {31, 35}
        row = {
            "levels": [
                {
                    "side": "sell",
                    "status": "active" if oid in keep_active else "paused_active_cap",
                    "oid": oid if oid in keep_active else None,
                    "is_buy": False,
                    "price": str(100 + oid),
                    "size": "1",
                }
                for oid in range(40)
            ],
        }
        should_wait = row["levels"][29]
        should_restore = row["levels"][39]

        self.assertFalse(grid_active_cap_restore_allowed(row, should_wait, "sell"))
        self.assertTrue(grid_active_cap_restore_allowed(row, should_restore, "sell"))

    def test_side_cap_clear_removes_paused_before_active(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.cancelled = []

            def bulk_cancel(self, requests):
                self.cancelled.extend(requests)
                return {"status": "ok"}

        row = {
            "levels": [
                {"side": "buy", "status": "active", "oid": oid, "price": str(100 - oid), "size": "1"}
                for oid in range(1024)
            ]
        }
        row["levels"].append({"side": "buy", "status": "paused_risk_density", "oid": None, "price": "1", "size": "1"})
        exchange = FakeExchange()

        cleared = clear_grid_side_cap_entries(exchange, "BTC", row, 123)

        self.assertEqual(cleared, 1)
        self.assertEqual(len([entry for entry in row["levels"] if entry["side"] == "buy"]), 1024)
        self.assertEqual(exchange.cancelled, [])
        self.assertNotIn("paused_risk_density", {entry["status"] for entry in row["levels"]})

    def test_side_cap_clear_cancels_active_overflow(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.cancelled = []

            def bulk_cancel(self, requests):
                self.cancelled.extend(requests)
                return {"status": "ok"}

        row = {
            "levels": [
                {
                    "side": "sell",
                    "status": "active",
                    "oid": oid,
                    "price": str(100 + oid),
                    "size": "1",
                    "submitted_at": oid,
                }
                for oid in range(1025)
            ]
        }
        exchange = FakeExchange()

        cleared = clear_grid_side_cap_entries(exchange, "BTC", row, 123)

        self.assertEqual(cleared, 1)
        self.assertEqual(exchange.cancelled, [{"coin": "BTC", "oid": 0}])
        self.assertEqual(len([entry for entry in row["levels"] if entry["side"] == "sell"]), 1024)
        self.assertNotIn(0, {entry["oid"] for entry in row["levels"]})

    def test_side_cap_clear_ignores_history_records(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.cancelled = []

            def bulk_cancel(self, requests):
                self.cancelled.extend(requests)
                return {"status": "ok"}

        row = {
            "levels": [
                {"side": "buy", "status": "active", "oid": oid, "price": str(100 - oid), "size": "1"}
                for oid in range(1024)
            ]
        }
        row["levels"].extend(
            {"side": "buy", "status": "filled", "oid": 1000 + oid, "price": str(50 - oid), "size": "1"}
            for oid in range(20)
        )
        exchange = FakeExchange()

        cleared = clear_grid_side_cap_entries(exchange, "BTC", row, 123)

        self.assertEqual(cleared, 0)
        self.assertEqual(len([entry for entry in row["levels"] if entry["side"] == "buy"]), 1044)
        self.assertEqual(exchange.cancelled, [])

    def test_far_topup_inserts_between_active_gap_using_target_gap(self) -> None:
        row = {
            "gap_rate": "0.01",
            "min_order_value": "1",
            "sz_decimals": 3,
            "base_buy_size": "0.100",
            "base_sell_size": "0.100",
            "topup_buy_size": "0.100",
            "topup_sell_size": "0.100",
            "topup_buy_gap": "0.01",
            "topup_sell_gap": "0.02",
            "levels": [
                {"side": "sell", "status": "active", "oid": 1, "is_buy": False, "price": "101", "size": "0.100"},
                {"side": "sell", "status": "active", "oid": 2, "is_buy": False, "price": "106", "size": "0.100"},
            ],
        }
        asset = {"szDecimals": 3}

        topup = next_depth_order(
            row,
            "BTC",
            asset,
            "sell",
            Decimal("100"),
            Decimal("-1"),
            Decimal("100"),
            Decimal("250"),
            "abs",
        )

        self.assertIsNotNone(topup)
        self.assertEqual(topup["price"], "103.5")
        self.assertEqual(topup["plan"]["grid_gap"], Decimal("0.02"))

    def test_isolated_asset_detection(self) -> None:
        self.assertTrue(asset_requires_isolated_margin({"onlyIsolated": True}))
        self.assertTrue(asset_requires_isolated_margin({"marginMode": "noCross"}))
        self.assertFalse(asset_requires_isolated_margin({"maxLeverage": 40}))

    def test_flat_isolated_grid_sets_capped_leverage_once_before_orders(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.calls = []
                self.next_oid = 1

            def update_leverage(self, leverage: int, coin: str, is_cross: bool) -> dict:
                self.calls.append(("leverage", leverage, coin, is_cross))
                return {"status": "ok"}

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.calls.append(("order", coin, is_buy, reduce_only))
                oid = self.next_oid
                self.next_oid += 1
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}

        exchange = FakeExchange()
        asset = {
            "szDecimals": 2,
            "maxLeverage": 20,
            "onlyIsolated": True,
            "marginMode": "noCross",
        }
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [],
        }
        ready = set()
        buy = grid_order_entry(row, "xyz:SPCX", asset, True, Decimal("100"), False)
        sell = grid_order_entry(row, "xyz:SPCX", asset, False, Decimal("101"), False)

        for order in (buy, sell):
            self.assertTrue(
                submit_grid_order_entry(
                    exchange,
                    "xyz:SPCX",
                    order,
                    1,
                    row,
                    asset,
                    Decimal("0"),
                    Decimal("0"),
                    "abs",
                    False,
                    ready,
                )
            )

        self.assertEqual(exchange.calls[0], ("leverage", 5, "xyz:SPCX", False))
        self.assertEqual(sum(call[0] == "leverage" for call in exchange.calls), 1)
        self.assertEqual(sum(call[0] == "order" for call in exchange.calls), 2)

    def test_grid_submit_keeps_alo_order_at_one_gap_from_active_order(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 123}}]}}}

        exchange = FakeExchange()
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99", "size": "1"},
            ],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False)

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            1,
            row,
            asset,
            Decimal("0"),
            Decimal("0"),
            "abs",
            False,
            set(),
        )

        self.assertTrue(submitted)
        self.assertEqual(order["price"], "100")
        self.assertEqual(exchange.orders[0][2], Decimal("100.0"))
        self.assertEqual(order["plan"]["order_type"], {"limit": {"tif": "Alo"}})

    def test_grid_order_entry_backfills_base_size_for_legacy_row(self) -> None:
        row = {
            "gap_rate": "0.0002",
            "buy_size": "0.07",
            "sell_size": "0.07",
            "min_order_value": "10",
        }

        order = grid_order_entry(row, "xyz:JPY", {"szDecimals": 2}, True, Decimal("161.05"), False)

        self.assertEqual(row["base_buy_size"], "0.07")
        self.assertEqual(row["base_sell_size"], "0.07")
        self.assertEqual(order["size"], "0.07")

    def test_grid_order_entry_infers_legacy_base_size_from_levels(self) -> None:
        row = {
            "gap_rate": "0.0002",
            "min_order_value": "10",
            "levels": [
                {"side": "buy", "size": "0.07"},
                {"side": "sell", "size": "0.08"},
            ],
        }

        order = grid_order_entry(row, "xyz:JPY", {"szDecimals": 2}, False, Decimal("161.05"), False)

        self.assertEqual(row["base_buy_size"], "0.07")
        self.assertEqual(row["base_sell_size"], "0.08")
        self.assertEqual(row["buy_size"], "0.07")
        self.assertEqual(row["sell_size"], "0.08")
        self.assertEqual(order["size"], "0.08")

    def test_grid_submit_does_not_move_non_replacement_before_alo_reject(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 123}}]}}}

        exchange = FakeExchange()
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99.1", "size": "1"},
            ],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False)

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            1,
            row,
            asset,
            Decimal("0"),
            Decimal("0"),
            "abs",
            False,
            set(),
        )

        self.assertTrue(submitted)
        self.assertEqual(order["price"], "100")
        self.assertEqual(exchange.orders[0][2], Decimal("100.0"))
        self.assertEqual(order["plan"]["order_type"], {"limit": {"tif": "Alo"}})

    def test_non_replacement_alo_reject_does_not_retry_price_search(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                return {"status": "ok", "response": {"data": {"statuses": [{"error": "Post only would immediately match"}]}}}

        exchange = FakeExchange()
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99.1", "size": "1"},
                {"side": "buy", "status": "active", "oid": 2, "price": "97", "size": "1"},
            ],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False)

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            1,
            row,
            asset,
            Decimal("0"),
            Decimal("0"),
            "abs",
            False,
            set(),
        )

        self.assertFalse(submitted)
        self.assertEqual(order["status"], "skipped_post_only")
        self.assertEqual(order["price"], "100")
        self.assertEqual(len(exchange.orders), 1)

    def test_failed_replacement_order_is_preserved_for_retry(self) -> None:
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = replacement_order_from_fill(
            row,
            "BTC",
            asset,
            Decimal("100"),
            True,
            Decimal("1"),
            Decimal("100"),
            Decimal("200"),
            "abs",
        )
        self.assertIsNotNone(order)
        order["status"] = "skipped_account_margin"
        order["skipped_at"] = 1

        preserve_replacement_order(row["levels"], order, 2)

        self.assertEqual(row["levels"], [order])
        self.assertTrue(order["replacement_order"])
        self.assertEqual(order["status"], "paused_replacement")
        self.assertEqual(order["replacement_pause_reason"], "skipped_account_margin")
        self.assertEqual(order["paused_at"], 2)

    def test_paused_replacement_survives_prune_when_side_is_full(self) -> None:
        row = {
            "type": "grid",
            "target_orders_per_side": 1,
            "gap_rate": "0.01",
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99", "size": "1"},
                {
                    "side": "buy",
                    "status": "paused_replacement",
                    "oid": None,
                    "price": "98",
                    "size": "1",
                    "reduce_only": True,
                    "replacement_order": True,
                    "paused_at": 1,
                },
                {
                    "side": "buy",
                    "status": "paused_limit",
                    "oid": None,
                    "price": "97",
                    "size": "1",
                    "reduce_only": True,
                    "paused_at": 1,
                },
            ],
        }

        changed = prune_grid_levels(row)

        self.assertTrue(changed)
        self.assertEqual([entry["price"] for entry in row["levels"]], ["99", "98"])

    def test_paused_replacement_does_not_count_as_active_oid(self) -> None:
        row = {
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99", "size": "1"},
                {
                    "side": "buy",
                    "status": "paused_replacement",
                    "oid": 2,
                    "price": "98",
                    "size": "1",
                    "replacement_order": True,
                },
            ]
        }

        self.assertEqual(active_grid_oids(row, "buy"), {1})

    def test_active_grid_orders_above_target_are_not_trimmed(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.cancel_requests = []

            def bulk_cancel(self, requests):
                self.cancel_requests.extend(requests)
                return {"status": "ok"}

        row = {
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99", "size": "1"},
                {"side": "buy", "status": "active", "oid": 2, "price": "98", "size": "1"},
            ],
        }
        exchange = FakeExchange()

        trimmed = trim_excess_grid_entries(exchange, "BTC", row, 1, 123)

        self.assertEqual(trimmed, 0)
        self.assertEqual(exchange.cancel_requests, [])
        self.assertEqual([entry["status"] for entry in row["levels"]], ["active", "active"])

    def test_dense_grid_uses_order_plan_gap_before_row_gap(self) -> None:
        row = {
            "gap_rate": "0.05",
            "levels": [
                {
                    "side": "buy",
                    "status": "active",
                    "oid": 1,
                    "price": "100",
                    "size": "1",
                    "plan": {"grid_gap": Decimal("0.01")},
                },
                {
                    "side": "buy",
                    "status": "active",
                    "oid": 2,
                    "price": "98",
                    "size": "1",
                    "plan": {"grid_gap": Decimal("0.01")},
                },
            ],
        }

        dense = dense_grid_entries(row)

        self.assertEqual(dense, [])

    def test_dense_grid_regrids_farther_instead_of_dedup_cancel(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.cancel_requests = []
                self.orders = []

            def bulk_cancel(self, requests):
                self.cancel_requests.extend(requests)
                return {"status": "ok"}

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 22}}]}}}

        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        keep = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False)
        keep["status"] = "active"
        keep["oid"] = 1
        dense = grid_order_entry(row, "BTC", asset, True, Decimal("99.5"), False)
        dense["status"] = "active"
        dense["oid"] = 2
        row["levels"] = [keep, dense]
        exchange = FakeExchange()

        regridded = regrid_dense_entries(
            exchange,
            "BTC",
            row,
            asset,
            123,
            Decimal("0"),
            Decimal("0"),
            "abs",
            False,
            set(),
        )

        self.assertEqual(regridded, 1)
        self.assertEqual(exchange.cancel_requests, [{"coin": "BTC", "oid": 2}])
        self.assertEqual(dense["status"], "active")
        self.assertEqual(dense["oid"], 22)
        self.assertEqual(dense["dense_regrid_from_oid"], 2)
        self.assertEqual(dense["dense_regrid_from_price"], "99.5")
        self.assertLess(Decimal(dense["price"]), Decimal("99.5"))
        self.assertNotIn("dedup_dense", {entry["status"] for entry in row["levels"]})

    def test_grid_order_moves_away_from_near_active_price_before_submit(self) -> None:
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99.1", "size": "1"},
                {"side": "buy", "status": "active", "oid": 2, "price": "97", "size": "1"},
            ],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False)

        moved = move_grid_order_away_from_active(row, asset, order)

        self.assertTrue(moved)
        self.assertEqual(order["price"], "98.05")

    def test_grid_order_moves_away_from_paused_replacement_price(self) -> None:
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99.1", "size": "1"},
                {
                    "side": "buy",
                    "status": "paused_replacement",
                    "oid": None,
                    "price": "98.05",
                    "size": "1",
                    "replacement_order": True,
                },
                {"side": "buy", "status": "active", "oid": 2, "price": "97", "size": "1"},
            ],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False)

        moved = move_grid_order_away_from_active(row, asset, order)

        self.assertTrue(moved)
        self.assertEqual(order["price"], "96.06")

    def test_replacement_alo_reject_inserts_between_wide_active_gap_before_moving_farther(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                if len(self.orders) == 1:
                    return {"status": "ok", "response": {"data": {"statuses": [{"error": "Post only would immediately match"}]}}}
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 124}}]}}}

        exchange = FakeExchange()
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99.1", "size": "1"},
                {"side": "buy", "status": "active", "oid": 2, "price": "97", "size": "1"},
            ],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False)

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            1,
            row,
            asset,
            Decimal("0"),
            Decimal("0"),
            "abs",
            False,
            set(),
            True,
        )

        self.assertTrue(submitted)
        self.assertEqual(order["price"], "95.138")
        self.assertEqual([call[2] for call in exchange.orders], [Decimal("98.05"), Decimal("95.138")])
        self.assertEqual([call[3] for call in exchange.orders], [{"limit": {"tif": "Alo"}}, {"limit": {"tif": "Gtc"}}])
        self.assertEqual(order["plan"]["order_type"], {"limit": {"tif": "Gtc"}})
        self.assertEqual(order["alo_rejects"], 1)

    def test_replacement_alo_reject_uses_order_target_gap_for_wide_gap(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                if len(self.orders) == 1:
                    return {"status": "ok", "response": {"data": {"statuses": [{"error": "Post only would immediately match"}]}}}
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 124}}]}}}

        exchange = FakeExchange()
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "99.1", "size": "1"},
                {"side": "buy", "status": "active", "oid": 2, "price": "97", "size": "1"},
            ],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False, gap=Decimal("0.02"))

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            1,
            row,
            asset,
            Decimal("0"),
            Decimal("0"),
            "abs",
            False,
            set(),
            True,
        )

        self.assertTrue(submitted)
        self.assertEqual(order["price"], "96.04")
        self.assertEqual([call[2] for call in exchange.orders], [Decimal("98.0"), Decimal("96.04")])
        self.assertEqual([call[3] for call in exchange.orders], [{"limit": {"tif": "Alo"}}, {"limit": {"tif": "Gtc"}}])
        self.assertEqual(order["plan"]["order_type"], {"limit": {"tif": "Gtc"}})
        self.assertEqual(order["alo_rejects"], 1)

    def test_replacement_alo_reject_retries_one_gap_farther(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.prices = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.prices.append(Decimal(str(limit_px)))
                if len(self.prices) == 1:
                    return {"status": "ok", "response": {"data": {"statuses": [{"error": "Post only would immediately match"}]}}}
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 456}}]}}}

        exchange = FakeExchange()
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, False, Decimal("100"), False)

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            1,
            row,
            asset,
            Decimal("0"),
            Decimal("0"),
            "abs",
            False,
            set(),
            True,
        )

        self.assertTrue(submitted)
        self.assertEqual(exchange.prices, [Decimal("100.0"), Decimal("101.0")])
        self.assertEqual(order["price"], "101")
        self.assertEqual(order["alo_rejects"], 1)

    def test_replacement_gtc_retry_keeps_spacing_from_paused_orders(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((Decimal(str(limit_px)), order_type))
                if len(self.orders) == 1:
                    return {"status": "ok", "response": {"data": {"statuses": [{"error": "Post only would immediately match"}]}}}
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 456}}]}}}

        exchange = FakeExchange()
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [
                {
                    "side": "buy",
                    "status": "paused_replacement",
                    "oid": None,
                    "price": "99",
                    "size": "1",
                    "replacement_order": True,
                },
            ],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False)

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            1,
            row,
            asset,
            Decimal("0"),
            Decimal("0"),
            "abs",
            False,
            set(),
            True,
        )

        self.assertTrue(submitted)
        self.assertEqual(exchange.orders, [(Decimal("100.0"), {"limit": {"tif": "Alo"}}), (Decimal("98.01"), {"limit": {"tif": "Gtc"}})])
        self.assertEqual(order["price"], "98.01")
        self.assertEqual(order["alo_rejects"], 1)

    def test_replacement_alo_reject_moves_outward_before_inserting_when_not_too_close(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.prices = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.prices.append(Decimal(str(limit_px)))
                if len(self.prices) == 1:
                    return {"status": "ok", "response": {"data": {"statuses": [{"error": "Post only would immediately match"}]}}}
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 789}}]}}}

        exchange = FakeExchange()
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "98", "size": "1"},
                {"side": "buy", "status": "active", "oid": 2, "price": "95", "size": "1"},
            ],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False)

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            1,
            row,
            asset,
            Decimal("0"),
            Decimal("0"),
            "abs",
            False,
            set(),
            True,
        )

        self.assertTrue(submitted)
        self.assertEqual(exchange.prices, [Decimal("100.0"), Decimal("99.0")])
        self.assertEqual(order["price"], "99")
        self.assertEqual(order["alo_rejects"], 1)

    def test_refresh_strategy_params_keeps_existing_levels(self) -> None:
        levels = [
            {"side": "buy", "status": "active", "oid": 1, "price": "90", "size": "0.1"},
            {"side": "sell", "status": "active", "oid": 2, "price": "110", "size": "0.1"},
        ]
        row = {
            "position_limit_mode": "abs",
            "min_position_value": "0",
            "max_position_value": "250",
            "avg": "0",
            "trend": "0",
            "gap_rate": "0.01",
            "min_order_value": "10",
            "sz_decimals": 3,
            "levels": levels,
        }
        refresh_grid_row_strategy_params(
            row,
            {"szDecimals": 3},
            Decimal("100"),
            Decimal("-1"),
            Decimal("125"),
        )
        self.assertIs(row["levels"], levels)
        self.assertEqual(row["levels"][0]["oid"], 1)
        self.assertEqual(row["avg_multiplier"], "1.62")
        self.assertEqual(row["topup_buy_gap"], "0.01")
        self.assertEqual(row["topup_sell_gap"], "0.0162")
        self.assertEqual(row["topup_buy_size"], row["base_buy_size"])
        self.assertEqual(row["topup_sell_size"], row["base_sell_size"])

    def test_topup_params_adjust_only_risk_gap(self) -> None:
        buy_favored = grid_avg_topup_params(
            Decimal("0.01"),
            Decimal("0.100"),
            Decimal("0.100"),
            Decimal("1.4"),
            "buy",
            3,
        )
        sell_favored = grid_avg_topup_params(
            Decimal("0.01"),
            Decimal("0.100"),
            Decimal("0.100"),
            Decimal("1.4"),
            "sell",
            3,
        )
        self.assertEqual(buy_favored, (Decimal("0.100"), Decimal("0.100"), Decimal("0.01"), Decimal("0.014")))
        self.assertEqual(sell_favored, (Decimal("0.100"), Decimal("0.100"), Decimal("0.014"), Decimal("0.01")))

    def test_query_summary_displays_live_avg_values(self) -> None:
        row = {
            "position_limit_mode": "abs",
            "min_position_value": "0",
            "max_position_value": "250",
            "avg": "0",
            "gap": "0.05%",
            "gap_rate": "0.0005",
            "base_buy_size": "0.00016",
            "base_sell_size": "0.00016",
            "sz_decimals": 5,
        }
        summary = dict(
            grid_query_avg_summary(
                row,
                {"szDecimals": 5},
                Decimal("-0.002"),
                Decimal("125"),
            )
        )
        self.assertEqual(summary["avg"], "0")
        self.assertEqual(summary["avg_position"], "-125")
        self.assertEqual(summary["avg_multiplier"], "1.62")
        self.assertEqual(summary["avg_side"], "buy")
        self.assertEqual(summary["base_gap"], "0.05% (0.0005)")
        self.assertEqual(summary["topup_gap"], "buy 0.0005 / sell 0.00081")
        self.assertEqual(summary["base_size"], "buy 0.00016 / sell 0.00016")
        self.assertEqual(summary["topup_size"], "buy 0.00016 / sell 0.00016")

    def test_grid_plan_persists_base_and_effective_values(self) -> None:
        class FakeInfo:
            def user_state(self, account: str, dex: str = "") -> dict:
                return {
                    "assetPositions": [
                        {"position": {"coin": "BTC", "szi": "1.5", "positionValue": "150"}}
                    ]
                }

        args = Namespace(
            trend=None,
            gap=["0.5%"],
            grid_min="10",
            grid_position_limit_mode="long",
            grid_position_min_value="100",
            grid_position_limit_value="400",
            grid_avg="200",
            resolved_grid_gap_spec=None,
            network="mainnet",
            coin="BTC",
        )
        plans = build_grid_orders(
            args,
            FakeInfo(),
            "account",
            "",
            None,
            "BTC",
            {"szDecimals": 3},
            Decimal("400"),
            Decimal("100"),
            Decimal("0.05"),
        )
        self.assertEqual(plans[0]["grid_avg_multiplier"], Decimal("1.62"))
        self.assertEqual(plans[0]["grid_base_gap"], Decimal("0.005"))
        self.assertEqual(plans[0]["grid_gap"], Decimal("0.005"))
        self.assertEqual(plans[0]["grid_effective_gap"], Decimal("0.00810"))
        self.assertEqual(plans[0]["grid_buy_size"], Decimal("0.101"))
        self.assertEqual(plans[0]["grid_sell_size"], Decimal("0.101"))
        self.assertEqual(plans[0]["grid_topup_buy_size"], Decimal("0.101"))
        self.assertEqual(plans[0]["grid_topup_sell_size"], Decimal("0.101"))
        self.assertEqual(plans[0]["grid_topup_buy_gap"], Decimal("0.005"))
        self.assertEqual(plans[0]["grid_topup_sell_gap"], Decimal("0.00810"))

        statuses = [{"resting": {"oid": index + 1}} for index in range(len(plans))]
        row = build_grid_batch_row(
            args,
            "account",
            "BTC",
            "",
            {"szDecimals": 3},
            plans,
            statuses,
            Decimal("400"),
            Decimal("0.05"),
        )
        self.assertEqual(row["avg"], "200")
        self.assertEqual(row["gap_rate"], "0.005")
        self.assertEqual(row["effective_gap_rate"], "0.0081")
        self.assertEqual(row["base_buy_size"], "0.101")
        self.assertEqual(row["buy_size"], "0.101")
        self.assertEqual(row["sell_size"], "0.101")
        self.assertEqual(row["topup_buy_size"], "0.101")
        self.assertEqual(row["topup_sell_size"], "0.101")
        self.assertEqual(row["topup_buy_gap"], "0.005")
        self.assertEqual(row["topup_sell_gap"], "0.0081")

    def test_far_topup_and_reverting_replacement_use_avg_skew(self) -> None:
        row = {
            "gap_rate": "0.01",
            "effective_gap_rate": "0.014",
            "min_order_value": "1",
            "sz_decimals": 3,
            "base_buy_size": "0.100",
            "base_sell_size": "0.100",
            "topup_buy_size": "0.140",
            "topup_sell_size": "0.100",
            "topup_buy_gap": "0.01",
            "topup_sell_gap": "0.014",
            "avg_favored_side": "buy",
            "levels": [
                {
                    "side": "buy",
                    "status": "active",
                    "oid": 1,
                    "is_buy": True,
                    "price": "89",
                    "size": "0.100",
                },
                {
                    "side": "sell",
                    "status": "active",
                    "oid": 2,
                    "is_buy": False,
                    "price": "110",
                    "size": "0.100",
                },
            ],
        }
        asset = {"szDecimals": 3}

        topup = next_depth_order(
            row,
            "BTC",
            asset,
            "buy",
            Decimal("100"),
            Decimal("-1"),
            Decimal("100"),
            Decimal("250"),
            "abs",
        )
        self.assertIsNotNone(topup)
        self.assertEqual(topup["size"], "0.1")
        self.assertEqual(topup["plan"]["grid_gap"], Decimal("0.01"))

        sell_topup = next_depth_order(
            row,
            "BTC",
            asset,
            "sell",
            Decimal("100"),
            Decimal("-1"),
            Decimal("100"),
            Decimal("250"),
            "abs",
        )
        self.assertIsNotNone(sell_topup)
        self.assertEqual(sell_topup["size"], "0.1")
        self.assertEqual(sell_topup["plan"]["grid_gap"], Decimal("0.014"))

        replacement = replacement_order_from_fill(
            row,
            "BTC",
            asset,
            Decimal("90"),
            True,
            Decimal("-1"),
            Decimal("100"),
            Decimal("250"),
            "abs",
        )
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement["size"], "0.1")
        self.assertEqual(replacement["plan"]["grid_gap"], Decimal("0.01"))

        reverting_replacement = replacement_order_from_fill(
            row,
            "BTC",
            asset,
            Decimal("110"),
            False,
            Decimal("-1"),
            Decimal("100"),
            Decimal("250"),
            "abs",
        )
        self.assertIsNotNone(reverting_replacement)
        self.assertEqual(reverting_replacement["size"], "0.1")
        self.assertEqual(reverting_replacement["plan"]["grid_gap"], Decimal("0.01"))

        row["levels"][0]["price"] = "69"
        near_orders = near_grid_orders_if_stale(
            row,
            "BTC",
            asset,
            "buy",
            Decimal("100"),
            Decimal("-1"),
            "abs",
        )
        self.assertEqual(len(near_orders), 1)
        self.assertEqual(near_orders[0]["price"], "85")
        self.assertEqual(near_orders[0]["size"], "0.1")
        self.assertEqual(near_orders[0]["plan"]["grid_gap"], Decimal("0.01"))

        row["levels"][1]["price"] = "130"
        add_risk_near_orders = near_grid_orders_if_stale(
            row,
            "BTC",
            asset,
            "sell",
            Decimal("100"),
            Decimal("-1"),
            "abs",
        )
        self.assertEqual(add_risk_near_orders, [])

    def test_long_multiplier_is_asymptotic_to_position_bounds(self) -> None:
        cases = (
            ("50", "1E+9", "buy"),
            ("100", "1E+9", "buy"),
            ("150", "1.62", "buy"),
            ("175", "1.38", "buy"),
            ("200", "1", None),
            ("250", "1.38", "sell"),
            ("300", "1.62", "sell"),
            ("400", "1E+9", "sell"),
            ("500", "1E+9", "sell"),
        )
        for position_value, expected_multiplier, expected_side in cases:
            multiplier, side, current = grid_avg_multiplier(
                "long",
                Decimal("100"),
                Decimal("400"),
                Decimal("200"),
                Decimal("1"),
                Decimal(position_value),
            )
            self.assertEqual(multiplier, Decimal(expected_multiplier))
            self.assertEqual(side, expected_side)
            self.assertEqual(current, Decimal(position_value))

    def test_short_uses_short_inventory_as_positive_policy_value(self) -> None:
        low_multiplier, low_side, low_current = grid_avg_multiplier(
            "short",
            Decimal("100"),
            Decimal("400"),
            Decimal("200"),
            Decimal("-1"),
            Decimal("100"),
        )
        high_multiplier, high_side, high_current = grid_avg_multiplier(
            "short",
            Decimal("100"),
            Decimal("400"),
            Decimal("200"),
            Decimal("-1"),
            Decimal("400"),
        )
        self.assertEqual((low_multiplier, low_side, low_current), (Decimal("1E+9"), "sell", Decimal("100")))
        self.assertEqual((high_multiplier, high_side, high_current), (Decimal("1E+9"), "buy", Decimal("400")))

    def test_abs_bounds_allow_negative_average(self) -> None:
        self.assertEqual(
            grid_avg_bounds("abs", Decimal("0"), Decimal("300")),
            (Decimal("-300"), Decimal("300")),
        )

    def test_avg_size_pair_keeps_base_sizes(self) -> None:
        self.assertEqual(
            grid_avg_size_pair(Decimal("0.002"), Decimal("0.002"), Decimal("1.2"), "buy", 3),
            (Decimal("0.002"), Decimal("0.002")),
        )
        self.assertEqual(
            grid_avg_size_pair(Decimal("0.002"), Decimal("0.002"), Decimal("1.4"), "buy", 3),
            (Decimal("0.002"), Decimal("0.002")),
        )


if __name__ == "__main__":
    unittest.main()
