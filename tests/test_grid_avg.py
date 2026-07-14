import unittest
from argparse import Namespace
from decimal import Decimal
from unittest.mock import patch

from hl_order import (
    append_server_batch_or_cancel_orders,
    asset_requires_isolated_margin,
    build_grid_batch_row,
    build_grid_orders,
    cancel_order,
    ensure_no_duplicate_grid_batch,
    grid_avg_bounds,
    grid_avg_multiplier,
    grid_avg_size_pair,
    grid_avg_topup_params,
    grid_limit_display,
    grid_order_allowed_by_max,
    grid_order_should_reduce_only,
    format_grid_detail_rows,
    format_server_batch_rows,
    grid_query_avg_summary,
    grid_query_rows,
    is_cumulative_action_limit_text as hl_order_is_cumulative_action_limit_text,
    is_auto_grid_gap,
    order_result_is_retryable_action_limit,
    refresh_grid_row_strategy_params,
    resolve_grid_spacing,
    submit_with_action_limit_retry,
    successful_cancel_oids,
    update_order_leverage,
    user_action_rate_limit_metrics,
)
from trail_worker import (
    GridActionBudgetUnavailable,
    active_grid_oids,
    action_limit_p1_budget_for_deficit,
    action_limit_p1_budget_for_headroom,
    action_limit_p1_budget_remaining,
    batch_row_raw_coin,
    build_grid_panic_reduce_order,
    cancel_grid_entries_with_p1_budget,
    cancel_grid_entries,
    clear_stale_grid_margin_pauses,
    clear_grid_side_cap_entries,
    consume_action_limit_headroom,
    consume_action_limit_p1_budget,
    apply_grid_add_risk_brake,
    dense_grid_entries,
    defer_paused_grid_restore_if_crossing,
    enable_action_limit_p1_budget,
    grid_panic_ratio,
    grid_panic_ratio_threshold,
    grid_row_recoverable_from_error,
    grid_active_cap_restore_allowed,
    grid_active_cap_pause_candidates,
    replacement_active_cap_submit_allowed,
    grid_margin_gap_multiplier,
    grid_margin_pause_active,
    grid_missing_recovery_allowed,
    grid_order_status_is_cancelled,
    find_current_position_from_state,
    is_cumulative_action_limit_text,
    is_min_order_value_error_text,
    grid_order_entry,
    grid_replacement_rebalance_pair,
    grid_reduce_only_canceled_restore_without_reduce_only,
    grid_roe_add_risk_allowed,
    grid_roe_for_position_value,
    grid_latest_replacement_roe_allowed,
    grid_roe_pause_candidates,
    grid_roe_restore_allowed,
    grid_risk_density_pause_candidates,
    grid_risk_density_restore_allowed,
    maintain_grid,
    mark_pending_cancel_confirmed_cancelled,
    modify_trail_stop,
    move_grid_order_away_from_active,
    near_grid_orders_if_stale,
    next_depth_order,
    noncritical_grid_work_allowed,
    normalize_margin_paused_replacement,
    panic_reversal_order_from_reduce,
    pause_refresh_reduce_only_replacement,
    pause_reduce_only_canceled_entry,
    pause_refreshed_reduce_only_entries,
    pause_grid_order_for_action_limit,
    pause_grid_margin_side,
    pause_grid_margin_side_entries,
    pending_cancel_rate,
    pending_cancel_overflow_candidates,
    prepare_grid_cancel_entries,
    pause_skipped_account_margin_replacement,
    precheck_action_limit,
    prune_add_risk_brake_state,
    preserve_replacement_order,
    prune_grid_levels,
    regrid_dense_entries,
    reserve_grid_exchange_actions,
    replacement_order_from_fill,
    restore_pending_cancel_entries,
    run_once,
    skip_stale_grid_recovery,
    skip_unknown_oid_grid_recovery,
    submit_grid_order_entry,
    trim_excess_grid_entries,
    GRID_ACTION_LIMIT_PAUSE_STATUS,
    GRID_PENDING_CANCEL_STATUS,
    GRID_ROE_PAUSE_STATUS,
    grid_entries_fit_within_max,
)


