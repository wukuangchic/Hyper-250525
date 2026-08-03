from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from trail_worker import (
    GRID_CHAIN_DEBT_STATUS,
    GRID_LIFECYCLE_PHASE_P10,
    GRID_LIFECYCLE_PHASES,
    lifecycle_process_p10,
    maintain_grid,
)


class FakeInfo:
    def __init__(self, *, cloid_status=None) -> None:
        self.cloid_status = cloid_status

    def query_order_by_oid(self, account, oid):
        return {"status": "order", "order": {"status": "canceled", "order": {"oid": oid}}}

    def query_order_by_cloid(self, account, cloid):
        return self.cloid_status or {"status": "unknownOid"}


class FakeExchange:
    def __init__(
        self,
        *,
        market_error: str | None = None,
        market_exception: str | None = None,
        market_fill_price: str | None = None,
    ) -> None:
        self.market_error = market_error
        self.market_exception = market_exception
        self.market_fill_price = market_fill_price
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
                            {
                                "filled": {
                                    "oid": 22,
                                    "avgPx": self.market_fill_price or str(price),
                                    "totalSz": str(size),
                                }
                            }
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
            "economic_chain_id": "C2608031200AbCd",
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
            "economic_chain_id": "C2608031200AbCd",
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
        "p10_chain_realized_surplus": {"C2608031200AbCd": "10"},
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

    def test_long_better_fill_uses_one_gap_sell_fallback(self) -> None:
        row, _source = make_row()
        exchange = FakeExchange(market_fill_price="49.9")

        changed = lifecycle_process_p10(
            row, make_ctx(exchange), {"action_limit_headroom": 10}
        )

        self.assertTrue(changed)
        self.assertEqual(len(exchange.order_calls), 2)
        self.assertFalse(exchange.order_calls[1]["is_buy"])
        self.assertEqual(exchange.order_calls[1]["price"], 50.399)
        self.assertEqual(row["levels"][0]["price"], "50.399")
        self.assertNotIn("p10_promotion_intent", row)

    def test_short_better_fill_uses_one_gap_buy_fallback(self) -> None:
        row, _source = make_row(short=True)
        exchange = FakeExchange(market_fill_price="70.1")

        changed = lifecycle_process_p10(
            row, make_ctx(exchange, short=True), {"action_limit_headroom": 10}
        )

        self.assertTrue(changed)
        self.assertEqual(len(exchange.order_calls), 2)
        self.assertTrue(exchange.order_calls[1]["is_buy"])
        self.assertEqual(exchange.order_calls[1]["price"], 69.399)
        self.assertEqual(row["levels"][0]["price"], "69.399")
        self.assertNotIn("p10_promotion_intent", row)

    def test_legacy_waiting_intent_reconciles_fill_and_places_replacement(self) -> None:
        row, source = make_row()
        source.update(
            {
                "oid": None,
                "status": "p10_cancelled",
                "p10_intent_cloid": "0x00000000000000000000000000000001",
            }
        )
        row["p10_promotion_intent"] = {
            "cloid": source["p10_intent_cloid"],
            "status": "filled_waiting_for_replacement",
            "created_at": 90,
            "source_oid": 11,
            "source_price": "50",
            "source_size": "0.2",
            "source_grid_leg": 0,
            "source_iteration": 4,
            "market_is_buy": True,
            "economic_chain_id": source["economic_chain_id"],
        }
        exchange = FakeExchange()
        ctx = make_ctx(exchange)
        ctx["info"] = FakeInfo(
            cloid_status={
                "status": "order",
                "order": {"status": "filled", "order": {"oid": 22}},
            }
        )
        ctx["fills_by_oid"] = {22: {"px": "49.9", "sz": "0.2"}}

        changed = lifecycle_process_p10(row, ctx, {"action_limit_headroom": 10})

        self.assertTrue(changed)
        self.assertEqual(len(exchange.cancel_calls), 0)
        self.assertEqual(len(exchange.order_calls), 1)
        self.assertFalse(exchange.order_calls[0]["is_buy"])
        self.assertEqual(exchange.order_calls[0]["price"], 50.399)
        self.assertEqual(row["levels"][0]["oid"], 23)
        self.assertNotIn("p10_promotion_intent", row)

    def test_source_strictly_within_one_gap_skips_before_cancel(self) -> None:
        row, source = make_row()
        source["price"] = "59.5"
        exchange = FakeExchange()
        ctx = make_ctx(exchange)
        ctx["open_orders"][0]["limitPx"] = "59.5"

        with patch("trail_worker.audit_grid_action") as audit_mock:
            changed = lifecycle_process_p10(row, ctx, {"action_limit_headroom": 10})

        self.assertFalse(changed)
        self.assertEqual(exchange.cancel_calls, [])
        self.assertEqual(exchange.order_calls, [])
        self.assertEqual(source["status"], "active")
        self.assertEqual(row["p10_status"], "skipped_source_within_gap")
        self.assertEqual(row["p10_source_distance_rate"], "0.008333333333333333333333333333")
        audit_mock.assert_called_once()
        self.assertEqual(
            audit_mock.call_args.args[0], "grid_p10_source_within_gap_skipped"
        )

    def test_source_exactly_one_gap_still_promotes(self) -> None:
        row, source = make_row()
        source["price"] = "59.4"
        exchange = FakeExchange()
        ctx = make_ctx(exchange)
        ctx["open_orders"][0]["limitPx"] = "59.4"

        changed = lifecycle_process_p10(row, ctx, {"action_limit_headroom": 10})

        self.assertTrue(changed)
        self.assertEqual(len(exchange.cancel_calls), 1)
        self.assertEqual(len(exchange.order_calls), 2)
        self.assertEqual(row["p10_source_distance_rate"], "0.01")

    def test_new_zero_profit_chain_is_not_promoted(self) -> None:
        row, source = make_row()
        exchange = FakeExchange()
        ctx = make_ctx(exchange)
        ctx["p10_chain_realized_surplus"][source["economic_chain_id"]] = "0"

        with patch("trail_worker.audit_grid_action") as audit_mock:
            changed = lifecycle_process_p10(row, ctx, {"action_limit_headroom": 10})

        self.assertFalse(changed)
        self.assertEqual(exchange.cancel_calls, [])
        self.assertEqual(exchange.order_calls, [])
        self.assertEqual(row["p10_status"], "skipped_chain_profit")
        self.assertEqual(row["p10_chain_realized_surplus"], "0")
        self.assertEqual(row["p10_required_surplus"], "2.12")
        self.assertEqual(audit_mock.call_args.args[0], "grid_p10_chain_profit_skipped")

    def test_positive_but_insufficient_profit_chain_is_not_promoted(self) -> None:
        row, source = make_row()
        exchange = FakeExchange()
        ctx = make_ctx(exchange)
        ctx["p10_chain_realized_surplus"][source["economic_chain_id"]] = "2.12"

        changed = lifecycle_process_p10(row, ctx, {"action_limit_headroom": 10})

        self.assertFalse(changed)
        self.assertEqual(exchange.cancel_calls, [])
        self.assertEqual(exchange.order_calls, [])
        self.assertEqual(row["p10_status"], "skipped_chain_profit")

    def test_legacy_chain_profit_is_treated_as_zero(self) -> None:
        row, source = make_row()
        source["economic_chain_id"] = "7N5E1HuGi8"
        exchange = FakeExchange()
        ctx = make_ctx(exchange)
        ctx["p10_chain_realized_surplus"] = {"7N5E1HuGi8": "100"}

        changed = lifecycle_process_p10(row, ctx, {"action_limit_headroom": 10})

        self.assertFalse(changed)
        self.assertEqual(exchange.cancel_calls, [])
        self.assertEqual(row["p10_chain_realized_surplus"], "0")

    def test_profit_must_cover_chase_cost_and_gap_buffer(self) -> None:
        row, _source = make_row()
        exchange = FakeExchange()
        ctx = make_ctx(exchange)
        ctx["p10_chain_realized_surplus"]["C2608031200AbCd"] = "2.13"

        changed = lifecycle_process_p10(row, ctx, {"action_limit_headroom": 10})

        self.assertTrue(changed)
        self.assertEqual(len(exchange.cancel_calls), 1)
        self.assertEqual(row["p10_chain_realized_surplus"], "2.13")
        self.assertEqual(row["p10_required_surplus"], "2.12")

    def test_p10_allows_one_new_promotion_per_coin_per_round(self) -> None:
        btc_row, _ = make_row()
        duplicate_btc_row, _ = make_row()
        duplicate_btc_row["id"] = "grid-p10-btc-2"
        eth_row, _ = make_row()
        eth_row["id"] = "grid-p10-eth"
        eth_row["coin"] = "ETH"
        cache = {
            "grid_action_phase": GRID_LIFECYCLE_PHASE_P10,
            "action_limit_headroom": 100,
        }

        def context_for(row, _cache):
            ctx = make_ctx(FakeExchange())
            ctx["coin"] = row["coin"]
            return ctx

        with (
            patch("trail_worker.lifecycle_context", side_effect=context_for),
            patch("trail_worker.lifecycle_process_p10", return_value=True) as process_mock,
        ):
            maintain_grid(btc_row, cache)
            maintain_grid(duplicate_btc_row, cache)
            maintain_grid(eth_row, cache)

        self.assertEqual(process_mock.call_count, 2)
        self.assertEqual(
            cache["lifecycle_p10_coins"],
            {("mainnet", "0xabc", "BTC"), ("mainnet", "0xabc", "ETH")},
        )

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
