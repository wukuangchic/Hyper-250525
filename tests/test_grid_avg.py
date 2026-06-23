import unittest
from argparse import Namespace
from decimal import Decimal

from hl_order import (
    build_grid_batch_row,
    build_grid_orders,
    grid_avg_bounds,
    grid_avg_multiplier,
    grid_avg_size_pair,
    grid_avg_topup_params,
    grid_query_avg_summary,
    refresh_grid_row_strategy_params,
)
from trail_worker import near_grid_orders_if_stale, next_depth_order, replacement_order_from_fill


class GridAvgTests(unittest.TestCase):
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
        self.assertEqual(row["avg_multiplier"], "1.31")
        self.assertEqual(row["topup_buy_gap"], "0.01")
        self.assertEqual(row["topup_sell_gap"], "0.0131")
        self.assertGreater(Decimal(row["topup_buy_size"]), Decimal(row["base_buy_size"]))
        self.assertEqual(row["topup_sell_size"], row["base_sell_size"])

    def test_topup_params_separate_reversion_size_from_risk_gap(self) -> None:
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
        self.assertEqual(buy_favored, (Decimal("0.140"), Decimal("0.100"), Decimal("0.01"), Decimal("0.014")))
        self.assertEqual(sell_favored, (Decimal("0.100"), Decimal("0.140"), Decimal("0.014"), Decimal("0.01")))

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
        self.assertEqual(summary["avg_multiplier"], "1.31")
        self.assertEqual(summary["avg_side"], "buy")
        self.assertEqual(summary["base_gap"], "0.05% (0.0005)")
        self.assertEqual(summary["topup_gap"], "buy 0.0005 / sell 0.000655")
        self.assertEqual(summary["base_size"], "buy 0.00016 / sell 0.00016")
        self.assertEqual(summary["topup_size"], "buy 0.00021 / sell 0.00016")

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
        self.assertEqual(plans[0]["grid_avg_multiplier"], Decimal("1.310"))
        self.assertEqual(plans[0]["grid_base_gap"], Decimal("0.005"))
        self.assertEqual(plans[0]["grid_gap"], Decimal("0.005"))
        self.assertEqual(plans[0]["grid_effective_gap"], Decimal("0.006550"))
        self.assertEqual(plans[0]["grid_buy_size"], Decimal("0.101"))
        self.assertEqual(plans[0]["grid_sell_size"], Decimal("0.101"))
        self.assertEqual(plans[0]["grid_topup_buy_size"], Decimal("0.132"))
        self.assertEqual(plans[0]["grid_topup_sell_size"], Decimal("0.101"))
        self.assertEqual(plans[0]["grid_topup_buy_gap"], Decimal("0.005"))
        self.assertEqual(plans[0]["grid_topup_sell_gap"], Decimal("0.006550"))

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
        self.assertEqual(row["effective_gap_rate"], "0.00655")
        self.assertEqual(row["base_buy_size"], "0.101")
        self.assertEqual(row["buy_size"], "0.101")
        self.assertEqual(row["sell_size"], "0.101")
        self.assertEqual(row["topup_buy_size"], "0.132")
        self.assertEqual(row["topup_sell_size"], "0.101")
        self.assertEqual(row["topup_buy_gap"], "0.005")
        self.assertEqual(row["topup_sell_gap"], "0.00655")

    def test_only_far_side_topup_uses_dynamic_gap_and_size(self) -> None:
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
            "levels": [
                {
                    "side": "buy",
                    "status": "active",
                    "oid": 1,
                    "is_buy": True,
                    "price": "90",
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
        self.assertEqual(topup["size"], "0.14")
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
        self.assertEqual(near_orders[0]["size"], "0.1")
        self.assertEqual(near_orders[0]["plan"]["grid_gap"], Decimal("0.01"))

    def test_long_multiplier_is_piecewise_linear_and_capped(self) -> None:
        cases = (
            ("50", "1.62", "buy"),
            ("100", "1.62", "buy"),
            ("150", "1.310", "buy"),
            ("200", "1", None),
            ("300", "1.310", "sell"),
            ("400", "1.62", "sell"),
            ("500", "1.62", "sell"),
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
        self.assertEqual((low_multiplier, low_side, low_current), (Decimal("1.62"), "sell", Decimal("100")))
        self.assertEqual((high_multiplier, high_side, high_current), (Decimal("1.62"), "buy", Decimal("400")))

    def test_abs_bounds_allow_negative_average(self) -> None:
        self.assertEqual(
            grid_avg_bounds("abs", Decimal("0"), Decimal("300")),
            (Decimal("-300"), Decimal("300")),
        )

    def test_size_uses_nearest_rounding_without_forced_increment(self) -> None:
        self.assertEqual(
            grid_avg_size_pair(Decimal("0.002"), Decimal("0.002"), Decimal("1.2"), "buy", 3),
            (Decimal("0.002"), Decimal("0.002")),
        )
        self.assertEqual(
            grid_avg_size_pair(Decimal("0.002"), Decimal("0.002"), Decimal("1.4"), "buy", 3),
            (Decimal("0.003"), Decimal("0.002")),
        )


if __name__ == "__main__":
    unittest.main()
