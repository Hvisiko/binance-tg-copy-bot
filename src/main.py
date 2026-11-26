from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Optional

from .binance_executor import BinanceFuturesExecutor
from .binance_tracker import BinanceCopyTracker
from .config import AppConfig
from .telegram_bot import TelegramBotController, format_position_event_html

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Silence verbose httpx request URLs so Telegram Bot Token is never printed in logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("binance-tg-copy-bot")


async def _start_http_and_webhook_server(
    bot: TelegramBotController,
) -> Optional[asyncio.AbstractServer]:
    """
    Expose HTTP healthcheck (/ and /healthz) AND Telegram Webhook receiver (/tg-webhook/<secret>)
    when PORT is injected by cloud platforms (Render / Koyeb / Fly.io / Railway).
    Receiving Telegram updates via inbound HTTPS POST wakes sleeping free-tier instances automatically!
    """
    port_raw = os.getenv("PORT", "").strip()
    if not port_raw.isdigit():
        return None

    port = int(port_raw)

    async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header_buf = b""
            while b"\r\n\r\n" not in header_buf and len(header_buf) < 16384:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                if not chunk:
                    break
                header_buf += chunk

            if b"\r\n\r\n" not in header_buf:
                return

            head_part, body_buf = header_buf.split(b"\r\n\r\n", 1)
            header_lines = head_part.decode("latin-1", errors="replace").split("\r\n")
            req_parts = header_lines[0].split() if header_lines else []
            method = req_parts[0].upper() if len(req_parts) >= 1 else "GET"
            raw_path = req_parts[1].split("?", 1)[0] if len(req_parts) >= 2 else "/"

            content_length = 0
            for hline in header_lines[1:]:
                if hline.lower().startswith("content-length:"):
                    try:
                        content_length = max(0, min(1_048_576, int(hline.split(":", 1)[1].strip())))
                    except ValueError:
                        content_length = 0
                    break

            while len(body_buf) < content_length:
                more = await asyncio.wait_for(
                    reader.read(content_length - len(body_buf)), timeout=5.0
                )
                if not more:
                    break
                body_buf += more

            if method == "POST" and raw_path == bot.webhook_path:
                try:
                    upd_obj = json.loads(body_buf[:content_length].decode("utf-8"))
                    if isinstance(upd_obj, dict):
                        asyncio.create_task(bot.process_update(upd_obj))
                except Exception as parse_exc:
                    logger.warning("解析 Telegram Webhook JSON 失敗: %s", parse_exc)

            body = b'{"status":"ok","service":"binance-tg-copy-bot"}'
            headers = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
            )
            writer.write(headers if method == "HEAD" else headers + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(_handle_client, host="0.0.0.0", port=port)
    logger.info("已啟動雲端 HTTP 健康檢查與 Webhook 端點 0.0.0.0:%d", port)
    return server


async def telegram_command_loop(
    cfg: AppConfig, bot: TelegramBotController, stop_event: asyncio.Event
) -> None:
    use_webhook = bool(cfg.webhook_base_url and os.getenv("PORT", "").strip().isdigit())
    await bot.register_commands(use_webhook=use_webhook)

    if use_webhook:
        ok = await bot.setup_webhook(cfg.webhook_base_url)
        if ok:
            await stop_event.wait()
            return
        logger.warning("Webhook 註冊失敗，自動降級為 getUpdates 長輪詢模式。")

    while not stop_event.is_set():
        await bot.poll_updates_once()
        await asyncio.sleep(0.5)


async def trader_monitor_loop(
    cfg: AppConfig,
    tracker: BinanceCopyTracker,
    executor: BinanceFuturesExecutor,
    bot: TelegramBotController,
    stop_event: asyncio.Event,
) -> None:
    # Seed persisted trade watermarks so container wakeups never miss trades or re-fire old ones
    tracker.seed_watermarks(cfg.last_order_time_ms)

    # Hydrate trader nicknames on startup
    hydrated_any = False
    for pid in cfg.list_portfolio_ids():
        if cfg.tracked_portfolios.get(pid) == pid:
            prof = await tracker.fetch_trader_detail(pid)
            cfg.tracked_portfolios[pid] = prof.nickname
            hydrated_any = True
    if hydrated_any:
        cfg.save_state()

    notify_startup = os.getenv("NOTIFY_ON_STARTUP", "false").lower() in ("1", "true", "yes", "on")
    if notify_startup and cfg.telegram_bot_token and cfg.telegram_chat_id:
        mode_note = "🟢 自動限價跟單＋通知" if cfg.enable_auto_trade else "🔔 純即時交易通知（免 API Key）"
        await bot.send_message(
            f"🚀 <b>幣安追蹤機器人已於雲端啟動！</b>\n"
            f"• 運作模式：{mode_note}\n"
            f"• 追蹤人數：<code>{len(cfg.tracked_portfolios)}</code> 位\n"
            f"• 點擊下方選單或輸入 <code>/top</code> 即可一鍵追蹤熱門帶單員！",
            reply_markup=bot._main_menu_keyboard(),
        )
    elif cfg.telegram_bot_token:
        logger.info("已連線 Telegram Bot，請打開手機對機器人傳送 /start 以自動綁定管理員 Chat ID！")

    while not stop_event.is_set():
        portfolio_items = list(cfg.tracked_portfolios.items())
        for pid, name in portfolio_items:
            if stop_event.is_set():
                break
            try:
                was_baseline, events, _ = await tracker.poll_portfolio(pid, name)
                new_wm = tracker._last_order_time_ms.get(pid, 0)
                if new_wm > cfg.last_order_time_ms.get(pid, 0):
                    cfg.last_order_time_ms[pid] = new_wm
                    cfg.save_state()
                if was_baseline and events:
                    continue
                for ev in events:
                    exec_res = await executor.handle_event(ev)
                    msg_html = format_position_event_html(ev, exec_note=exec_res.summary)
                    await bot.send_message(msg_html)
            except Exception as exc:
                logger.warning("監控交易員 %s (%s) 時發生異常: %s", name, pid, exc)
            await asyncio.sleep(0.8)

        # Check pending limit orders for fills or timeout auto-cancellation
        try:
            order_alerts = await executor.check_pending_orders()
            for alert in order_alerts:
                await bot.send_message(alert)
        except Exception as exc:
            logger.warning("檢查未成交限價單時發生異常: %s", exc)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=cfg.poll_interval_sec)
        except asyncio.TimeoutError:
            pass


async def async_main() -> None:
    cfg = AppConfig.from_env()
    if not cfg.telegram_bot_token:
        logger.warning("尚未設定 TELEGRAM_BOT_TOKEN！請在雲端環境變數填入 Bot Token。")

    tracker = BinanceCopyTracker(cookie=cfg.binance_web_cookie)
    executor = BinanceFuturesExecutor(config=cfg)
    bot = TelegramBotController(config=cfg, tracker=tracker, executor=executor)
    http_server = await _start_http_and_webhook_server(bot)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await asyncio.gather(
            telegram_command_loop(cfg, bot, stop_event),
            trader_monitor_loop(cfg, tracker, executor, bot, stop_event),
        )
    finally:
        if http_server is not None:
            http_server.close()
            await http_server.wait_closed()
        await bot.close()
        await executor.close()
        await tracker.close()


if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
