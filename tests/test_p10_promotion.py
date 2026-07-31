from __future__ import annotations

import unittest
from decimal import Decimal

from trail_worker import (
    GRID_CHAIN_DEBT_STATUS,
    GRID_LIFECYCLE_PHASES,
    lifecycle_process_p10,
)


class FakeInfo:
    def query_order_by_oid(self, account, oid):
        return {"status": "order", "order": {"status": "canceled", "order": {"oid": oid}}}

    def query_order_by_cloid(self, account, cloid):
        return {"status": "unknownOid"}


class FakeExchange:
    def __init__(self, *, market_error: str | None = None, market_exception: str | None = None) -> None:
        self.market_error = market_error
        self.market_exception = market_exception
        self.cancel_calls = []
        self.order_calls = []

    def _slippage_price(self, coin, is_buy, slippage, current_mid):
        return current_mid

    def bulk_cancel(self, requests):
        self.cancel_calls.append(requests)
        return {"status": "ok", "response": {"data": {"statuses": ["success"]}}}

    def order(self, coin, is_buy, size, price, order_type, reduce_only=False, cloid=None):
        self.order_calls.append(
            {
                "coin": coin,
                "is_buy": is_buy,
                "size": size,
                "price": price,
                "order_type": order_type,
                "reduce_only": reduce_only,
                "cloid": cloid,
            }
        )
        if cloid is not None:
            if self.market_exception:
                raise TimeoutError(self.market_exception)
            if self.market_error:
                return {
                    "status": "ok",
                    "response": {"data": {"statuses": [{"error": self.market_error}]}},
                }
            return {
                "status": "ok",
                "response": {
                    "data": {
                        "statuses": [
                            {"filled": {"oid": 22, "avgPx": str(price), "totalSz": str(size)}}
                        ]
                    }
                },
            }
        return {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 23}}]}},
        }


def make_row(*, short: bool = False) -> tuple[dict, dict]:
    if short:
        source = {
            "side": "sell",
            "is_buy": False,
            "status": "active",
            "oid": 11,
            "price": "70",
            "size": "0.2",
            "reduce_only": False,
            "grid_leg": 0,
            "iteration": 4,
        }
        minimum, maximum = "-500", "-100"
    else:
        source = {
            "side": "buy",
            "is_buy": True,
            "status": "active",
            "oid": 11,
            "price": "50",
            "size": "0.2",
            "reduce_only": False,
            "grid_leg": 0,
            "iteration": 4,
        }
        minimum, maximum = "100", "500"
    row = {
        "id": "grid-p10",
        "type": "grid",
        "status": "active",
        "grid_lifecycle_version": 2,
        "network": "mainnet",
        "account": "0xabc",
        "coin": "BTC",
        "position_limit_mode": "limit",
        "min_position_value": minimum,
        "max_position_value": maximum,
        "gap_rate": "0.01",
        "slippage": "0.01",
        "min_order_value": "10",
        "base_buy_size": "0.2",
        "base_sell_size": "0.2",
        "sz_decimals": 2,
        "levels": [source],
    }
    return row, source


def make_ctx(exchange, *, short: bool = False, withdrawable: str = "10") -> dict:
    return {
        "network": "mainnet",
        "account": "0xabc",
        "coin": "BTC",
        "asset": {"szDecimals": 2, "maxLeverage": 10},
        "exchange": exchange,
        "info": FakeInfo(),
        "now": 100,
        "now_ms": 100000,
        "position_size": Decimal("-1" if short else "1"),
        "position_value": Decimal("60"),
        "position_leverage": Decimal("10"),
        "current_mid": Decimal("60"),
        "best_bid": Decimal("59.9"),
        "best_ask": Decimal("60.1"),
        "withdrawable": Decimal(withdrawable),
        "liquidation_px": None,
        "open_orders": [
            {
                "coin": "BTC",
                "side": "A" if short else "B",
                "limitPx": "70" if short else "50",
                "sz": "0.2",
                "oid": 11,
                "reduceOnly": False,
            }
        ],
        "open_oids": {11},
        "fills_by_oid": {},
    }


