import unittest
from decimal import Decimal

from trail_worker import lifecycle_submit_limit_chase


class P4CrossZeroTests(unittest.TestCase):
    def test_long_position_is_closed_before_p4_can_open_short(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.calls = []

            def _slippage_price(self, coin, is_buy, slippage, mid):
                return mid

            def order(
                self,
                coin,
                is_buy,
                size,
                price,
                order_type,
                reduce_only=False,
                cloid=None,
            ):
                self.calls.append(
                    {
                        "is_buy": is_buy,
                        "size": Decimal(str(size)),
                        "reduce_only": reduce_only,
                    }
                )
                if len(self.calls) == 1:
                    return {
                        "status": "ok",
                        "response": {
                            "data": {
                                "statuses": [
                                    {
                                        "filled": {
                                            "oid": 1,
                                            "avgPx": "100",
                                            "totalSz": str(size),
                                        }
                                    }
                                ]
                            }
                        },
                    }
                return {
                    "status": "ok",
                    "response": {
                        "data": {
                            "statuses": [{"resting": {"oid": len(self.calls)}}]
                        }
                    },
                }

        row = {
            "position_limit_mode": "limit",
            "min_position_value": "-200",
            "max_position_value": "-100",
            "gap_rate": "0.01",
            "min_order_value": "10",
            "base_buy_size": "0.1",
            "base_sell_size": "0.1",
            "levels": [],
        }
        exchange = FakeExchange()
        ctx = {
            "withdrawable": Decimal("100"),
            "position_size": Decimal("0.5"),
            "position_value": Decimal("50"),
            "position_leverage": Decimal("10"),
            "exchange": exchange,
            "coin": "XYZ",
            "asset": {"szDecimals": 2, "maxLeverage": 20},
            "current_mid": Decimal("100"),
            "best_bid": Decimal("99.9"),
            "best_ask": Decimal("100.1"),
            "now": 123,
            "open_orders": [],
        }

        self.assertTrue(
            lifecycle_submit_limit_chase(
                row,
                ctx,
                {"action_limit_headroom": 200},
            )
        )

        market_call = exchange.calls[0]
        self.assertFalse(market_call["is_buy"])
        self.assertEqual(market_call["size"], Decimal("0.5"))
        self.assertTrue(market_call["reduce_only"])
        self.assertEqual(
            sum(Decimal(entry["size"]) for entry in row["levels"]),
            Decimal("0.5"),
        )
        self.assertTrue(all(entry["side"] == "buy" for entry in row["levels"]))


if __name__ == "__main__":
    unittest.main()