class GridAvgTests(unittest.TestCase):
    def test_batch_persist_failure_cancels_submitted_orders(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.requests = []

            def bulk_cancel(self, requests: list[dict]) -> dict:
                self.requests.append(requests)
                return {"status": "ok", "response": {"data": {"statuses": ["success", "success"]}}}

        exchange = FakeExchange()
        with patch("hl_order.append_server_batch", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "submitted orders were cancelled"):
                append_server_batch_or_cancel_orders({}, exchange, "BTC", {22, 11})

        self.assertEqual(exchange.requests, [[{"coin": "BTC", "oid": 11}, {"coin": "BTC", "oid": 22}]])

    def test_batch_persist_failure_reports_unconfirmed_orders(self) -> None:
        class FakeExchange:
            def bulk_cancel(self, requests: list[dict]) -> dict:
                return {
                    "status": "ok",
                    "response": {"data": {"statuses": ["success", {"error": "order was already filled"}]}},
                }

        with patch("hl_order.append_server_batch", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RuntimeError, r"orders may still be active: \[22\].*already filled"):
                append_server_batch_or_cancel_orders({}, FakeExchange(), "BTC", {11, 22})

    def test_user_action_rate_limit_metrics_formats_deficit(self) -> None:
        class FakeInfo:
            def post(self, path: str, payload: dict) -> dict:
                if path != "/info" or payload.get("type") != "userRateLimit":
                    raise AssertionError("unexpected userRateLimit request")
                return {"nRequestsUsed": 209725, "nRequestsCap": 207711}

        self.assertEqual(
            user_action_rate_limit_metrics(FakeInfo(), "0xabc"),
            {"nRequestsUsed": "209725", "nRequestsCap": "207711", "deficit": "2014"},
        )

    def test_precheck_action_limit_blocks_p1_when_used_reaches_cap(self) -> None:
        class FakeInfo:
            def __init__(self) -> None:
                self.calls = 0

            def post(self, path: str, payload: dict) -> dict:
                self.calls += 1
                if path != "/info" or payload.get("type") != "userRateLimit":
                    raise AssertionError("unexpected userRateLimit request")
                return {"nRequestsUsed": 209689, "nRequestsCap": 207702}

        info = FakeInfo()
        cache: dict = {}
        with patch("trail_worker.random.random", return_value=0.99):
            error = precheck_action_limit(info, "0xabc", cache, "mainnet", 123)

        self.assertIn("deficit=1987", error or "")
        self.assertEqual(cache["action_limit_error"], error)
        self.assertEqual(cache["action_limit_at"], 123)
        self.assertEqual(action_limit_p1_budget_remaining(cache), 0)
        self.assertFalse(cache.get("action_limit_p1_enabled", False))
        enable_action_limit_p1_budget(cache)
        self.assertTrue(cache["action_limit_p1_enabled"])
        consume_action_limit_p1_budget(cache)
        self.assertEqual(action_limit_p1_budget_remaining(cache), 0)
        self.assertEqual(info.calls, 1)
        self.assertEqual(precheck_action_limit(info, "0xabc", cache, "mainnet", 124), error)
        self.assertEqual(info.calls, 1)

    def test_action_limit_p1_budget_for_deficit_edges_and_probability(self) -> None:
        self.assertEqual(action_limit_p1_budget_for_deficit(0), 1)
        self.assertEqual(action_limit_p1_budget_for_deficit(1), 1)
        self.assertEqual(action_limit_p1_budget_for_deficit(2), 1)
        with patch("trail_worker.random.random", return_value=0.99):
            self.assertEqual(action_limit_p1_budget_for_deficit(3), 0)
        with patch("trail_worker.random.random", return_value=0):
            self.assertEqual(action_limit_p1_budget_for_deficit(1779), 1)
        with patch("trail_worker.random.random", return_value=0.99):
            self.assertEqual(action_limit_p1_budget_for_deficit(1779), 0)

    def test_action_limit_p1_budget_for_headroom_leaves_one_request(self) -> None:
        self.assertEqual(action_limit_p1_budget_for_headroom(0), 1)
        self.assertEqual(action_limit_p1_budget_for_headroom(1), 1)
        self.assertEqual(action_limit_p1_budget_for_headroom(2), 1)
        self.assertEqual(action_limit_p1_budget_for_headroom(100), 99)

    def test_run_once_scans_all_grids_by_global_action_phase(self) -> None:
        rows = [
            {"type": "grid", "status": "active", "coin": "BTC", "levels": []},
            {"type": "grid", "status": "active", "coin": "ETH", "levels": []},
        ]
        calls = []

        def fake_maintain_grid(row: dict, cache: dict) -> tuple[dict, bool]:
            calls.append((row["coin"], cache.get("grid_action_phase")))
            return row, False

        with (
            patch("trail_worker.load_server_batch", return_value=rows),
            patch("trail_worker.maintain_grid", side_effect=fake_maintain_grid),
            patch("trail_worker.random.shuffle", side_effect=lambda indexes: indexes.reverse()),
            patch("trail_worker.prune_done_rows", return_value=(rows, False)),
            patch("trail_worker.prune_grid_level_history", return_value=False),
            patch("trail_worker.save_server_batch") as save_server_batch,
        ):
            run_once()

        self.assertEqual(
            calls,
            [
                ("ETH", "p0"),
                ("BTC", "p0"),
                ("ETH", "p1_latest_replacement"),
                ("BTC", "p1_latest_replacement"),
                ("ETH", "p1_paused_replacement"),
                ("BTC", "p1_paused_replacement"),
                ("ETH", "p1_cancels"),
                ("BTC", "p1_cancels"),
                ("ETH", "p1_topup"),
                ("BTC", "p1_topup"),
                ("ETH", "p1_restore"),
                ("BTC", "p1_restore"),
                ("ETH", "p2"),
                ("BTC", "p2"),
            ],
        )
        save_server_batch.assert_not_called()

    def test_run_once_reuses_read_caches_between_action_phases(self) -> None:
        rows = [{"type": "grid", "status": "active", "coin": "BTC", "levels": []}]
        seen = []

        class FakeInfo:
            def __init__(self) -> None:
                self.clear_calls = 0

            def clear_cache(self) -> None:
                self.clear_calls += 1

        info = FakeInfo()

        def fake_maintain_grid(row: dict, cache: dict) -> tuple[dict, bool]:
            seen.append((cache.get("user_states"), cache.get("mids"), cache.get("action_limit_p1_budget_remaining")))
            cache["user_states"] = {"stale": True}
            cache["account_margin_ratios"] = {"stale": True}
            cache["open_orders"] = {"stale": True}
            cache["fills"] = {"stale": True}
            cache.setdefault("mids", {})["shared"] = True
            cache.setdefault("action_limit_p1_budget_remaining", 3)
            cache.setdefault("clients", {})["shared"] = (info, None, "account", None, None)
            return row, False

        with (
            patch("trail_worker.load_server_batch", return_value=rows),
            patch("trail_worker.maintain_grid", side_effect=fake_maintain_grid),
            patch("trail_worker.random.shuffle"),
            patch("trail_worker.prune_done_rows", return_value=(rows, False)),
            patch("trail_worker.prune_grid_level_history", return_value=False),
        ):
            run_once()

        self.assertEqual(seen[0], (None, None, None))
        self.assertEqual(seen[1], ({"stale": True}, {"shared": True}, 3))
        self.assertEqual(info.clear_calls, 7)

    def test_precheck_action_limit_initializes_shared_p1_budget_below_cap_once(self) -> None:
        class FakeInfo:
            def __init__(self) -> None:
                self.calls = 0

            def post(self, path: str, payload: dict) -> dict:
                self.calls += 1
                if path != "/info" or payload.get("type") != "userRateLimit":
                    raise AssertionError("unexpected userRateLimit request")
                return {"nRequestsUsed": 100, "nRequestsCap": 105}

        info = FakeInfo()
        cache: dict = {}

        self.assertIsNone(precheck_action_limit(info, "0xabc", cache, "mainnet", 123))
        self.assertEqual(action_limit_p1_budget_remaining(cache), 4)
        consume_action_limit_p1_budget(cache)
        self.assertEqual(action_limit_p1_budget_remaining(cache), 3)
        self.assertIsNone(precheck_action_limit(info, "0xabc", cache, "mainnet", 124))

        self.assertEqual(action_limit_p1_budget_remaining(cache), 3)
        self.assertEqual(info.calls, 1)

    def test_action_limit_p1_budget_gates_cancels(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.requests = []

            def bulk_cancel(self, requests: list[dict]) -> dict:
                self.requests.append(requests)
                return {"status": "ok", "response": {"data": {"statuses": ["success"] * len(requests)}}}

        exchange = FakeExchange()
        entries = [{"oid": 1, "status": "active"}, {"oid": 2, "status": "active"}]
        cache = {
            "action_limit_error": "address action limit exhausted",
            "action_limit_p1_budget_remaining": 1,
        }

        self.assertEqual(cancel_grid_entries_with_p1_budget(exchange, "BTC", entries, 123, "paused_limit", cache), 0)
        self.assertEqual(exchange.requests, [])
        enable_action_limit_p1_budget(cache)
        self.assertEqual(cancel_grid_entries_with_p1_budget(exchange, "BTC", entries, 124, "paused_limit", cache), 1)

        self.assertEqual(exchange.requests, [[{"coin": "BTC", "oid": 1}]])
        self.assertEqual(entries[0]["status"], "paused_limit")
        self.assertEqual(entries[1]["status"], "active")
        self.assertEqual(action_limit_p1_budget_remaining(cache), 0)

    def test_cancel_grid_entries_marks_only_item_successes(self) -> None:
        class FakeExchange:
            def bulk_cancel(self, requests: list[dict]) -> dict:
                return {
                    "status": "ok",
                    "response": {"data": {"statuses": ["success", {"error": "order was already filled"}]}},
                }

        entries = [{"oid": 1, "status": "active"}, {"oid": 2, "status": "active"}]

        self.assertEqual(cancel_grid_entries(FakeExchange(), "BTC", entries, 123, "paused_limit"), 1)
        self.assertEqual(entries[0]["status"], "paused_limit")
        self.assertEqual(entries[1]["status"], "active")

    def test_cancel_statuses_missing_or_wrong_length_confirm_nothing(self) -> None:
        requests = [{"coin": "BTC", "oid": 1}, {"coin": "BTC", "oid": 2}]

        missing = successful_cancel_oids({"status": "ok"}, requests)
        mismatched = successful_cancel_oids(
            {"status": "ok", "response": {"data": {"statuses": ["success"]}}},
            requests,
        )

        self.assertEqual(missing[0], set())
        self.assertIn("did not include statuses", missing[1][0])
        self.assertEqual(mismatched[0], set())
        self.assertIn("did not match requests length", mismatched[1][0])

    def test_regular_bulk_cancel_marks_only_successful_batch_oids(self) -> None:
        class FakeExchange:
            def bulk_cancel(self, requests: list[dict]) -> dict:
                return {
                    "status": "ok",
                    "response": {"data": {"statuses": ["success", {"error": "order was already filled"}]}},
                }

        orders = [
            {"coin": "BTC", "side": "B", "oid": 1, "limitPx": "100", "origSz": "1"},
            {"coin": "BTC", "side": "A", "oid": 2, "limitPx": "101", "origSz": "1"},
        ]
        with (
            patch("hl_order.collect_frontend_open_orders", return_value=orders),
            patch("hl_order.mark_cancelled_server_batch_oids", return_value=1) as mark_cancelled,
            patch("hl_order.print_account_metrics"),
            patch("hl_order.print_box"),
            patch("hl_order.print_order_row"),
            patch("builtins.print"),
        ):
            cancel_order(FakeExchange(), object(), "account", "mainnet", "BTC", "", "all", False)

        mark_cancelled.assert_called_once()
        self.assertEqual(mark_cancelled.call_args.args[2], {1})

    def test_tracked_p1_budget_gates_cancels_below_cap(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.requests = []

            def bulk_cancel(self, requests: list[dict]) -> dict:
                self.requests.append(requests)
                return {"status": "ok", "response": {"data": {"statuses": ["success"] * len(requests)}}}

        exchange = FakeExchange()
        entries = [{"oid": 1, "status": "active"}]
        cache = {
            "action_limit_p1_budget_remaining": 0,
            "action_limit_p1_budget_initialized": True,
        }
        enable_action_limit_p1_budget(cache)

        self.assertEqual(cancel_grid_entries_with_p1_budget(exchange, "BTC", entries, 123, "paused_limit", cache), 0)
        self.assertEqual(exchange.requests, [])
        self.assertEqual(entries[0]["status"], "active")

    def test_p2_work_requires_more_than_one_hundred_headroom(self) -> None:
        self.assertTrue(noncritical_grid_work_allowed({"action_limit_headroom": 101}))
        self.assertFalse(noncritical_grid_work_allowed({"action_limit_headroom": 100}))
        self.assertFalse(
            noncritical_grid_work_allowed(
                {"action_limit_error": "address action limit exhausted", "action_limit_headroom": 1000}
            )
        )

    def test_p1_consumption_reduces_p2_headroom(self) -> None:
        cache = {"action_limit_headroom": 101}

        consume_action_limit_headroom(cache)

        self.assertEqual(cache["action_limit_headroom"], 100)
        self.assertFalse(noncritical_grid_work_allowed(cache))

    def test_p0_reservation_pre_deducts_headroom_and_clamps_p1_budget(self) -> None:
        cache = {
            "action_limit_headroom": 5,
            "action_limit_p1_budget_remaining": 4,
            "action_limit_p1_enabled": True,
        }

        reserve_grid_exchange_actions(cache)

        self.assertEqual(cache["action_limit_headroom"], 4)
        self.assertEqual(action_limit_p1_budget_remaining(cache), 3)

    def test_p1_reservation_pre_deducts_both_budgets_once(self) -> None:
        cache = {
            "action_limit_headroom": 5,
            "action_limit_p1_budget_remaining": 4,
            "action_limit_p1_enabled": True,
        }

        reserve_grid_exchange_actions(cache, consume_p1_budget=True)

        self.assertEqual(cache["action_limit_headroom"], 4)
        self.assertEqual(action_limit_p1_budget_remaining(cache), 3)

    def test_p1_reservation_rejects_before_exchange_when_budget_is_empty(self) -> None:
        cache = {
            "action_limit_headroom": 5,
            "action_limit_p1_budget_remaining": 0,
            "action_limit_p1_enabled": True,
        }

        with self.assertRaises(GridActionBudgetUnavailable):
            reserve_grid_exchange_actions(cache, consume_p1_budget=True)

        self.assertEqual(cache["action_limit_headroom"], 5)
        self.assertEqual(action_limit_p1_budget_remaining(cache), 0)

    def test_bulk_cancel_pre_deducts_every_address_action(self) -> None:
        class FakeExchange:
            def bulk_cancel(self, requests: list[dict]) -> dict:
                return {
                    "status": "ok",
                    "response": {"data": {"statuses": ["success"] * len(requests)}},
                }

        entries = [{"oid": 1, "status": "active"}, {"oid": 2, "status": "active"}]
        cache = {
            "action_limit_headroom": 5,
            "action_limit_p1_budget_remaining": 4,
            "action_limit_p1_enabled": True,
        }

        self.assertEqual(
            cancel_grid_entries(
                FakeExchange(),
                "BTC",
                entries,
                123,
                "paused_limit",
                cache=cache,
            ),
            2,
        )

        self.assertEqual(cache["action_limit_headroom"], 3)
        self.assertEqual(action_limit_p1_budget_remaining(cache), 2)

    def test_cumulative_action_limit_text_is_recognized(self) -> None:
        text = (
            "Failed to submit grid child order: {'status': 'err', 'response': "
            "'Too many cumulative requests sent (208641 > 207574) for cumulative volume traded $197575. "
            "Place taker orders to free up 1 request per USDC traded.'}"
        )
        self.assertTrue(is_cumulative_action_limit_text(text))
        self.assertFalse(is_cumulative_action_limit_text("Too many requests"))

    def test_manual_order_action_limit_text_is_recognized(self) -> None:
        text = (
            "Too many cumulative requests sent (211928 > 211236) for cumulative volume traded $201237.65. "
            "Place taker orders to free up 1 request per USDC traded."
        )
        self.assertTrue(hl_order_is_cumulative_action_limit_text(text))
        self.assertFalse(hl_order_is_cumulative_action_limit_text("Too many requests"))

    def test_manual_order_action_limit_retry_retries_then_succeeds(self) -> None:
        action_limit_result = {
            "status": "err",
            "response": (
                "Too many cumulative requests sent (211928 > 211236) for cumulative volume traded $201237.65. "
                "Place taker orders to free up 1 request per USDC traded."
            ),
        }
        ok_result = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}
        results = [action_limit_result, action_limit_result, ok_result]
        calls = []

        def submit():
            calls.append(1)
            return results.pop(0)

        with patch("hl_order.time.sleep") as sleep:
            self.assertEqual(
                submit_with_action_limit_retry(submit, "test", max_retries=3, retry_delay_seconds=10),
                ok_result,
            )

        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(10)

    def test_manual_bulk_partial_success_action_limit_does_not_retry(self) -> None:
        result = {
            "status": "ok",
            "response": {
                "data": {
                    "statuses": [
                        {"resting": {"oid": 1}},
                        {
                            "error": (
                                "Too many cumulative requests sent (211928 > 211236) "
                                "for cumulative volume traded $201237.65."
                            )
                        },
                    ]
                }
            },
        }
        self.assertFalse(order_result_is_retryable_action_limit(result))

    def test_manual_bulk_all_action_limit_errors_are_retryable(self) -> None:
        result = {
            "status": "ok",
            "response": {
                "data": {
                    "statuses": [
                        {
                            "error": (
                                "Too many cumulative requests sent (211928 > 211236) "
                                "for cumulative volume traded $201237.65."
                            )
                        },
                        {
                            "error": (
                                "Too many cumulative requests sent (211928 > 211236) "
                                "for cumulative volume traded $201237.65."
                            )
                        },
                    ]
                }
            },
        }
        self.assertTrue(order_result_is_retryable_action_limit(result))

    def test_trail_modify_keeps_active_on_response_action_limit(self) -> None:
        class FakeExchange:
            def modify_order(self, *args, **kwargs):
                return {
                    "status": "ok",
                    "response": {"data": {"statuses": [{"error": "Too many requests: action limit reached"}]}},
                }

        row = {
            "network": "mainnet",
            "coin": "BTC",
            "oid": 1,
            "is_buy": False,
            "side": "A",
            "amount": "10",
            "size": "1",
            "best_px": "100",
            "stop_px": "95",
            "trail_distance": "5",
            "status": "active",
        }
        plan = {"size": Decimal("1"), "limit_px": Decimal("96"), "order_type": {"trigger": {}}}
        with (
            patch("trail_worker.build_clients", return_value=(object(), FakeExchange(), "account", None, None)),
            patch("trail_worker.resolve_perp_asset", return_value=("BTC", {"szDecimals": 2})),
            patch("trail_worker.find_open_order_by_oid", return_value={"oid": 1}),
            patch("trail_worker.build_trigger_order_plan", return_value=plan),
        ):
            updated, changed = modify_trail_stop(row, Decimal("101"))

        self.assertTrue(changed)
        self.assertEqual(updated["status"], "active")
        self.assertIn("action limit", updated["last_error"])
        self.assertEqual(updated["stop_px"], "95")

    def test_trail_modify_marks_permanent_response_error(self) -> None:
        class FakeExchange:
            def modify_order(self, *args, **kwargs):
                return {
                    "status": "ok",
                    "response": {"data": {"statuses": [{"error": "invalid trigger price"}]}},
                }

        row = {
            "network": "mainnet",
            "coin": "BTC",
            "oid": 1,
            "is_buy": False,
            "side": "A",
            "amount": "10",
            "size": "1",
            "best_px": "100",
            "stop_px": "95",
            "trail_distance": "5",
            "status": "active",
        }
        plan = {"size": Decimal("1"), "limit_px": Decimal("96"), "order_type": {"trigger": {}}}
        with (
            patch("trail_worker.build_clients", return_value=(object(), FakeExchange(), "account", None, None)),
            patch("trail_worker.resolve_perp_asset", return_value=("BTC", {"szDecimals": 2})),
            patch("trail_worker.find_open_order_by_oid", return_value={"oid": 1}),
            patch("trail_worker.build_trigger_order_plan", return_value=plan),
        ):
            updated, changed = modify_trail_stop(row, Decimal("101"))

        self.assertTrue(changed)
        self.assertEqual(updated["status"], "error")
        self.assertIn("invalid trigger price", updated["error"])

    def test_min_trade_notional_rejected_is_min_order_value_error(self) -> None:
        self.assertTrue(is_min_order_value_error_text("minTradeNtlRejected"))

    def test_action_limit_defer_keeps_order_status_and_oid(self) -> None:
        order = {"status": "active", "oid": 123, "side": "buy"}
        pause_grid_order_for_action_limit(order, 456, "Too many cumulative requests sent", old_oid=123)
        self.assertEqual(order["status"], "active")
        self.assertEqual(order["oid"], 123)
        self.assertEqual(order["action_limit_deferred_at"], 456)
        self.assertEqual(order["action_limit_deferred_status"], "active")
        self.assertEqual(order["action_limit_deferred_oid"], 123)

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

    def test_worker_uses_row_dex_for_legacy_raw_coin(self) -> None:
        row = {"coin": "xyz:JPY", "raw_coin": "JPY", "dex": "xyz"}

        self.assertEqual(batch_row_raw_coin(row), "xyz:JPY")

    def test_worker_prefers_resolved_coin_over_legacy_alias(self) -> None:
        row = {"coin": "xyz:XYZ100", "raw_coin": "QQQ", "dex": "xyz"}

        self.assertEqual(batch_row_raw_coin(row), "xyz:XYZ100")

    def test_legacy_dex_raw_coin_error_is_recoverable(self) -> None:
        row = {
            "type": "grid",
            "status": "error",
            "coin": "xyz:JPY",
            "raw_coin": "JPY",
            "dex": "xyz",
            "error": "Unknown perp coin: JPY",
        }

        self.assertTrue(grid_row_recoverable_from_error(row))

    def test_bare_coin_key_error_is_recoverable(self) -> None:
        row = {
            "type": "grid",
            "status": "error",
            "coin": "xyz:SPCX",
            "raw_coin": "xyz:SPCX",
            "dex": "xyz",
            "error": "'xyz:SPCX'",
            "note": "grid maintained before the old key error",
        }

        self.assertTrue(grid_row_recoverable_from_error(row))

    def test_isolated_opening_leverage_error_is_recoverable(self) -> None:
        row = {
            "type": "grid",
            "status": "error",
            "coin": "xyz:JPY",
            "raw_coin": "JPY",
            "dex": "xyz",
            "error": "Failed to set isolated opening leverage to 5x for xyz:JPY; order was not submitted.",
        }

        self.assertTrue(grid_row_recoverable_from_error(row))

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
        self.assertEqual(grid_panic_ratio_threshold({}), Decimal("100"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "10"}), Decimal("100"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "20"}), Decimal("100"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "30"}), Decimal("100"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "50"}), Decimal("100"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "60"}), Decimal("100"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "65"}), Decimal("100"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "70"}), Decimal("100"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "75"}), Decimal("100"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "80"}), Decimal("100"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "85"}), Decimal("100"))
        self.assertEqual(grid_panic_ratio_threshold({"panic_ratio_threshold": "15"}), Decimal("15"))

    def test_panic_reduce_order_uses_base_size_ioc_and_reduce_only(self) -> None:
        class FakeExchange:
            def _slippage_price(self, coin, is_buy, slippage, reference_price):
                self.args = (coin, is_buy, Decimal(str(slippage)), Decimal(str(reference_price)))
                return reference_price * (1.001 if is_buy else 0.999)

        row = {
            "base_buy_size": "0.20",
            "base_sell_size": "0.20",
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
        self.assertEqual(order["size"], "0.2")
        self.assertTrue(order["reduce_only"])
        self.assertEqual(order["plan"]["order_type"], {"limit": {"tif": "Ioc"}})
        self.assertTrue(order["plan"]["reduce_only"])

    def test_panic_reduce_order_uses_min_notional_buffer(self) -> None:
        class FakeExchange:
            def _slippage_price(self, coin, is_buy, slippage, reference_price):
                return Decimal("65483.0")

        row = {
            "base_buy_size": "0.00016",
            "base_sell_size": "0.00016",
            "slippage": "0.001",
            "sz_decimals": 5,
        }

        order = build_grid_panic_reduce_order(
            FakeExchange(),
            row,
            "BTC",
            {"szDecimals": 5},
            Decimal("65500"),
            Decimal("-0.001"),
        )

        self.assertIsNotNone(order)
        self.assertEqual(order["size"], "0.00017")
        self.assertGreaterEqual(Decimal(str(order["plan"]["notional"])), Decimal("11"))

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
        self.assertEqual(order["price"], "102.71")
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
        self.assertEqual(order["price"], "97.29")
        self.assertFalse(order["reduce_only"])
        self.assertTrue(order["replacement_order"])
        self.assertTrue(order["panic_reversal_order"])
        self.assertEqual(order["plan"]["label"], "grid-panic-reversal")
        self.assertEqual(order["plan"]["grid_gap"], Decimal("0.01"))

    def test_panic_reversal_submits_without_restore_limit_checks(self) -> None:
        class FakeInfo:
            def post(self, path, payload):
                return {"nRequestsUsed": 0, "nRequestsCap": 1000}

            def meta(self, dex=""):
                return {"universe": [{"name": "BTC", "szDecimals": 2, "maxLeverage": 20}]}

            def all_mids(self, dex=""):
                return {"BTC": "100"}

            def l2_snapshot(self, coin):
                return {"levels": [[{"px": "99"}], [{"px": "101"}]]}

            def user_state(self, account, dex=""):
                return {
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "BTC",
                                "szi": "-4",
                                "positionValue": "400",
                                "liquidationPx": "130",
                                "returnOnEquity": "-0.50",
                            }
                        }
                    ]
                }

            def spot_user_state(self, account):
                return {
                    "balances": [{"token": 0, "coin": "USDC", "total": "100"}],
                    "tokenToAvailableAfterMaintenance": [[0, "10"]],
                }

            def frontend_open_orders(self, account, dex=""):
                return [{"coin": "BTC", "oid": 1}]

            def user_fills_by_time(self, account, start_ms, end_ms):
                return []

        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def _slippage_price(self, coin, is_buy, slippage, reference_price):
                side_factor = Decimal("1.001") if is_buy else Decimal("0.999")
                return Decimal(str(reference_price)) * side_factor

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                oid = 10 if len(self.orders) == 1 else 11
                status_key = "filled" if len(self.orders) == 1 else "resting"
                return {"status": "ok", "response": {"data": {"statuses": [{status_key: {"oid": oid}}]}}}

        info = FakeInfo()
        exchange = FakeExchange()
        row = {
            "coin": "BTC",
            "network": "mainnet",
            "gap_rate": "0.01",
            "min_order_value": "10",
            "max_position_value": "1",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "slippage": "0.001",
            "sz_decimals": 2,
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "90", "size": "1", "reduce_only": True}
            ],
        }

        with patch("trail_worker.build_clients", return_value=(info, exchange, "acct", "signer", {})):
            updated, changed = maintain_grid(row, {"now": 123, "grid_action_phase": "p0"})

        self.assertTrue(changed)
        self.assertEqual(len(exchange.orders), 2)
        self.assertEqual(exchange.orders[0][1], True)
        self.assertTrue(exchange.orders[0][4])
        self.assertEqual(exchange.orders[1][1], False)
        self.assertFalse(exchange.orders[1][4])
        self.assertEqual(exchange.orders[1][2], Decimal("102.71"))
        reversal = next(entry for entry in updated["levels"] if entry.get("panic_reversal_order"))
        self.assertEqual(reversal["status"], "active")
        self.assertEqual(reversal["oid"], 11)

    def test_panic_reversal_waits_and_retries_action_limit_once(self) -> None:
        class FakeInfo:
            def post(self, path, payload):
                return {"nRequestsUsed": 9, "nRequestsCap": 10}

            def meta(self, dex=""):
                return {"universe": [{"name": "BTC", "szDecimals": 2, "maxLeverage": 20}]}

            def all_mids(self, dex=""):
                return {"BTC": "100"}

            def l2_snapshot(self, coin):
                return {"levels": [[{"px": "99"}], [{"px": "101"}]]}

            def user_state(self, account, dex=""):
                return {
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "BTC",
                                "szi": "-4",
                                "positionValue": "400",
                                "liquidationPx": "130",
                                "returnOnEquity": "-0.50",
                            }
                        }
                    ]
                }

            def spot_user_state(self, account):
                return {
                    "balances": [{"token": 0, "coin": "USDC", "total": "100"}],
                    "tokenToAvailableAfterMaintenance": [[0, "100"]],
                }

            def frontend_open_orders(self, account, dex=""):
                return [{"coin": "BTC", "oid": 1}]

            def user_fills_by_time(self, account, start_ms, end_ms):
                return []

        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def _slippage_price(self, coin, is_buy, slippage, reference_price):
                side_factor = Decimal("1.001") if is_buy else Decimal("0.999")
                return Decimal(str(reference_price)) * side_factor

            def order(self, coin, is_buy, size, limit_px, order_type, reduce_only=False):
                self.orders.append((coin, is_buy, Decimal(str(limit_px)), order_type, reduce_only))
                if len(self.orders) == 1:
                    return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 10}}]}}}
                if len(self.orders) == 2:
                    return {
                        "status": "err",
                        "response": (
                            "Too many cumulative requests sent (10 > 9) for cumulative volume traded $100. "
                            "Place taker orders to free up 1 request per USDC traded."
                        ),
                    }
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 11}}]}}}

        info = FakeInfo()
        exchange = FakeExchange()
        row = {
            "coin": "BTC",
            "network": "mainnet",
            "gap_rate": "0.01",
            "min_order_value": "10",
            "max_position_value": "1",
            "base_buy_size": "1",
            "base_sell_size": "1",
            "slippage": "0.001",
            "sz_decimals": 2,
            "levels": [
                {"side": "buy", "status": "active", "oid": 1, "price": "90", "size": "1", "reduce_only": True}
            ],
        }

        with (
            patch("trail_worker.build_clients", return_value=(info, exchange, "acct", "signer", {})),
            patch("trail_worker.time.sleep") as sleep_mock,
        ):
            updated, changed = maintain_grid(row, {"now": 123, "grid_action_phase": "p0"})

        self.assertTrue(changed)
        sleep_mock.assert_called_once_with(10)
        self.assertEqual(len(exchange.orders), 3)
        reversal = next(entry for entry in updated["levels"] if entry.get("panic_reversal_order"))
        self.assertEqual(reversal["status"], "active")
        self.assertEqual(reversal["oid"], 11)
        self.assertEqual(reversal["panic_reversal_action_limit_wait_seconds"], 10)
        self.assertEqual(reversal["panic_reversal_action_limit_wait_at"], 123)

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
                return {"status": "ok", "response": {"data": {"statuses": ["success"] * len(requests)}}}

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
                return {"status": "ok", "response": {"data": {"statuses": ["success"] * len(requests)}}}

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

    def test_roe_controls_require_position_value_strictly_above_one_hundred(self) -> None:
        roe = Decimal("-0.50")

        self.assertIsNone(grid_roe_for_position_value(Decimal("99.99"), roe))
        self.assertIsNone(grid_roe_for_position_value(Decimal("100"), roe))
        self.assertIsNone(grid_roe_for_position_value(Decimal("-100"), roe))
        self.assertEqual(grid_roe_for_position_value(Decimal("100.01"), roe), roe)
        self.assertEqual(grid_roe_for_position_value(Decimal("-100.01"), roe), roe)

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

    def test_roe_pause_candidates_do_not_pause_without_compressed_roe(self) -> None:
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
                for oid, price in enumerate(range(100, 88, -1), start=1)
            ]
        }

        without_value_candidates, without_value_allowed = grid_roe_pause_candidates(
            row, "buy", Decimal("2"), 10, None
        )
        positive_candidates, positive_allowed = grid_roe_pause_candidates(
            row, "buy", Decimal("2"), 10, Decimal("0.15")
        )

        self.assertEqual(without_value_candidates, [])
        self.assertEqual(without_value_allowed, 10)
        self.assertEqual(positive_candidates, [])
        self.assertEqual(positive_allowed, 10)

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

    def test_latest_replacement_bypasses_target_side_cap_when_roe_is_not_compressed(self) -> None:
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
        replacement = {"side": "buy", "is_buy": True, "price": "90", "size": "1", "replacement_order": True}

        self.assertFalse(grid_roe_restore_allowed(row, replacement, "buy", Decimal("2"), 10, Decimal("0.15")))
        self.assertTrue(
            grid_latest_replacement_roe_allowed(row, replacement, "buy", Decimal("2"), 10, Decimal("0.15"))
        )
        self.assertTrue(
            grid_latest_replacement_roe_allowed(row, replacement, "buy", Decimal("2"), 10, Decimal("-0.10"))
        )
        self.assertFalse(
            grid_latest_replacement_roe_allowed(row, replacement, "buy", Decimal("2"), 10, Decimal("-0.25"))
        )

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

    def test_active_cap_pauses_logarithmically_beyond_sixteen_active_orders(self) -> None:
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

        self.assertEqual(allowed, 16)
        self.assertEqual(
            [entry["oid"] for entry in candidates],
            [10, 12, 13, 15, 16, 17, 19, 20, 21, 22, 24, 25, 26, 27, 28, 29, 31, 32, 33, 34, 35, 36, 37, 38],
        )

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
                for oid in range(16)
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

        self.assertEqual(allowed, 16)
        self.assertNotIn(999, [entry["oid"] for entry in candidates])
        self.assertEqual(len(candidates), 1)

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
        keep_active = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 14, 18, 23, 30}
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
        should_wait = row["levels"][10]
        should_restore = row["levels"][39]

        self.assertFalse(grid_active_cap_restore_allowed(row, should_wait, "sell"))
        self.assertTrue(grid_active_cap_restore_allowed(row, should_restore, "sell"))

    def test_replacement_waits_until_active_side_is_at_most_32(self) -> None:
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
                for oid in range(33)
            ]
        }

        self.assertFalse(replacement_active_cap_submit_allowed(row, "sell"))
        row["levels"].pop()
        self.assertTrue(replacement_active_cap_submit_allowed(row, "sell"))

    def test_missing_recovery_requires_fewer_than_sixteen_active_orders(self) -> None:
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
                for oid in range(16)
            ]
        }

        self.assertFalse(grid_missing_recovery_allowed(row, "sell", set(range(16))))
        self.assertTrue(grid_missing_recovery_allowed(row, "sell", set(range(15))))
        row["levels"].pop()
        self.assertTrue(grid_missing_recovery_allowed(row, "sell", set(range(15))))

    def test_pending_cancel_rate_uses_special_one_and_deficit_floor(self) -> None:
        self.assertIsNone(pending_cancel_rate(0))
        self.assertEqual(pending_cancel_rate(1), Decimal("0.20"))
        self.assertEqual(pending_cancel_rate(100), Decimal("0.02"))
        self.assertEqual(pending_cancel_rate(1000000), Decimal("0.01"))

    def test_pending_cancel_overflow_selects_farthest_live_orders_above_thirty_two(self) -> None:
        row = {
            "levels": [
                {
                    "side": "sell",
                    "status": GRID_PENDING_CANCEL_STATUS if oid >= 30 else "active",
                    "oid": oid,
                    "price": str(100 + oid),
                    "size": "1",
                }
                for oid in range(35)
            ]
        }

        candidates = pending_cancel_overflow_candidates(row, set(range(35)))

        self.assertEqual([entry["oid"] for entry in candidates], [34, 33, 32])
        self.assertEqual(pending_cancel_overflow_candidates(row, set(range(32))), [])

    def test_far_cancel_becomes_pending_without_exchange_request(self) -> None:
        row = {"coin": "BTC", "levels": []}
        entry = {
            "side": "sell",
            "status": "active",
            "oid": 123,
            "price": "120",
            "size": "1",
        }
        row["levels"].append(entry)
        cache = {"action_limit_deficit": 100}

        immediate, deferred = prepare_grid_cancel_entries(
            row, [entry], 10, "paused_limit", Decimal("100"), cache
        )

        self.assertEqual(immediate, [])
        self.assertEqual(deferred, 1)
        self.assertEqual(entry["status"], GRID_PENDING_CANCEL_STATUS)
        self.assertEqual(entry["pending_cancel_reason"], "paused_limit")

    def test_pending_cancel_restores_locally_when_price_returns(self) -> None:
        row = {
            "coin": "BTC",
            "levels": [
                {
                    "side": "sell",
                    "status": GRID_PENDING_CANCEL_STATUS,
                    "oid": 123,
                    "price": "101",
                    "size": "1",
                    "pending_cancel_reason": "paused_limit",
                    "pending_cancel_at": 1,
                }
            ],
        }

        restored = restore_pending_cancel_entries(row, Decimal("100"), {"action_limit_deficit": 100}, 10)

        self.assertEqual(restored, 1)
        self.assertEqual(row["levels"][0]["status"], "active")
        self.assertNotIn("pending_cancel_reason", row["levels"][0])

    def test_pending_cancel_confirmed_cancelled_becomes_history(self) -> None:
        entry = {
            "side": "sell",
            "status": GRID_PENDING_CANCEL_STATUS,
            "oid": 123,
            "price": "120",
            "pending_cancel_reason": "paused_limit",
            "pending_cancel_at": 1,
        }

        changed = mark_pending_cancel_confirmed_cancelled(
            entry,
            123,
            10,
            {"order": {"status": "canceled"}},
        )

        self.assertTrue(changed)
        self.assertEqual(entry["status"], "cancelled")
        self.assertIsNone(entry["oid"])
        self.assertEqual(entry["cancelled_oid"], 123)
        self.assertEqual(entry["exchange_cancel_status"], "canceled")
        self.assertNotIn("pending_cancel_reason", entry)

    def test_pending_cancel_does_not_treat_filled_or_unknown_as_cancelled(self) -> None:
        self.assertTrue(grid_order_status_is_cancelled({"status": "reduceOnlyCanceled"}))
        self.assertTrue(grid_order_status_is_cancelled({"status": "scheduledCancel"}))
        self.assertFalse(grid_order_status_is_cancelled({"status": "filled"}))
        self.assertFalse(grid_order_status_is_cancelled({"status": "unknownOid"}))

        entry = {"status": GRID_PENDING_CANCEL_STATUS, "oid": 123}
        self.assertFalse(mark_pending_cancel_confirmed_cancelled(entry, 123, 10, {"status": "filled"}))
        self.assertEqual(entry["status"], GRID_PENDING_CANCEL_STATUS)

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
                return {"status": "ok", "response": {"data": {"statuses": ["success"] * len(requests)}}}

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
                return {"status": "ok", "response": {"data": {"statuses": ["success"] * len(requests)}}}

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
                return {"status": "ok", "response": {"data": {"statuses": ["success"] * len(requests)}}}

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

    def test_update_order_leverage_skips_matching_cross_position(self) -> None:
        class FakeInfo:
            def user_state(self, account: str, dex: str = "") -> dict:
                return {
                    "assetPositions": [
                        {"position": {"coin": "BTC", "leverage": {"type": "cross", "value": 40}}},
                    ]
                }

        class FakeExchange:
            def __init__(self) -> None:
                self.calls = []

            def update_leverage(self, leverage: int, coin: str, is_cross: bool) -> dict:
                self.calls.append((leverage, coin, is_cross))
                return {"status": "ok"}

        exchange = FakeExchange()

        mode, result = update_order_leverage(exchange, 40, "BTC", FakeInfo(), "account")

        self.assertEqual(mode, "cross")
        self.assertTrue(result["skipped"])
        self.assertEqual(exchange.calls, [])

    def test_update_order_leverage_skips_matching_isolated_position(self) -> None:
        class FakeInfo:
            def user_state(self, account: str, dex: str = "") -> dict:
                return {
                    "assetPositions": [
                        {"position": {"coin": "xyz:JPY", "leverage": {"type": "isolated", "value": 5}}},
                    ]
                }

        class FakeExchange:
            def __init__(self) -> None:
                self.calls = []

            def update_leverage(self, leverage: int, coin: str, is_cross: bool) -> dict:
                self.calls.append((leverage, coin, is_cross))
                return {"status": "ok"}

        exchange = FakeExchange()

        mode, result = update_order_leverage(exchange, 20, "xyz:JPY", FakeInfo(), "account", "xyz")

        self.assertEqual(mode, "isolated")
        self.assertTrue(result["skipped"])
        self.assertEqual(exchange.calls, [])

    def test_update_order_leverage_updates_when_position_mismatches(self) -> None:
        class FakeInfo:
            def user_state(self, account: str, dex: str = "") -> dict:
                return {
                    "assetPositions": [
                        {"position": {"coin": "BTC", "leverage": {"type": "cross", "value": 20}}},
                    ]
                }

        class FakeExchange:
            def __init__(self) -> None:
                self.calls = []

            def update_leverage(self, leverage: int, coin: str, is_cross: bool) -> dict:
                self.calls.append((leverage, coin, is_cross))
                return {"status": "ok"}

        exchange = FakeExchange()

        mode, result = update_order_leverage(exchange, 40, "BTC", FakeInfo(), "account")

        self.assertEqual(mode, "cross")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(exchange.calls, [(40, "BTC", True)])

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

    def test_duplicate_paused_replacement_keeps_canonical_status(self) -> None:
        row = {
            "type": "grid",
            "target_orders_per_side": 1,
            "gap_rate": "0.01",
            "levels": [
                {
                    "side": "buy",
                    "status": "paused_action_limit",
                    "oid": None,
                    "price": "29172",
                    "size": "0.0004",
                    "replacement_order": True,
                    "paused_at": 1,
                },
                {
                    "side": "buy",
                    "status": "paused_replacement",
                    "oid": None,
                    "price": "29172",
                    "size": "0.0004",
                    "replacement_order": True,
                    "paused_at": 2,
                },
                {
                    "side": "sell",
                    "status": "paused_action_limit",
                    "oid": None,
                    "price": "29172",
                    "size": "0.0004",
                    "replacement_order": True,
                    "paused_at": 1,
                },
            ],
        }

        changed = prune_grid_levels(row)

        self.assertTrue(changed)
        self.assertEqual(
            [(entry["side"], entry["status"], entry["price"]) for entry in row["levels"]],
            [
                ("buy", "paused_replacement", "29172"),
                ("sell", "paused_action_limit", "29172"),
            ],
        )

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
                return {"status": "ok", "response": {"data": {"statuses": ["success"] * len(requests)}}}

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
                return {"status": "ok", "response": {"data": {"statuses": ["success"] * len(requests)}}}

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

    def test_dense_grid_does_not_replace_when_cancel_is_not_confirmed(self) -> None:
        class FakeExchange:
            def __init__(self) -> None:
                self.orders = []

            def bulk_cancel(self, requests):
                return {"status": "ok", "response": {"data": {"statuses": [{"error": "action limit"}]}}}

            def order(self, *args, **kwargs):
                self.orders.append((args, kwargs))
                raise AssertionError("replacement must not be submitted")

        row = {"gap_rate": "0.01", "min_order_value": "10", "base_buy_size": "1", "base_sell_size": "1", "levels": []}
        asset = {"szDecimals": 2, "maxLeverage": 20}
        keep = grid_order_entry(row, "BTC", asset, True, Decimal("100"), False)
        keep.update({"status": "active", "oid": 1})
        dense = grid_order_entry(row, "BTC", asset, True, Decimal("99.5"), False)
        dense.update({"status": "active", "oid": 2})
        row["levels"] = [keep, dense]
        exchange = FakeExchange()

        with self.assertRaisesRegex(RuntimeError, "action limit"):
            regrid_dense_entries(
                exchange, "BTC", row, asset, 123, Decimal("0"), Decimal("0"), "abs", False, set()
            )

        self.assertEqual(dense["oid"], 2)
        self.assertEqual(dense["price"], "99.5")
        self.assertEqual(exchange.orders, [])

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

    def test_server_batch_rows_display_grid_avg(self) -> None:
        rows = [
            {
                "type": "grid",
                "status": "active",
                "network": "mainnet",
                "account": "0xabc",
                "coin": "BTC",
                "avg": "200",
                "position_limit_mode": "abs",
                "max_position_value": "400",
                "levels": [],
            },
            {
                "type": "trail",
                "status": "active",
                "network": "mainnet",
                "account": "0xabc",
                "coin": "ETH",
                "side": "sell",
                "oid": "123",
            },
        ]

        display_rows = format_server_batch_rows(rows, "mainnet", "0xabc")

        self.assertEqual(display_rows[0]["avg"], "200")
        self.assertEqual(display_rows[1]["avg"], "-")

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

    def test_limit_bounds_use_signed_position_values(self) -> None:
        self.assertEqual(
            grid_avg_bounds("limit", Decimal("-200"), Decimal("400")),
            (Decimal("-200"), Decimal("400")),
        )
        self.assertEqual(
            grid_limit_display(
                {
                    "position_limit_mode": "limit",
                    "min_position_value": "-200",
                    "max_position_value": "400",
                }
            ),
            "limit -200 400",
        )

    def test_initial_limit_plan_keeps_signed_projection_after_crossing_zero(self) -> None:
        class FakeInfo:
            def user_state(self, account: str, dex: str = "") -> dict:
                return {"assetPositions": [{"position": {"coin": "BTC", "szi": "5", "positionValue": "50"}}]}

        args = Namespace(
            trend=None,
            gap=["1%"],
            grid_min="30",
            grid_position_limit_mode="limit",
            grid_position_min_value="-100",
            grid_avg=None,
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
            {"szDecimals": 2},
            Decimal("100"),
            Decimal("10"),
            Decimal("0.05"),
        )

        sell_notional = Decimal("50")
        sell_count = 0
        for plan in plans:
            if plan["is_buy"]:
                continue
            sell_notional -= plan["notional"]
            sell_count += 1
        self.assertLess(sell_count, 10)
        self.assertGreaterEqual(sell_notional, Decimal("-100"))

    def test_worker_limit_projection_rejects_after_crossing_zero_and_lower_bound(self) -> None:
        entries = [
            {"is_buy": False, "price": "60", "size": "1"},
            {"is_buy": False, "price": "60", "size": "1"},
            {"is_buy": False, "price": "60", "size": "1"},
        ]

        projected = grid_entries_fit_within_max(
            entries,
            "sell",
            Decimal("5"),
            Decimal("50"),
            Decimal("100"),
            "limit",
            Decimal("-100"),
        )

        self.assertIsNone(projected)

    def test_limit_allows_only_orders_inside_or_toward_signed_range(self) -> None:
        self.assertTrue(
            grid_order_allowed_by_max(
                Decimal("1"),
                Decimal("250"),
                False,
                Decimal("40"),
                Decimal("400"),
                "limit",
                Decimal("200"),
            )
        )
        self.assertFalse(
            grid_order_allowed_by_max(
                Decimal("1"),
                Decimal("250"),
                False,
                Decimal("60"),
                Decimal("400"),
                "limit",
                Decimal("200"),
            )
        )

    def test_limit_outside_range_cannot_cross_beyond_opposite_bound(self) -> None:
        self.assertFalse(
            grid_order_allowed_by_max(
                Decimal("-1"), Decimal("150"), True, Decimal("300"), Decimal("100"), "limit", Decimal("-100")
            )
        )
        self.assertFalse(
            grid_order_allowed_by_max(
                Decimal("1"), Decimal("150"), False, Decimal("300"), Decimal("100"), "limit", Decimal("-100")
            )
        )
        self.assertTrue(
            grid_order_allowed_by_max(
                Decimal("-3"),
                Decimal("300"),
                True,
                Decimal("50"),
                Decimal("400"),
                "limit",
                Decimal("200"),
            )
        )
        self.assertFalse(
            grid_order_allowed_by_max(
                Decimal("-3"),
                Decimal("300"),
                False,
                Decimal("50"),
                Decimal("400"),
                "limit",
                Decimal("200"),
            )
        )

    def test_limit_cross_zero_range_does_not_force_reduce_only(self) -> None:
        self.assertFalse(grid_order_should_reduce_only(Decimal("1"), False, "limit"))
        self.assertFalse(grid_order_should_reduce_only(Decimal("-1"), True, "limit"))

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