class P10PromotionTests(unittest.TestCase):
    def test_p10_is_last_lifecycle_phase(self) -> None:
        self.assertEqual(GRID_LIFECYCLE_PHASES[-1], "p10")

    def test_long_promotion_cancels_then_buys_and_places_mirrored_sell(self) -> None:
        row, source = make_row()
        exchange = FakeExchange()
        cache = {"action_limit_headroom": 10}

        changed = lifecycle_process_p10(row, make_ctx(exchange), cache)

        self.assertTrue(changed)
        self.assertEqual(exchange.cancel_calls, [[{"coin": "BTC", "oid": 11}]])
        self.assertEqual(len(exchange.order_calls), 2)
        self.assertTrue(exchange.order_calls[0]["is_buy"])
        self.assertEqual(exchange.order_calls[0]["size"], 0.2)
        self.assertIsNotNone(exchange.order_calls[0]["cloid"])
        self.assertFalse(exchange.order_calls[1]["is_buy"])
        self.assertEqual(exchange.order_calls[1]["price"], 70.0)
        self.assertTrue(exchange.order_calls[1]["reduce_only"])
        self.assertNotIn(source, row["levels"])
        replacement = row["levels"][0]
        self.assertEqual(replacement["price"], "70")
        self.assertEqual(replacement["size"], "0.2")
        self.assertEqual(replacement["oid"], 23)
        self.assertEqual(replacement["status"], "active")
        self.assertTrue(replacement["p10_replacement"])
        self.assertNotIn("p10_promotion_intent", row)
        self.assertEqual(cache["action_limit_headroom"], 7)

    def test_short_promotion_uses_symmetric_mirrored_buy(self) -> None:
        row, _source = make_row(short=True)
        exchange = FakeExchange()

        changed = lifecycle_process_p10(
            row, make_ctx(exchange, short=True), {"action_limit_headroom": 10}
        )

        self.assertTrue(changed)
        self.assertFalse(exchange.order_calls[0]["is_buy"])
        self.assertTrue(exchange.order_calls[1]["is_buy"])
        self.assertEqual(exchange.order_calls[1]["price"], 50.0)
        self.assertTrue(exchange.order_calls[1]["reduce_only"])
        self.assertEqual(row["levels"][0]["price"], "50")

    def test_margin_budget_failure_never_cancels_source(self) -> None:
        row, source = make_row()
        exchange = FakeExchange()

        changed = lifecycle_process_p10(
            row, make_ctx(exchange, withdrawable="1"), {"action_limit_headroom": 10}
        )

        self.assertFalse(changed)
        self.assertEqual(exchange.cancel_calls, [])
        self.assertEqual(exchange.order_calls, [])
        self.assertEqual(source["status"], "active")
        self.assertEqual(source["oid"], 11)
        self.assertEqual(row["p10_status"], "skipped_pre_cancel_safety")
        self.assertEqual(row["p10_estimated_margin"], "1.2")

    def test_max_position_failure_never_cancels_source(self) -> None:
        row, source = make_row()
        row["max_position_value"] = "65"
        exchange = FakeExchange()

        changed = lifecycle_process_p10(
            row, make_ctx(exchange), {"action_limit_headroom": 10}
        )

        self.assertFalse(changed)
        self.assertEqual(exchange.cancel_calls, [])
        self.assertEqual(exchange.order_calls, [])
        self.assertEqual(source["status"], "active")

    def test_insufficient_action_headroom_never_cancels_source(self) -> None:
        row, source = make_row()
        exchange = FakeExchange()

        changed = lifecycle_process_p10(
            row, make_ctx(exchange), {"action_limit_headroom": 2}
        )

        self.assertFalse(changed)
        self.assertEqual(exchange.cancel_calls, [])
        self.assertEqual(exchange.order_calls, [])
        self.assertEqual(source["status"], "active")

    def test_market_timeout_preserves_cancelled_source_and_intent_for_reconcile(self) -> None:
        row, source = make_row()
        exchange = FakeExchange(market_exception="timed out")

        changed = lifecycle_process_p10(
            row, make_ctx(exchange), {"action_limit_headroom": 10}
        )

        self.assertTrue(changed)
        self.assertEqual(source["status"], "p10_cancelled")
        self.assertIsNone(source["oid"])
        self.assertEqual(row["p10_promotion_intent"]["status"], "awaiting_reconcile")
        self.assertIn("timed out", row["p10_promotion_intent"]["last_error"])
        self.assertEqual(len(exchange.cancel_calls), 1)
        self.assertEqual(len(exchange.order_calls), 1)

    def test_market_rejection_restores_cancelled_source_to_p3(self) -> None:
        row, source = make_row()
        exchange = FakeExchange(market_error="Insufficient margin")
        cache = {"action_limit_headroom": 10}

        changed = lifecycle_process_p10(row, make_ctx(exchange), cache)

        self.assertTrue(changed)
        self.assertEqual(len(exchange.cancel_calls), 1)
        self.assertEqual(len(exchange.order_calls), 1)
        self.assertEqual(source["status"], GRID_CHAIN_DEBT_STATUS)
        self.assertIsNone(source["oid"])
        self.assertTrue(source["p10_restore"])
        self.assertEqual(source["p10_source_oid"], 11)
        self.assertEqual(source["p3_queue_seq"], 0)
        self.assertNotIn("p10_promotion_intent", row)


if __name__ == "__main__":
    unittest.main()
