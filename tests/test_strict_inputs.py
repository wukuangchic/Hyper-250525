import sys
import unittest
from contextlib import redirect_stderr
from decimal import Decimal
from io import StringIO
from unittest.mock import Mock, patch

from hl_order import parse_args, place_order, position_relative_order_side, post_only_immediate_match_error
from simple_hyper.order_specs import canonical_coin_input, normalize_coin_input, parse_side


class StrictInputTests(unittest.TestCase):
    def parse_cli(self, *args: str):
        with patch.object(sys, "argv", ["hl_order.py", *args]):
            return parse_args()

    def assert_cli_rejected(self, *args: str) -> None:
        with self.assertRaises(SystemExit):
            with redirect_stderr(StringIO()):
                self.parse_cli(*args)

    def test_side_requires_buy_or_sell(self) -> None:
        self.assertTrue(parse_side("buy"))
        self.assertFalse(parse_side("sell"))
        for side in ("b", "s", "long", "short", "up", "down", "多", "空", "看多", "看空"):
            with self.subTest(side=side):
                with self.assertRaisesRegex(ValueError, "Use buy or sell"):
                    parse_side(side)

    def test_cli_rejects_old_side_aliases(self) -> None:
        for side in ("b", "s", "long", "short", "多", "空"):
            with self.subTest(side=side):
                self.assert_cli_rejected("BTC", side, "10", "--dry-run")

    def test_cli_accepts_position_relative_sides(self) -> None:
        self.assertEqual(self.parse_cli("BTC", "open", "10", "--dry-run").side, "open")
        self.assertEqual(self.parse_cli("BTC", "close", "10", "--dry-run").side, "close")

    def test_position_relative_side_follows_or_opposes_current_position(self) -> None:
        self.assertEqual(position_relative_order_side("open", Decimal("1")), "buy")
        self.assertEqual(position_relative_order_side("close", Decimal("1")), "sell")
        self.assertEqual(position_relative_order_side("open", Decimal("-1")), "sell")
        self.assertEqual(position_relative_order_side("close", Decimal("-1")), "buy")
        with self.assertRaisesRegex(ValueError, "without a current position"):
            position_relative_order_side("open", Decimal("0"))

    def test_symmetric_orders_require_both_literal(self) -> None:
        args = self.parse_cli("BTC", "both", "--total", "20", "--offset", "2%", "--dry-run")
        self.assertTrue(args.symmetric)
        self.assertEqual(args.side, "both")

        for side in ("sym", "symmetric", "dual", "双向", "对称", "对称单"):
            with self.subTest(side=side):
                self.assert_cli_rejected("BTC", side, "--total", "20", "--offset", "2%", "--dry-run")

    def test_book_level_alias_is_removed(self) -> None:
        self.assert_cli_rejected("BTC", "buy", "10", "--book-level", "3", "--dry-run")

    def test_post_only_immediate_match_error_matches_only_single_alo_cross_reject(self) -> None:
        rejected = {
            "status": "ok",
            "response": {"data": {"statuses": [{"error": "Post only would immediately match"}]}},
        }
        self.assertEqual(post_only_immediate_match_error(rejected), "Post only would immediately match")
        self.assertIsNone(
            post_only_immediate_match_error(
                {"status": "ok", "response": {"data": {"statuses": [{"error": "Insufficient margin"}]}}}
            )
        )
        self.assertIsNone(
            post_only_immediate_match_error(
                {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}
            )
        )

    def test_coin_input_no_longer_expands_suffix_shorthands(self) -> None:
        self.assertEqual(canonical_coin_input("xyz:gold"), "xyz:GOLD")
        self.assertEqual(normalize_coin_input("BTCUSD"), ["BTCUSD", "BTCUSD"])
        self.assertEqual(normalize_coin_input("BTC-PERP"), ["BTC-PERP", "BTC-PERP"])

    def test_grid_limit_accepts_signed_min_max(self) -> None:
        args = self.parse_cli("BTC", "grid", "--limit", "-200", "400", "--dry-run")
        self.assertEqual(args.grid_position_limit_mode, "limit")
        self.assertEqual(args.grid_position_min_value, "-200")
        self.assertEqual(args.grid_position_limit_value, "400")

    def test_grid_modify_accepts_all_negative_signed_limit(self) -> None:
        args = self.parse_cli(
            "xyz:JPY",
            "grid",
            "--modify",
            "--limit",
            "-400",
            "-50",
            "--avg",
            "-150",
            "--min",
            "20",
        )
        info = Mock()
        info.all_mids.return_value = {"xyz:JPY": "1"}
        asset = {"maxLeverage": 5}

        with patch("hl_order.build_clients", return_value=(info, Mock(), "account", "signer", "agent")), \
             patch("hl_order.resolve_perp_asset", return_value=("xyz:JPY", asset)), \
             patch("hl_order.coin_dex", return_value="xyz"), \
             patch("hl_order.coin_display_rate", return_value=None), \
             patch("hl_order.modify_grid_batch_order") as modify_grid:
            place_order(args)

        modify_grid.assert_called_once()
        self.assertEqual(args.grid_position_min_value, "-400")
        self.assertEqual(args.grid_position_limit_value, "-50")

    def test_grid_requires_limit_range(self) -> None:
        self.assert_cli_rejected("BTC", "grid", "--dry-run")
        self.assert_cli_rejected("BTC", "grid", "--limit", "400", "200", "--dry-run")


if __name__ == "__main__":
    unittest.main()
