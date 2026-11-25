from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

import httpx

from src.binance_executor import (
    BinanceFuturesExecutor,
    compute_limit_entry_price,
    round_step,
)
from src.binance_tracker import (
    BinanceCopyTracker,
    TraderOrderFill,
    TraderPosition,
    diff_positions,
)
from src.config import AppConfig, extract_portfolio_id
from src.key_utils import generate_ed25519_keypair
from src.telegram_bot import (
    TelegramBotController,
    format_position_event_html,
    split_html_chunks,
)


class TestBinanceCopyBotSuite(unittest.IsolatedAsyncioTestCase):
    def test_extract_portfolio_id(self) -> None:
        self.assertEqual(
            extract_portfolio_id(
                "https://www.binance.com/zh-TC/copy-trading/lead-details/4776391476896986880?timeRange=30D"
            ),
            "4776391476896986880",
        )
        self.assertEqual(
            extract_portfolio_id(
                "https://www.binance.com/en/copy-trading/lead-details?portfolioId=5075281354358777856"
            ),
            "5075281354358777856",
        )
        self.assertEqual(
            extract_portfolio_id(
                "https://www.binance.com/en/copy-trading/lead-details?leadPortfolioId=3958362104"
            ),
            "3958362104",
        )
        self.assertEqual(extract_portfolio_id("4776391476896986880"), "4776391476896986880")
        self.assertEqual(
            extract_portfolio_id(
                "https://www.binance.com/en/futures-activity/leaderboard/user/um?encryptedUid=ABCDEF1234567890ABCDEF1234567890"
            ),
            "uid:ABCDEF1234567890ABCDEF1234567890",
        )
        self.assertIsNone(extract_portfolio_id("invalid-text"))

    def test_round_step_and_limit_price(self) -> None:
        self.assertEqual(round_step(0.1239, "0.001", round_up=False), "0.123")
        self.assertEqual(round_step(1234.56, "10", round_up=False), "1230")
        self.assertEqual(compute_limit_entry_price(95000.0, "BUY", 0.1, "0.10"), "95095")
        self.assertEqual(compute_limit_entry_price(95000.0, "BUY", 0.0, "0.10"), "95000")
        self.assertEqual(compute_limit_entry_price(3000.0, "SELL", 0.1, "0.01"), "2997")

    def test_split_html_chunks(self) -> None:
        long_msg = "\n\n".join([f"區塊 #{i}：" + ("測試內容" * 40) for i in range(30)])
        chunks = split_html_chunks(long_msg, max_len=1000)
        self.assertGreater(len(chunks), 3)
        for ch in chunks:
            self.assertLessEqual(len(ch), 1000)

    def test_diff_positions_lifecycle_and_telegram_format(self) -> None:
        pid = "4776391476896986880"
        name = "AlphaWhale"

        curr_1 = {
            ("BTCUSDT", "LONG"): TraderPosition(
                symbol="BTCUSDT",
                position_side="LONG",
                position_amt=0.5,
                entry_price=92000.0,
                mark_price=92050.0,
                unrealized_pnl=25.0,
                leverage=20,
            )
        }
        ev_open = diff_positions(pid, name, {}, curr_1)
        self.assertEqual(len(ev_open), 1)
        self.assertEqual(ev_open[0].action, "OPEN")
        self.assertEqual(ev_open[0].ref_price, 92000.0)

        html_msg = format_position_event_html(ev_open[0], exec_note="📝 測試限價單")
        self.assertIn("🟢 新開倉", html_msg)
        self.assertIn("BTCUSDT", html_msg)
        self.assertIn("92,000.00", html_msg)
        self.assertIn("📝 測試限價單", html_msg)

        curr_2 = {
            ("BTCUSDT", "LONG"): TraderPosition(
                symbol="BTCUSDT",
                position_side="LONG",
                position_amt=1.0,
                entry_price=93000.0,
                mark_price=94100.0,
                unrealized_pnl=1100.0,
                leverage=20,
            )
        }
        ev_inc = diff_positions(pid, name, curr_1, curr_2)
        self.assertEqual(len(ev_inc), 1)
        self.assertEqual(ev_inc[0].action, "INCREASE")
        self.assertAlmostEqual(ev_inc[0].delta_qty, 0.5)
        self.assertAlmostEqual(ev_inc[0].ref_price, 94000.0)

        fill = TraderOrderFill(
            order_id="999",
            symbol="BTCUSDT",
            side="SELL",
            position_side="LONG",
            order_type="MARKET",
            avg_price=95000.0,
            executed_qty=1.0,
            realized_pnl=2000.0,
            order_time_ms=int(time.time() * 1000),
        )
        ev_close = diff_positions(pid, name, curr_2, {}, recent_fills=[fill])
        self.assertEqual(len(ev_close), 1)
        self.assertEqual(ev_close[0].action, "CLOSE")
        self.assertEqual(ev_close[0].ref_price, 95000.0)
        self.assertEqual(ev_close[0].realized_pnl, 2000.0)

    async def test_tracker_lead_data_positions_and_realtime_event(self) -> None:
        pid = "5075281354358777856"
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            url_str = str(request.url)
            if "lead-data/positions" in url_str:
                call_count += 1
                if call_count == 1:
                    return httpx.Response(200, json={"code": "000000", "data": {"positions": []}})
                return httpx.Response(
                    200,
                    json={
                        "code": "000000",
                        "data": {
                            "positions": [
                                {
                                    "symbol": "ETHUSDT",
                                    "positionSide": "SHORT",
                                    "amount": "-2.5",
                                    "entryPrice": "3120.5",
                                    "markPrice": "3115.0",
                                    "pnl": "13.75",
                                    "leverage": 15,
                                }
                            ]
                        },
                    },
                )
            if "order-history" in url_str or "position-history" in url_str:
                return httpx.Response(200, json={"code": "000000", "data": {"list": []}})
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
            tracker = BinanceCopyTracker(client=mock_client)
            was_base, ev1, pos1 = await tracker.poll_portfolio(pid, "MasterETH")
            self.assertTrue(was_base)
            self.assertEqual(ev1, [])
            self.assertEqual(pos1, [])

            was_base2, ev2, pos2 = await tracker.poll_portfolio(pid, "MasterETH")
            self.assertFalse(was_base2)
            self.assertEqual(len(ev2), 1)
            self.assertEqual(ev2[0].symbol, "ETHUSDT")
            self.assertEqual(ev2[0].position_side, "SHORT")
            self.assertEqual(ev2[0].action, "OPEN")
            self.assertEqual(ev2[0].curr_qty, 2.5)
            self.assertEqual(ev2[0].unrealized_pnl, 13.75)
            self.assertIsNotNone(pos2)

    async def test_executor_ed25519_persistence_and_early_close_cancel(self) -> None:
        _, pem_str, env_priv = generate_ed25519_keypair()
        self.assertIn("\\n", env_priv)

        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "state.json"
            cfg = AppConfig(
                telegram_bot_token="fake",
                telegram_chat_id="123",
                enable_auto_trade=True,
                binance_api_key="ed25519_test_key",
                binance_ed25519_private_key=pem_str,
                trade_margin_usdt=20.0,
                max_margin_usdt=60.0,
                trade_leverage=10,
                limit_order_mode="TARGET_PRICE",
                limit_slippage_pct=0.1,
                limit_order_timeout_sec=60,
                exit_order_type="MARKET",
                state_file=state_path,
            )

            placed_orders: List[Dict[str, Any]] = []
            canceled_ids: List[str] = []

            def fapi_handler(request: httpx.Request) -> httpx.Response:
                path = request.url.path
                params = dict(request.url.params)
                if path == "/fapi/v1/exchangeInfo":
                    return httpx.Response(
                        200,
                        json={
                            "symbols": [
                                {
                                    "symbol": "SOLUSDT",
                                    "filters": [
                                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                        {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
                                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                                    ],
                                },
                                {
                                    "symbol": "BTCUSDT",
                                    "filters": [
                                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                                        {"filterType": "MIN_NOTIONAL", "notional": "1000"},
                                    ],
                                },
                            ]
                        },
                    )
                if path == "/fapi/v1/positionSide/dual":
                    return httpx.Response(200, json={"dualSidePosition": False})
                if path == "/fapi/v1/leverage":
                    return httpx.Response(200, json={"leverage": 10, "symbol": params.get("symbol")})
                if path == "/fapi/v1/order" and request.method == "POST":
                    placed_orders.append(params)
                    return httpx.Response(200, json={"orderId": 88801, "status": "NEW"})
                if path == "/fapi/v1/order" and request.method == "DELETE":
                    canceled_ids.append(params["orderId"])
                    return httpx.Response(
                        200, json={"orderId": int(params["orderId"]), "status": "CANCELED"}
                    )
                if path == "/fapi/v2/positionRisk":
                    return httpx.Response(
                        200, json=[{"symbol": "SOLUSDT", "positionAmt": "0.0", "positionSide": "BOTH"}]
                    )
                if path == "/fapi/v2/balance":
                    return httpx.Response(
                        200, json=[{"asset": "USDT", "balance": "500.00", "availableBalance": "480.00"}]
                    )
                return httpx.Response(404, text=json.dumps({"code": -1, "msg": path}))

            async with httpx.AsyncClient(transport=httpx.MockTransport(fapi_handler)) as mock_client:
                executor = BinanceFuturesExecutor(cfg, client=mock_client)

                # Verify MIN_NOTIONAL safety cap blocks oversized symbol (1000 USDT notional at 10x needs >100 U margin > 60 U max)
                btc_ev = diff_positions(
                    "111",
                    "Whale",
                    {},
                    {
                        ("BTCUSDT", "LONG"): TraderPosition(
                            symbol="BTCUSDT",
                            position_side="LONG",
                            position_amt=1.0,
                            entry_price=95000.0,
                            mark_price=95000.0,
                            unrealized_pnl=0.0,
                            leverage=10,
                        )
                    },
                )[0]
                res_btc = await executor.handle_event(btc_ev)
                self.assertFalse(res_btc.ok)
                self.assertIn("風控攔截", res_btc.summary)

                # Normal SOLUSDT limit entry
                open_ev = diff_positions(
                    "111",
                    "SolTrader",
                    {},
                    {
                        ("SOLUSDT", "LONG"): TraderPosition(
                            symbol="SOLUSDT",
                            position_side="LONG",
                            position_amt=50.0,
                            entry_price=200.0,
                            mark_price=200.0,
                            unrealized_pnl=0.0,
                            leverage=10,
                        )
                    },
                )[0]

                res_open = await executor.handle_event(open_ev)
                self.assertTrue(res_open.ok)
                self.assertEqual(res_open.order_id, 88801)
                self.assertEqual(placed_orders[0]["type"], "LIMIT")
                self.assertEqual(placed_orders[0]["price"], "200.2")

                # Simulate container restart: reload config & executor from disk state.json!
                reloaded_cfg = AppConfig(
                    telegram_bot_token="fake",
                    telegram_chat_id="",
                    enable_auto_trade=True,
                    binance_api_key="ed25519_test_key",
                    binance_ed25519_private_key=pem_str,
                    state_file=state_path,
                )
                reloaded_cfg.load_state()
                self.assertEqual(reloaded_cfg.telegram_chat_id, "123")
                reloaded_executor = BinanceFuturesExecutor(reloaded_cfg, client=mock_client)
                self.assertIn(88801, reloaded_executor.pending_orders)

                acct_html = await reloaded_executor.fetch_account_summary()
                self.assertIn("500.00 USDT", acct_html)
                self.assertIn("#88801", acct_html)

                # Early close cancels restored pending order #88801
                close_ev = diff_positions(
                    "111",
                    "SolTrader",
                    {
                        ("SOLUSDT", "LONG"): TraderPosition(
                            symbol="SOLUSDT",
                            position_side="LONG",
                            position_amt=50.0,
                            entry_price=200.0,
                            mark_price=201.0,
                            unrealized_pnl=50.0,
                            leverage=10,
                        )
                    },
                    {},
                )[0]
                res_close = await reloaded_executor.handle_event(close_ev)
                self.assertTrue(res_close.ok)
                self.assertEqual(canceled_ids, ["88801"])
                self.assertNotIn(88801, reloaded_executor.pending_orders)

    async def test_post_only_gtx_retry_and_fallback(self) -> None:
        _, pem_str, _ = generate_ed25519_keypair()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = AppConfig(
                telegram_bot_token="fake",
                telegram_chat_id="123",
                enable_auto_trade=True,
                binance_api_key="ed_key",
                binance_ed25519_private_key=pem_str,
                limit_order_mode="POST_ONLY_BBO",
                limit_slippage_pct=0.1,
                state_file=Path(tmp_dir) / "state.json",
            )
            order_attempts: List[Dict[str, Any]] = []

            def handler(request: httpx.Request) -> httpx.Response:
                path = request.url.path
                params = dict(request.url.params)
                if path == "/fapi/v1/exchangeInfo":
                    return httpx.Response(
                        200,
                        json={
                            "symbols": [
                                {
                                    "symbol": "ETHUSDT",
                                    "filters": [
                                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                        {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                                    ],
                                }
                            ]
                        },
                    )
                if path == "/fapi/v1/positionSide/dual":
                    return httpx.Response(200, json={"dualSidePosition": False})
                if path == "/fapi/v1/leverage":
                    return httpx.Response(200, json={"leverage": 10})
                if path == "/fapi/v1/ticker/bookTicker":
                    return httpx.Response(200, json={"bidPrice": "3000.00", "askPrice": "3000.10"})
                if path == "/fapi/v1/order" and request.method == "POST":
                    order_attempts.append(params)
                    if params.get("timeInForce") == "GTX":
                        return httpx.Response(
                            400, json={"code": -5022, "msg": "Post Only order will be rejected"}
                        )
                    return httpx.Response(200, json={"orderId": 77701, "status": "NEW"})
                return httpx.Response(404)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
                executor = BinanceFuturesExecutor(cfg, client=mock_client)
                ev = diff_positions(
                    "222",
                    "EthMaker",
                    {},
                    {
                        ("ETHUSDT", "LONG"): TraderPosition(
                            symbol="ETHUSDT",
                            position_side="LONG",
                            position_amt=2.0,
                            entry_price=3000.0,
                            mark_price=3000.0,
                            unrealized_pnl=0.0,
                            leverage=10,
                        )
                    },
                )[0]
                res = await executor.handle_event(ev)
                self.assertTrue(res.ok)
                self.assertEqual(len(order_attempts), 3)
                self.assertEqual(order_attempts[0]["timeInForce"], "GTX")
                self.assertEqual(order_attempts[1]["timeInForce"], "GTX")
                self.assertEqual(order_attempts[2]["timeInForce"], "GTC")

    async def test_telegram_interactive_commands_and_callbacks(self) -> None:
        sent_messages: List[Dict[str, Any]] = []

        def tg_and_bn_handler(request: httpx.Request) -> httpx.Response:
            url_s = str(request.url)
            if "/sendMessage" in url_s:
                sent_messages.append(json.loads(request.content.decode("utf-8")))
                return httpx.Response(200, json={"ok": True})
            if "/answerCallbackQuery" in url_s:
                return httpx.Response(200, json={"ok": True})
            if "lead-portfolio/detail" in url_s:
                return httpx.Response(200, json={"code": "000000", "data": {"nickname": "TopSniper"}})
            if "positions" in url_s:
                return httpx.Response(200, json={"code": "000000", "data": []})
            if "order-history" in url_s or "position-history" in url_s:
                return httpx.Response(200, json={"code": "000000", "data": {"list": []}})
            return httpx.Response(200, json={"ok": True})

        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "state.json"
            cfg = AppConfig(
                telegram_bot_token="123:FAKE_TOKEN",
                telegram_chat_id="",  # Blank initially to test auto-bind & disk persistence!
                state_file=state_file,
            )
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(tg_and_bn_handler)
            ) as mock_client:
                tracker = BinanceCopyTracker(client=mock_client)
                executor = BinanceFuturesExecutor(cfg, client=mock_client)
                bot = TelegramBotController(cfg, tracker, executor, client=mock_client)

                # 1. First /start auto-binds chat_id="998877" and saves to state.json
                await bot._handle_incoming_message(
                    {"chat": {"id": 998877, "type": "private"}, "text": "/start"}
                )
                self.assertEqual(cfg.telegram_chat_id, "998877")
                saved_disk = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual(saved_disk["telegram_chat_id"], "998877")
                self.assertIn("inline_keyboard", sent_messages[-1]["reply_markup"])

                # 2. /set slippage 0.05 updates & persists setting
                await bot._handle_incoming_message(
                    {"chat": {"id": 998877, "type": "private"}, "text": "/set slippage 0.05"}
                )
                self.assertEqual(cfg.limit_slippage_pct, 0.05)

                # 3. /genkey in private chat outputs Ed25519 keypair; blocked in group chat
                await bot._handle_incoming_message(
                    {"chat": {"id": 998877, "type": "group"}, "text": "/genkey"}
                )
                self.assertIn("僅限在與機器人的一對一私訊", sent_messages[-1]["text"].replace("<b>", "").replace("</b>", ""))

                await bot._handle_incoming_message(
                    {"chat": {"id": 998877, "type": "private"}, "text": "/genkey"}
                )
                self.assertIn("BEGIN PUBLIC KEY", sent_messages[-1]["text"])
                self.assertIn("BINANCE_ED25519_PRIVATE_KEY", sent_messages[-1]["text"])

                # 4. One-tap inline button callback `cb_add:4776391476896986880` adds trader
                await bot._handle_callback_query(
                    {
                        "id": "cq1",
                        "data": "cb_add:4776391476896986880",
                        "message": {"chat": {"id": 998877, "type": "private"}},
                    }
                )
                self.assertEqual(cfg.tracked_portfolios.get("4776391476896986880"), "TopSniper")


if __name__ == "__main__":
    unittest.main()
