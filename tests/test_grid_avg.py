import unittest
from argparse import Namespace
from decimal import Decimal
from unittest.mock import patch

from hl_order import (
    asset_requires_isolated_margin,
    build_grid_batch_row,
    build_grid_orders,
    ensure_no_duplicate_grid_batch,
    grid_avg_bounds,
    grid_avg_multiplier,
    grid_avg_size_pair,
    grid_avg_topup_params,
    format_grid_detail_rows,
    grid_query_avg_summary,
    grid_query_rows,
    is_auto_grid_gap,
    refresh_grid_row_strategy_params,
    resolve_grid_spacing,
)
from trail_worker import (
    active_grid_oids,
    build_grid_panic_reduce_order,
    clear_stale_grid_margin_pauses,
    clear_grid_side_cap_entries,
    apply_grid_add_risk_brake,
    dense_grid_entries,
    defer_paused_grid_restore_if_crossing,
    grid_panic_ratio,
    grid_panic_ratio_threshold,
    grid_active_cap_restore_allowed,
    grid_active_cap_pause_candidates,
    grid_margin_gap_multiplier,
    grid_margin_pause_active,
    find_current_position_from_state,
    grid_order_entry,
    grid_replacement_rebalance_pair,
    grid_reduce_only_canceled_restore_without_reduce_only,
    grid_roe_add_risk_allowed,
    grid_roe_pause_candidates,
    grid_roe_restore_allowed,
    grid_risk_density_pause_candidates,
    grid_risk_density_restore_allowed,
    move_grid_order_away_from_active,
    near_grid_orders_if_stale,
    next_depth_order,
    normalize_margin_paused_replacement,
    panic_reversal_order_from_reduce,
    pause_refresh_reduce_only_replacement,
    pause_reduce_only_canceled_entry,
    pause_refreshed_reduce_only_entries,
    pause_grid_margin_side,
    pause_grid_margin_side_entries,
    pause_skipped_account_margin_replacement,
    prune_add_risk_brake_state,
    preserve_replacement_order,
    prune_grid_levels,
    regrid_dense_entries,
    replacement_order_from_fill,
    skip_stale_grid_recovery,
    skip_unknown_oid_grid_recovery,
    submit_grid_order_entry,
    trim_excess_grid_entries,
    GRID_ROE_PAUSE_STATUS,
)


