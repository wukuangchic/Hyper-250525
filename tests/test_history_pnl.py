import unittest
from decimal import Decimal

from hl_order import HISTORY_PNL_MAX_FILLS, calculate_history_pnl, history_pnl_window_hours, real_pnl_label


class HistoryPnlTests(unittest.TestCase):
    def test_formats_rounded_up_account_fill_window_in_header(self) -> None:
        fills = [{"time": 1_000}]
        now_ms = 1_000 + 34 * 60 * 60 * 1000 + 1

        hours = history_pnl_window_hours(fills, now_ms=now_ms)

        self.assertEqual(hours, 35)
        self.assertEqual(real_pnl_label(hours), "realPnl(35H)")

    def test_uses_exchange_unrealized_pnl_when_recent_fills_do_not_rebuild_position(self) -> None:
        fills = [
            {
                "coin": "BTC",
                "closedPnl": "3.5",
                "fee": "0.2",
                "side": "A",
                "sz": "1",
                "px": "100",
                "time": 1,
            }
        ]
        funding = [{"time": 2, "delta": {"coin": "BTC", "usdc": "-0.1"}}]

        result = calculate_history_pnl(
            None,
            "account",
            "BTC",
            mark_px=Decimal("90"),
            unrealized_pnl=Decimal("1.25"),
            fills=fills,
            funding_rows=funding,
        )

        self.assertEqual(result["openPnl"], Decimal("1.25"))
        self.assertEqual(result["realPnl"], Decimal("4.45"))

    def test_marks_result_partial_when_account_fill_window_reaches_limit(self) -> None:
        fills = [
            {
                "coin": "ETH",
                "closedPnl": "0",
                "fee": "0",
                "side": "B",
                "sz": "1",
                "px": "100",
                "time": index,
            }
            for index in range(HISTORY_PNL_MAX_FILLS)
        ]

        result = calculate_history_pnl(
            None,
            "account",
            "BTC",
            unrealized_pnl=Decimal("0"),
            fills=fills,
            funding_rows=[],
        )

        self.assertEqual(result["fills"], 0)
        self.assertTrue(result["partial"])


if __name__ == "__main__":
    unittest.main()
