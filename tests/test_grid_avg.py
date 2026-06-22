import unittest
from argparse import Namespace
from decimal import Decimal

from hl_order import build_grid_batch_row, build_grid_orders, grid_avg_bounds, grid_avg_multiplier, grid_avg_size_pair


class GridAvgTests(unittest.TestCase):
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
        self.assertEqual(plans[0]["grid_avg_multiplier"], Decimal("1.20"))
        self.assertEqual(plans[0]["grid_base_gap"], Decimal("0.005"))
        self.assertEqual(plans[0]["grid_gap"], Decimal("0.00600"))
        self.assertEqual(plans[0]["grid_buy_size"], Decimal("0.121"))
        self.assertEqual(plans[0]["grid_sell_size"], Decimal("0.101"))

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
        self.assertEqual(row["effective_gap_rate"], "0.006")
        self.assertEqual(row["base_buy_size"], "0.101")
        self.assertEqual(row["buy_size"], "0.121")
        self.assertEqual(row["sell_size"], "0.101")

    def test_long_multiplier_is_piecewise_linear_and_capped(self) -> None:
        cases = (
            ("50", "1.4", "buy"),
            ("100", "1.4", "buy"),
            ("150", "1.20", "buy"),
            ("200", "1", None),
            ("300", "1.20", "sell"),
            ("400", "1.4", "sell"),
            ("500", "1.4", "sell"),
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
        self.assertEqual((low_multiplier, low_side, low_current), (Decimal("1.4"), "sell", Decimal("100")))
        self.assertEqual((high_multiplier, high_side, high_current), (Decimal("1.4"), "buy", Decimal("400")))

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