class GridAvgTests(unittest.TestCase):
    def test_grid_gap_zero_requests_default_spacing(self) -> None:
        for value in (["0"], ["0%"], ["0.0%"]):
            args = Namespace(gap=value, resolved_grid_gap_spec=None)
            with patch(
                "hl_order.effective_perp_fee_rates",
                return_value={
                    "taker_effective": Decimal("0.0004"),
                    "maker_effective": Decimal("0.0001"),
                },
            ):
                spacing = resolve_grid_spacing(args, object(), "account", {"szDecimals": 3}, "", Decimal("400"))

            self.assertTrue(is_auto_grid_gap(value))
            self.assertEqual(spacing, Decimal("0.000550"))
            self.assertEqual(
                args.resolved_grid_gap_spec,
                ["0.0550% (minTick 0.0050% + taker 0.0400% + maker 0.0100%)"],
            )

    def test_grid_gap_positive_still_uses_explicit_spacing(self) -> None:
        args = Namespace(gap=["0.3%"], resolved_grid_gap_spec=None)

        spacing = resolve_grid_spacing(args, object(), "account", {"szDecimals": 3}, "", Decimal("400"))

        self.assertFalse(is_auto_grid_gap(args.gap))
        self.assertEqual(spacing, Decimal("0.003"))
        self.assertIsNone(args.resolved_grid_gap_spec)

    def test_duplicate_grid_batch_guard_blocks_active_same_coin(self) -> None:
        rows = [
            {
                "type": "grid",
                "status": "active",
                "network": "mainnet",
                "account": "0xabc",
                "coin": "xyz:SP500",
            },
            {
                "type": "grid",
                "status": "cancelled",
                "network": "mainnet",
                "account": "0xabc",
                "coin": "xyz:SP500",
            },
        ]

        with self.assertRaisesRegex(ValueError, "active grid batch already exists for xyz:SP500"):
            ensure_no_duplicate_grid_batch(rows, "mainnet", "0xabc", "xyz:SP500")

    def test_duplicate_grid_batch_guard_allows_cancelled_same_coin(self) -> None:
        rows = [
            {
                "type": "grid",
                "status": "cancelled",
                "network": "mainnet",
                "account": "0xabc",
                "coin": "xyz:SP500",
            }
        ]

        ensure_no_duplicate_grid_batch(rows, "mainnet", "0xabc", "xyz:SP500")

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

    def test_grid_detail_rows_hide_skipped_account_margin_history(self) -> None:
        row = {
            "levels": [
                {"side": "buy", "status": "skipped_account_margin", "price": "99", "size": "1", "skipped_at": 1},
                {"side": "buy", "status": "paused_account_margin", "price": "98", "size": "1", "paused_at": 2},
                {"side": "buy", "status": "paused_roe", "price": "97", "size": "1", "paused_at": 3},
                {"side": "sell", "status": "active", "oid": 3, "price": "101", "size": "1"},
            ]
        }

        rows = format_grid_detail_rows(row, {3})

        self.assertEqual([item["status"] for item in rows], ["active", "paused_account_margin", "paused_roe"])
        self.assertNotIn("skipped_account_margin", [item["status"] for item in rows])

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

    def test_panic_threshold_migrates_legacy_defaults_only(self) -> None:
        self.assertEqual(grid_panic_ratio_threshold({}), Decimal("70"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "10"}), Decimal("70"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "20"}), Decimal("70"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "30"}), Decimal("70"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "50"}), Decimal("70"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "60"}), Decimal("70"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "65"}), Decimal("70"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "15"}), Decimal("15"))

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

    def test_panic_reversal_after_short_reduce_is_far_sell_with_normal_gap(self) -> None:
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}

        order = panic_reversal_order_from_reduce(
            row,
            "BTC",
            asset,
            Decimal("100"),
            True,
            Decimal("-4"),
            "abs",
        )

        self.assertIsNotNone(order)
        self.assertEqual(order["side"], "sell")
        self.assertEqual(order["price"], "110")
        self.assertFalse(order["reduce_only"])
        self.assertTrue(order["replacement_order"])
        self.assertTrue(order["panic_reversal_order"])
        self.assertEqual(order["plan"]["label"], "grid-panic-reversal")
        self.assertEqual(order["plan"]["grid_gap"], Decimal("0.01"))

    def test_panic_reversal_after_long_reduce_is_far_buy_with_normal_gap(self) -> None:
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}

        order = panic_reversal_order_from_reduce(
            row,
            "BTC",
            asset,
            Decimal("100"),
            False,
            Decimal("4"),
            "abs",
        )

        self.assertIsNotNone(order)
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["price"], "90")
        self.assertFalse(order["reduce_only"])
        self.assertTrue(order["replacement_order"])
        self.assertTrue(order["panic_reversal_order"])
        self.assertEqual(order["plan"]["label"], "grid-panic-reversal")
        self.assertEqual(order["plan"]["grid_gap"], Decimal("0.01"))

    def test_refresh_reduce_only_cancel_becomes_non_reduce_paused_replacement(self) -> None:
        entry = {
            "side": "buy",
            "status": "refresh_reduce_only",
            "oid": 123,
            "is_buy": True,
            "price": "64.214",
            "size": "0.16",
            "reduce_only": True,
            "cancelled_at": 10,
            "plan": {"reduce_only": True},
        }

        paused = pause_refreshed_reduce_only_entries([entry], 10)

        self.assertEqual(paused, 1)
        self.assertEqual(entry["status"], "paused_replacement")
        self.assertIsNone(entry["oid"])
        self.assertTrue(entry["replacement_order"])
        self.assertEqual(entry["replacement_pause_reason"], "refresh_reduce_only")
        self.assertFalse(entry["reduce_only"])
        self.assertFalse(entry["plan"]["reduce_only"])

    def test_old_refresh_reduce_only_replacement_is_migrated_to_paused_replacement(self) -> None:
        entry = {
            "side": "buy",
            "status": "refresh_reduce_only",
            "oid": 123,
            "is_buy": True,
            "price": "64.214",
            "size": "0.16",
            "reduce_only": True,
            "replacement_order": True,
            "plan": {"reduce_only": True},
        }

        migrated = pause_refresh_reduce_only_replacement(entry, 10)

        self.assertTrue(migrated)
        self.assertEqual(entry["status"], "paused_replacement")
        self.assertIsNone(entry["oid"])
        self.assertEqual(entry["replacement_pause_reason"], "refresh_reduce_only")
        self.assertFalse(entry["reduce_only"])
        self.assertFalse(entry["plan"]["reduce_only"])

    def test_regular_refresh_reduce_only_is_not_migrated_to_replacement(self) -> None:
        entry = {
            "side": "buy",
            "status": "refresh_reduce_only",
            "oid": 123,
            "is_buy": True,
            "price": "64.214",
            "size": "0.16",
        }

        migrated = pause_refresh_reduce_only_replacement(entry, 10)

        self.assertFalse(migrated)
        self.assertEqual(entry["status"], "refresh_reduce_only")
        self.assertEqual(entry["oid"], 123)

    def test_paused_replacement_restore_recomputes_reduce_only_from_current_position(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 456}}]}}}

        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False)
        exchange = FakeExchange()

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            1,
            row,
            asset,
            Decimal("-1"),
            Decimal("100"),
            "abs",
            True,
            set(),
        )

        self.assertTrue(submitted)
        self.assertTrue(order["reduce_only"])
        self.assertTrue(order["plan"]["reduce_only"])
        self.assertTrue(exchange.orders[0][4])

    def test_grid_submit_adopts_matching_exchange_open_order(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, size, limit_px, order_type, reduce_only))
                raise AssertionError("duplicate exchange order should not be submitted")

        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, False, Decimal("101"), False)
        open_orders = [
            {
                "coin": "BTC",
                "side": "A",
                "limitPx": "101",
                "sz": "1",
                "oid": 789,
                "reduceOnly": False,
                "timestamp": 123000,
            }
        ]

        submitted = submit_grid_order_entry(
            FakeExchange(),
            "BTC",
            order,
            2,
            row,
            asset,
            Decimal("0"),
            Decimal("0"),
            "abs",
            False,
            set(),
            open_orders=open_orders,
        )

        self.assertTrue(submitted)
        self.assertEqual(order["status"], "active")
        self.assertEqual(order["oid"], 789)
        self.assertEqual(order["submitted_at"], 2)
        self.assertEqual(order["last_submit_status"]["adopted_open_order"]["oid"], 789)

    def test_grid_submit_does_not_adopt_oid_already_tracked_locally(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, size, limit_px, order_type, reduce_only))
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 790}}]}}}

        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [{"side": "sell", "status": "active", "oid": 789, "price": "101"}],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, False, Decimal("101"), False)
        open_orders = [
            {
                "coin": "BTC",
                "side": "A",
                "limitPx": "101",
                "sz": "1",
                "oid": 789,
                "reduceOnly": False,
            }
        ]
        exchange = FakeExchange()

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            2,
            row,
            asset,
            Decimal("0"),
            Decimal("0"),
            "abs",
            False,
            set(),
            open_orders=open_orders,
        )

        self.assertTrue(submitted)
        self.assertEqual(order["oid"], 790)
        self.assertEqual(len(exchange.orders), 1)

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

    def test_reduce_only_canceled_pauses_for_restore(self) -> None:
        entry = {
            "side": "buy",
            "status": "active",
            "oid": 123,
            "price": "99",
            "size": "1",
            "reduce_only": True,
        }

        pause_reduce_only_canceled_entry(entry, 123, 456)

        self.assertEqual(entry["status"], "paused_reduce_capacity")
        self.assertIsNone(entry["oid"])
        self.assertEqual(entry["reduce_only_canceled_oid"], 123)
        self.assertEqual(entry["reduce_only_canceled_at"], 456)
        self.assertEqual(entry["paused_at"], 456)
        self.assertNotIn("skipped_at", entry)

    def test_reduce_only_canceled_restore_submits_without_reduce_only(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 456}}]}}}

        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("99"), True)
        order["status"] = "paused_reduce_capacity"
        order["oid"] = None
        order["reduce_only_canceled_oid"] = 123
        exchange = FakeExchange()

        self.assertTrue(grid_reduce_only_canceled_restore_without_reduce_only(order))
        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            789,
            row,
            asset,
            Decimal("-1"),
            Decimal("100"),
            "abs",
            False,
            set(),
        )

        self.assertTrue(submitted)
        self.assertEqual(order["status"], "active")
        self.assertFalse(order["reduce_only"])
        self.assertFalse(order["plan"]["reduce_only"])
        self.assertFalse(exchange.orders[0][4])
        self.assertEqual(order["reduce_only_canceled_restore_without_reduce_only_at"], 789)

    def test_reduce_only_canceled_restore_margin_reject_does_not_retry_reduce_only(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                return {"status": "ok", "response": {"data": {"statuses": [{"error": "Insufficient margin"}]}}}

        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("99"), True)
        order["status"] = "paused_reduce_capacity"
        order["oid"] = None
        order["reduce_only_canceled_oid"] = 123
        exchange = FakeExchange()

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            789,
            row,
            asset,
            Decimal("-1"),
            Decimal("100"),
            "abs",
            False,
            set(),
        )

        self.assertFalse(submitted)
        self.assertEqual(len(exchange.orders), 1)
        self.assertFalse(exchange.orders[0][4])
        self.assertEqual(order["status"], "paused_margin")
        self.assertFalse(order["reduce_only"])
        self.assertFalse(order["plan"]["reduce_only"])

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

    def test_roe_allowed_linearly_compresses_add_risk_active_count(self) -> None:
        self.assertEqual(grid_roe_add_risk_allowed(10, None), 10)
        self.assertEqual(grid_roe_add_risk_allowed(10, Decimal("-0.10")), 10)
        self.assertEqual(grid_roe_add_risk_allowed(10, Decimal("-0.25")), 5)
        self.assertEqual(grid_roe_add_risk_allowed(10, Decimal("-0.399")), 1)
        self.assertEqual(grid_roe_add_risk_allowed(10, Decimal("-0.40")), 0)

    def test_roe_pause_candidates_limit_only_add_risk_side(self) -> None:
        row = {
            "levels": [
                {
                    "side": "buy",
                    "status": "active",
                    "oid": oid,
                    "is_buy": True,
                    "price": str(price),
                    "size": "1",
                }
                for oid, price in enumerate(range(100, 90, -1), start=1)
            ]
        }

        buy_candidates, buy_allowed = grid_roe_pause_candidates(row, "buy", Decimal("2"), 10, Decimal("-0.25"))
        sell_candidates, sell_allowed = grid_roe_pause_candidates(row, "sell", Decimal("2"), 10, Decimal("-0.25"))

        self.assertEqual(buy_allowed, 5)
        self.assertEqual(len(buy_candidates), 5)
        self.assertEqual(sell_allowed, 10)
        self.assertEqual(sell_candidates, [])

    def test_roe_restore_respects_stop_and_keep_distribution(self) -> None:
        row = {
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "is_buy": True, "price": "100", "size": "1"},
                {"side": "buy", "status": GRID_ROE_PAUSE_STATUS, "is_buy": True, "price": "99", "size": "1"},
            ]
        }

        paused = row["levels"][1]
        open_slot_row = {
            "levels": [
                {"side": "buy", "status": GRID_ROE_PAUSE_STATUS, "is_buy": True, "price": "99", "size": "1"},
            ]
        }

        self.assertFalse(grid_roe_restore_allowed(row, paused, "buy", Decimal("2"), 10, Decimal("-0.40")))
        self.assertFalse(grid_roe_restore_allowed(row, paused, "buy", Decimal("2"), 10, Decimal("-0.37")))
        self.assertTrue(grid_roe_restore_allowed(open_slot_row, open_slot_row["levels"][0], "buy", Decimal("2"), 10, Decimal("-0.37")))

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

    def test_replacement_rebalance_swaps_toward_logarithmic_distribution(self) -> None:
        row = {
            "levels": [
                {
                    "side": "buy",
                    "status": "paused_replacement",
                    "oid": None,
                    "is_buy": True,
                    "price": price,
                    "size": "1",
                    "replacement_order": True,
                }
                for price in ("106", "105", "101")
            ]
            + [
                {
                    "side": "buy",
                    "status": "active",
                    "oid": oid,
                    "is_buy": True,
                    "price": price,
                    "size": "1",
                    "replacement_order": True,
                }
                for oid, price in ((1, "104"), (2, "103"), (3, "102"))
            ]
        }

        pause_entry, restore_entry = grid_replacement_rebalance_pair(
            row,
            "buy",
            Decimal("0"),
            Decimal("0"),
            Decimal("400"),
            "abs",
        )

        self.assertEqual(pause_entry["price"], "102")
        self.assertEqual(restore_entry["price"], "106")

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

    def test_topup_ignores_paused_gap_that_would_cross_market(self) -> None:
        row = {
            "gap_rate": "0.0002",
            "min_order_value": "1",
            "sz_decimals": 3,
            "base_buy_size": "0.002",
            "topup_buy_size": "0.002",
            "topup_buy_gap": "0.0002",
            "levels": [
                {"side": "buy", "status": "paused_replacement", "is_buy": True, "price": "7370.6", "size": "0.002"},
                {"side": "buy", "status": "paused_replacement", "is_buy": True, "price": "7360.2", "size": "0.002"},
                {"side": "buy", "status": "active", "oid": 1, "is_buy": True, "price": "7316.7", "size": "0.002"},
                {"side": "buy", "status": "active", "oid": 2, "is_buy": True, "price": "7274.8", "size": "0.002"},
            ],
        }
        asset = {"szDecimals": 3}

        topup = next_depth_order(
            row,
            "xyz:SP500",
            asset,
            "buy",
            Decimal("7332.0"),
            Decimal("0.008"),
            Decimal("58.6"),
            Decimal("250"),
            "abs",
            Decimal("7331.8"),
            Decimal("7331.8"),
            Decimal("7332.2"),
        )

        self.assertIsNotNone(topup)
        self.assertEqual(topup["price"], "7324.4")
        self.assertLess(Decimal(str(topup["price"])), Decimal("7332.2"))
        self.assertNotEqual(topup["price"], "7365.4")

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

    def test_account_margin_protected_replacement_pauses_for_restore(self) -> None:
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
            "levels": [],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False)
        order["replacement_order"] = True

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            7,
            row,
            asset,
            Decimal("1"),
            Decimal("100"),
            "abs",
            True,
            set(),
        )

        self.assertFalse(submitted)
        self.assertEqual(exchange.orders, [])
        self.assertEqual(order["status"], "paused_account_margin")
        self.assertIsNone(order["oid"])
        self.assertEqual(order["paused_at"], 7)
        self.assertNotIn("skipped_at", order)

    def test_account_margin_protected_regular_add_risk_is_skipped(self) -> None:
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
            "levels": [],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False)

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            7,
            row,
            asset,
            Decimal("1"),
            Decimal("100"),
            "abs",
            True,
            set(),
        )

        self.assertFalse(submitted)
        self.assertEqual(exchange.orders, [])
        self.assertEqual(order["status"], "skipped_account_margin")
        self.assertIsNone(order["oid"])
        self.assertEqual(order["skipped_at"], 7)
        self.assertNotIn("paused_at", order)

    def test_same_run_insufficient_margin_pauses_later_same_side_without_submit(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                return {"status": "ok", "response": {"data": {"statuses": [{"error": "Insufficient margin"}]}}}

        exchange = FakeExchange()
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        first = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False)
        second = grid_order_entry(row, "BTC", asset, True, Decimal("99"), False)
        margin_blocked_sides: set[tuple[str, str]] = set()

        first_submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            first,
            7,
            row,
            asset,
            Decimal("1"),
            Decimal("100"),
            "abs",
            False,
            set(),
            margin_blocked_sides=margin_blocked_sides,
        )
        second_submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            second,
            7,
            row,
            asset,
            Decimal("1"),
            Decimal("100"),
            "abs",
            False,
            set(),
            margin_blocked_sides=margin_blocked_sides,
        )

        self.assertFalse(first_submitted)
        self.assertFalse(second_submitted)
        self.assertEqual(len(exchange.orders), 1)
        self.assertEqual(margin_blocked_sides, {("BTC", "buy")})
        self.assertEqual(first["status"], "paused_margin")
        self.assertIn("Insufficient margin", first["last_error"])
        self.assertEqual(second["status"], "paused_margin")
        self.assertEqual(second["last_error"], "same-run insufficient margin pause")
        self.assertEqual(second["paused_at"], 7)

    def test_margin_reject_pauses_same_side_missing_entries(self) -> None:
        row = {
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "100"},
                {"side": "buy", "status": "recovery_deferred", "oid": 2, "price": "99"},
                {"side": "buy", "status": "active", "oid": None, "price": "98"},
                {"side": "buy", "status": "paused_reduce_capacity", "oid": None, "price": "97"},
                {"side": "buy", "status": "paused_replacement", "oid": None, "price": "96"},
                {"side": "sell", "status": "recovery_deferred", "oid": 3, "price": "101"},
                {"side": "buy", "status": "filled", "oid": 4, "price": "95"},
            ]
        }

        paused = pause_grid_margin_side_entries(row, "buy", 7, "Insufficient margin")

        self.assertEqual(paused, 4)
        self.assertEqual(row["levels"][0]["status"], "active")
        self.assertEqual(row["levels"][0]["oid"], 1)
        self.assertEqual(row["levels"][1]["status"], "paused_margin")
        self.assertIsNone(row["levels"][1]["oid"])
        self.assertEqual(row["levels"][1]["last_error"], "Insufficient margin")
        self.assertEqual(row["levels"][2]["status"], "paused_margin")
        self.assertEqual(row["levels"][3]["status"], "paused_margin")
        self.assertEqual(row["levels"][4]["status"], "paused_margin")
        self.assertEqual(row["levels"][5]["status"], "recovery_deferred")
        self.assertEqual(row["levels"][6]["status"], "filled")

    def test_margin_pause_is_current_run_only(self) -> None:
        row = {}

        pause_grid_margin_side(row, "sell", 7, Decimal("100"))

        self.assertTrue(grid_margin_pause_active(row, "sell", 7, Decimal("100"), Decimal("1")))
        self.assertFalse(grid_margin_pause_active(row, "sell", 8, Decimal("100"), Decimal("1")))
        self.assertNotIn("margin_pauses", row)

    def test_find_current_position_from_cached_state_matches_coin(self) -> None:
        state = {
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "0"}},
                {"position": {"coin": "xyz:XYZ100", "szi": "0.2", "positionValue": "10"}},
            ]
        }

        position = find_current_position_from_state(state, "xyz:XYZ100")

        self.assertIsNotNone(position)
        self.assertEqual(position["positionValue"], "10")
        self.assertIsNone(find_current_position_from_state(state, "BTC"))

    def test_historical_insufficient_margin_does_not_create_new_margin_pause(self) -> None:
        row = {
            "margin_pauses": {"sell": {"paused_at": 6, "position_value": "100"}},
            "levels": [
                {
                    "side": "sell",
                    "is_buy": False,
                    "status": "paused_replacement",
                    "oid": None,
                    "price": "101",
                    "last_error": "Failed to submit grid child order: Insufficient margin",
                },
                {"side": "sell", "is_buy": False, "status": "paused_reduce_capacity", "oid": None, "price": "102"},
            ]
        }

        self.assertFalse(grid_margin_pause_active(row, "sell", 7, Decimal("100"), Decimal("-1")))
        self.assertNotIn("margin_pauses", row)
        self.assertEqual(row["levels"][0]["status"], "paused_replacement")
        self.assertEqual(row["levels"][1]["status"], "paused_reduce_capacity")

    def test_stale_margin_pauses_clear_at_next_worker_run(self) -> None:
        row = {
            "margin_pauses": {
                "buy": {"paused_at": 7, "position_value": "100"},
                "sell": {"paused_at": 8, "position_value": "100"},
            }
        }

        changed = clear_stale_grid_margin_pauses(row, 8)

        self.assertTrue(changed)
        self.assertEqual(row["margin_pauses"], {"sell": {"paused_at": 8, "position_value": "100"}})

    def test_reduce_risk_margin_reject_retries_as_reduce_only(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                if len(self.orders) == 1:
                    return {"status": "ok", "response": {"data": {"statuses": [{"error": "Insufficient margin"}]}}}
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
        order = grid_order_entry(row, "BTC", asset, False, Decimal("99"), False)

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            7,
            row,
            asset,
            Decimal("1"),
            Decimal("100"),
            "abs",
            False,
            set(),
        )

        self.assertTrue(submitted)
        self.assertEqual([call[4] for call in exchange.orders], [False, True])
        self.assertEqual(order["status"], "active")
        self.assertEqual(order["oid"], 456)
        self.assertTrue(order["reduce_only"])
        self.assertTrue(order["plan"]["reduce_only"])
        self.assertEqual(order["margin_reduce_only_retry_at"], 7)

    def test_reduce_risk_margin_reject_does_not_block_same_side(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                return {"status": "ok", "response": {"data": {"statuses": [{"error": "Insufficient margin"}]}}}

        exchange = FakeExchange()
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        first = grid_order_entry(row, "BTC", asset, False, Decimal("101"), False)
        second = grid_order_entry(row, "BTC", asset, False, Decimal("102"), False)
        margin_blocked_sides: set[tuple[str, str]] = set()

        first_submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            first,
            7,
            row,
            asset,
            Decimal("1"),
            Decimal("200"),
            "abs",
            False,
            set(),
            margin_blocked_sides=margin_blocked_sides,
        )
        second_submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            second,
            7,
            row,
            asset,
            Decimal("1"),
            Decimal("200"),
            "abs",
            False,
            set(),
            margin_blocked_sides=margin_blocked_sides,
        )

        self.assertFalse(first_submitted)
        self.assertFalse(second_submitted)
        self.assertEqual(len(exchange.orders), 4)
        self.assertEqual([call[4] for call in exchange.orders], [False, True, False, True])
        self.assertEqual(margin_blocked_sides, set())
        self.assertNotIn("margin_pauses", row)
        self.assertEqual(first["status"], "paused_margin")
        self.assertEqual(second["status"], "paused_margin")
        self.assertNotEqual(second["last_error"], "same-run insufficient margin pause")

    def test_add_risk_margin_reject_does_not_retry_reduce_only(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                return {"status": "ok", "response": {"data": {"statuses": [{"error": "Insufficient margin"}]}}}

        exchange = FakeExchange()
        row = {
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "levels": [],
        }
        asset = {"szDecimals": 2, "maxLeverage": 20}
        order = grid_order_entry(row, "BTC", asset, True, Decimal("99"), False)

        submitted = submit_grid_order_entry(
            exchange,
            "BTC",
            order,
            7,
            row,
            asset,
            Decimal("1"),
            Decimal("100"),
            "abs",
            False,
            set(),
        )

        self.assertFalse(submitted)
        self.assertEqual(len(exchange.orders), 1)
        self.assertEqual(exchange.orders[0][4], False)
        self.assertEqual(order["status"], "paused_margin")
        self.assertFalse(order["reduce_only"])

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

    def test_margin_rejected_replacement_normalizes_after_restore_attempt(self) -> None:
        levels = []
        order = {
            "side": "sell",
            "status": "paused_margin",
            "oid": None,
            "price": "101",
            "size": "1",
            "replacement_order": True,
            "last_error": "Insufficient margin",
            "paused_at": 1,
        }

        preserve_replacement_order(levels, order, 2)

        self.assertEqual(order["status"], "paused_margin")
        self.assertEqual(order["replacement_pause_reason"], "paused_margin")

        preserve_replacement_order(levels, order, 3, normalize_margin=True)

        self.assertEqual(order["status"], "paused_replacement")
        self.assertEqual(order["replacement_pause_reason"], "paused_margin")
        self.assertEqual(order["last_error"], "Insufficient margin")

    def test_restore_loop_normalizes_margin_paused_replacement_before_submit_quota(self) -> None:
        order = {
            "side": "sell",
            "status": "paused_margin",
            "oid": None,
            "price": "101",
            "size": "1",
            "replacement_order": True,
            "last_error": "Insufficient margin",
            "paused_at": 1,
        }

        self.assertTrue(normalize_margin_paused_replacement(order, 3))
        self.assertEqual(order["status"], "paused_replacement")
        self.assertEqual(order["replacement_pause_reason"], "paused_margin")
        self.assertEqual(order["last_error"], "Insufficient margin")

    def test_skipped_account_margin_replacement_is_migrated_to_paused_replacement(self) -> None:
        levels = [
            {
                "side": "sell",
                "status": "skipped_account_margin",
                "oid": None,
                "price": "101",
                "size": "1",
                "replacement_order": True,
                "skipped_at": 1,
            }
        ]

        migrated = pause_skipped_account_margin_replacement(levels, levels[0], 2)

        self.assertTrue(migrated)
        self.assertEqual(levels[0]["status"], "paused_replacement")
        self.assertTrue(levels[0]["replacement_order"])
        self.assertEqual(levels[0]["replacement_pause_reason"], "skipped_account_margin")
        self.assertEqual(levels[0]["paused_at"], 2)

    def test_regular_skipped_account_margin_is_not_migrated_to_replacement(self) -> None:
        entry = {
            "side": "sell",
            "status": "skipped_account_margin",
            "oid": None,
            "price": "101",
            "size": "1",
            "skipped_at": 1,
        }

        migrated = pause_skipped_account_margin_replacement([entry], entry, 2)

        self.assertFalse(migrated)
        self.assertEqual(entry["status"], "skipped_account_margin")
        self.assertNotIn("replacement_order", entry)

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
        self.assertEqual(row["avg_multiplier"], "1.744")
        self.assertEqual(row["topup_buy_gap"], "0.01")
        self.assertEqual(row["topup_sell_gap"], "0.01744")
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
        self.assertEqual(summary["avg_multiplier"], "1.744")
        self.assertEqual(summary["avg_side"], "buy")
        self.assertEqual(summary["base_gap"], "0.05% (0.0005)")
        self.assertEqual(summary["topup_gap"], "buy 0.0005 / sell 0.000872")
        self.assertEqual(summary["base_size"], "buy 0.00016 / sell 0.00016")
        self.assertEqual(summary["topup_size"], "buy 0.00016 / sell 0.00016")

    def test_grid_query_rows_hide_cancelled_grid_batches(self) -> None:
        rows = [
            {"type": "grid", "status": "cancelled", "network": "mainnet", "account": "0xabc", "coin": "BTC"},
            {"type": "grid", "status": "active", "network": "mainnet", "account": "0xabc", "coin": "BTC"},
            {"type": "trail", "status": "active", "network": "mainnet", "account": "0xabc", "coin": "BTC"},
        ]

        self.assertEqual(
            grid_query_rows(rows, "mainnet", "0xabc", "BTC"),
            [rows[1]],
        )

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
        self.assertEqual(plans[0]["grid_avg_multiplier"], Decimal("1.744"))
        self.assertEqual(plans[0]["grid_base_gap"], Decimal("0.005"))
        self.assertEqual(plans[0]["grid_gap"], Decimal("0.005"))
        self.assertEqual(plans[0]["grid_effective_gap"], Decimal("0.008720"))
        self.assertEqual(plans[0]["grid_buy_size"], Decimal("0.101"))
        self.assertEqual(plans[0]["grid_sell_size"], Decimal("0.101"))
        self.assertEqual(plans[0]["grid_topup_buy_size"], Decimal("0.101"))
        self.assertEqual(plans[0]["grid_topup_sell_size"], Decimal("0.101"))
        self.assertEqual(plans[0]["grid_topup_buy_gap"], Decimal("0.005"))
        self.assertEqual(plans[0]["grid_topup_sell_gap"], Decimal("0.008720"))

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
        self.assertEqual(row["effective_gap_rate"], "0.00872")
        self.assertEqual(row["base_buy_size"], "0.101")
        self.assertEqual(row["buy_size"], "0.101")
        self.assertEqual(row["sell_size"], "0.101")
        self.assertEqual(row["topup_buy_size"], "0.101")
        self.assertEqual(row["topup_sell_size"], "0.101")
        self.assertEqual(row["topup_buy_gap"], "0.005")
        self.assertEqual(row["topup_sell_gap"], "0.00872")

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
            ("150", "1.744", "buy"),
            ("175", "1.456", "buy"),
            ("200", "1", None),
            ("250", "1.456", "sell"),
            ("300", "1.744", "sell"),
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
