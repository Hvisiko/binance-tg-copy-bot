from __future__ import annotations

import asyncio
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


async def _start_optional_health_server() -> Optional[asyncio.AbstractServer]:
    """
    If a cloud platform (Zeabur Web / Fly.io / Render / Koyeb) injects PORT,
    expose a minimal 200 OK HTTP endpoint so container health checks always pass.
    """
    port_raw = os.getenv("PORT", "").strip()
    if not port_raw.isdigit():
        return None

    port = int(port_raw)

    async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req_bytes = await asyncio.wait_for(reader.read(1024), timeout=2.0)
            is_head = req_bytes.upper().startswith(b"HEAD ")
            body = b'{"status":"ok","service":"binance-tg-copy-bot"}'
            headers = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
            )
            writer.write(headers if is_head else headers + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(_handle_client, host="0.0.0.0", port=port)
    logger.info("已啟動雲端健康檢查端點 0.0.0.0:%d", port)
    return server


async def telegram_command_loop(bot: TelegramBotController, stop_event: asyncio.Event) -> None:
    await bot.register_commands()
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
    # Hydrate trader nicknames on startup
    for pid in cfg.list_portfolio_ids():
        if cfg.tracked_portfolios.get(pid) == pid:
            prof = await tracker.fetch_trader_detail(pid)
            cfg.tracked_portfolios[pid] = prof.nickname
            cfg.save_state()

    if cfg.telegram_bot_token and cfg.telegram_chat_id:
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
                if was_baseline and events:
                    continue
                for ev in events:
                    exec_res = await executor.handle_event(ev)
                    msg_html = format_position_event_html(ev, exec_note=exec_res.summary)
                    await bot.send_message(msg_html)
            except Exception as exc:
                logger.warning("監控交易員 %s (%s) 時發生異常: %s", name, pid, exc)
            await asyncio.sleep(0.4)

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
    health_server = await _start_optional_health_server()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await asyncio.gather(
            telegram_command_loop(bot, stop_event),
            trader_monitor_loop(cfg, tracker, executor, bot, stop_event),
        )
    finally:
        if health_server is not None:
            health_server.close()
            await health_server.wait_closed()
        await bot.close()
        await executor.close()
        await tracker.close()


if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
