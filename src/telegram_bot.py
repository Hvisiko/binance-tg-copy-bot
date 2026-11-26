from __future__ import annotations

import asyncio
import hashlib
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .binance_executor import BinanceFuturesExecutor
from .binance_tracker import (
    BinanceCopyTracker,
    PositionEvent,
    TraderOrderFill,
    TraderProfile,
)
from .config import AppConfig
from .key_utils import generate_ed25519_keypair

logger = logging.getLogger(__name__)

TZ_TAIPEI = timezone(timedelta(hours=8))
MAX_TG_MESSAGE_LEN = 3900


def _fmt_price(val: float) -> str:
    if abs(val) >= 1000:
        return f"{val:,.2f}"
    if abs(val) >= 1:
        return f"{val:,.4f}".rstrip("0").rstrip(".")
    return f"{val:.6f}".rstrip("0").rstrip(".") or "0"


def _fmt_pct(pct: float) -> str:
    ap = abs(pct)
    if ap >= 100:
        return f"{pct:,.0f}%"
    if ap >= 10:
        return f"{pct:.1f}%"
    if ap >= 1:
        return f"{pct:.1f}%"
    if ap >= 0.01:
        return f"{pct:.2f}%"
    if ap > 0:
        return "<0.01%" if pct > 0 else ">-0.01%"
    return "0%"


def _fmt_compact_usd(val: float) -> str:
    av = abs(val)
    sign = "-" if val < 0 else ""
    if av >= 1_000_000:
        return f"{sign}${av / 1_000_000:.2f}M"
    if av >= 10_000:
        return f"{sign}${av / 1_000:.1f}K"
    if av >= 100:
        return f"{sign}${av:,.0f}"
    return f"{sign}${av:,.2f}"


def _short_coin(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith("USDT") and len(s) > 4:
        return s[:-4]
    return s


def _calc_pos_pct_label(notional_usdt: float, margin_balance: Optional[float]) -> str:
    if margin_balance is not None and margin_balance > 1.0:
        pct = (notional_usdt / margin_balance) * 100.0
        return _fmt_pct(pct)
    return _fmt_compact_usd(notional_usdt)


def split_html_chunks(text: str, max_len: int = MAX_TG_MESSAGE_LEN) -> List[str]:
    """Safely split long Telegram HTML messages along paragraph or line boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    remaining = text
    while len(remaining) > max_len:
        cut = remaining.rfind("\n\n", 0, max_len)
        if cut <= 0:
            cut = remaining.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = max_len
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining.strip():
        chunks.append(remaining.strip())
    return chunks


def _action_badge(action: str, position_side: str) -> Tuple[str, str, bool]:
    """Returns (emoji, short_label, is_open)."""
    if action in ("OPEN", "INCREASE"):
        if position_side == "LONG":
            return "🟢", "開多", True
        return "🔴", "開空", True
    if position_side == "LONG":
        return "⚪", "平多", False
    return "⚪", "平空", False


def format_position_event_html(event: PositionEvent, exec_note: str = "") -> str:
    """Format a newly detected trader execution record into a sleek, percentage-based Telegram card."""
    icon, act_label, is_open = _action_badge(event.action, event.position_side)
    ts_sec = (
        (event.event_time_ms / 1000.0)
        if event.event_time_ms > 0
        else datetime.now().timestamp()
    )
    dt_str = datetime.fromtimestamp(ts_sec, tz=TZ_TAIPEI).strftime("%m-%d %H:%M:%S")
    url = BinanceCopyTracker.build_trader_url(event.portfolio_id)
    safe_name = html.escape(event.trader_name)

    notional_val = event.ref_price * event.delta_qty
    pct_str = _calc_pos_pct_label(notional_val, event.trader_margin_balance)
    price_title = "成交均價" if is_open else "平倉均價"
    size_title = "倉位佔比" if is_open else "平倉佔比"
    type_tag = f"  <code>{html.escape(event.order_type)}</code>" if event.order_type else ""

    lines = [
        f"{icon} <b>{act_label} {html.escape(event.symbol)}</b>{type_tag}",
        f"👤 <a href=\"{url}\"><b>{safe_name}</b></a>",
        "━━━━━━━━━━━━━━",
        f"💰 {price_title}：<code>{_fmt_price(event.ref_price)}</code>",
        f"📊 {size_title}：<code>{pct_str}</code>",
    ]

    if event.realized_pnl is not None and abs(event.realized_pnl) > 1e-9:
        pnl_emoji = "🤑" if event.realized_pnl >= 0 else "💸"
        sign = "+" if event.realized_pnl >= 0 else ""
        if event.trader_margin_balance and event.trader_margin_balance > 1.0:
            ret_pct = (event.realized_pnl / event.trader_margin_balance) * 100.0
            lines.append(
                f"{pnl_emoji} 本金損益：<code>{sign}{_fmt_pct(ret_pct)}</code> <i>({sign}{_fmt_compact_usd(event.realized_pnl)})</i>"
            )
        else:
            lines.append(
                f"{pnl_emoji} 實現盈虧：<code>{sign}{_fmt_compact_usd(event.realized_pnl)}</code>"
            )

    lines.append(f"🕒 成交時間：<code>{dt_str}</code>")

    if exec_note:
        lines.append("━━━━━━━━━━━━━━")
        lines.append(exec_note)

    return "\n".join(lines)


def format_recent_trades_html(
    profile: TraderProfile, fills: Optional[List[TraderOrderFill]], max_items: int = 5
) -> str:
    """Format a trader's latest executed trade records cleanly using % of total portfolio."""
    safe_name = html.escape(profile.nickname)
    header = f"👤 <a href=\"{profile.url}\"><b>{safe_name}</b></a>"
    if fills is None:
        return f"{header}\n  <i>⚠️ 暫時無法讀取公開交易紀錄</i>"
    if not fills:
        return f"{header}\n  <i>📭 近期尚無公開成交紀錄</i>"

    recent = sorted(fills, key=lambda x: x.order_time_ms, reverse=True)[:max_items]
    lines = [header]
    for f in recent:
        action, p_side = f.infer_action_and_pos_side()
        icon, act_label, _ = _action_badge(action, p_side)
        dt_str = (
            datetime.fromtimestamp(f.order_time_ms / 1000.0, tz=TZ_TAIPEI).strftime(
                "%m-%d %H:%M"
            )
            if f.order_time_ms > 0
            else "--:--"
        )
        notional_u = f.avg_price * f.executed_qty
        pct_str = _calc_pos_pct_label(notional_u, profile.margin_balance)
        coin = html.escape(_short_coin(f.symbol))

        pnl_suffix = ""
        if abs(f.realized_pnl) > 1e-9:
            p_icon = "🤑" if f.realized_pnl >= 0 else "💸"
            sign = "+" if f.realized_pnl >= 0 else ""
            if profile.margin_balance and profile.margin_balance > 1.0:
                rpct = (f.realized_pnl / profile.margin_balance) * 100.0
                pnl_suffix = f" {p_icon}<code>{sign}{_fmt_pct(rpct)}</code>"
            else:
                pnl_suffix = f" {p_icon}<code>{sign}{_fmt_compact_usd(f.realized_pnl)}</code>"

        lines.append(
            f"<code>{dt_str}</code> {icon}{act_label} <b>{coin}</b> "
            f"@<code>{_fmt_price(f.avg_price)}</code> · <code>{pct_str}</code>{pnl_suffix}"
        )
    return "\n".join(lines)


class TelegramBotController:
    """Lightweight async Telegram Bot API client with Inline Buttons & interactive commands."""

    def __init__(
        self,
        config: AppConfig,
        tracker: BinanceCopyTracker,
        executor: BinanceFuturesExecutor,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.cfg = config
        self.tracker = tracker
        self.executor = executor
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=35.0)
        self._update_offset: int = 0

    @property
    def api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}"

    @property
    def webhook_path(self) -> str:
        secret = hashlib.sha256(self.cfg.telegram_bot_token.encode("utf-8")).hexdigest()[:24]
        return f"/tg-webhook/{secret}"

    async def setup_webhook(self, base_url: str) -> bool:
        if not self.cfg.telegram_bot_token or not base_url:
            return False
        full_url = f"{base_url.rstrip('/')}{self.webhook_path}"
        try:
            resp = await self.client.post(
                f"{self.api_base}/setWebhook",
                json={
                    "url": full_url,
                    "allowed_updates": ["message", "edited_message", "callback_query"],
                    "drop_pending_updates": False,
                },
            )
            ok = resp.status_code == 200 and bool((resp.json() or {}).get("ok"))
            if ok:
                logger.info(
                    "已啟用 Telegram Webhook 即時推播喚醒模式: %s/tg-webhook/***",
                    base_url.rstrip("/"),
                )
            else:
                logger.error("設定 Telegram Webhook 失敗 (%s): %s", resp.status_code, resp.text)
            return ok
        except Exception as exc:
            logger.error("設定 Telegram Webhook 異常: %s", exc)
            return False

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _main_menu_keyboard(self) -> Dict[str, Any]:
        toggle_label = "🔔 切回純通知" if self.cfg.enable_auto_trade else "🟢 開啟自動限價跟單"
        return {
            "inline_keyboard": [
                [
                    {"text": "📜 最新交易紀錄", "callback_data": "cb_menu:trades"},
                    {"text": "🏆 熱門帶單排行", "callback_data": "cb_menu:top"},
                ],
                [
                    {"text": "🎛️ 管理監控名單", "callback_data": "cb_menu:manage"},
                    {"text": "⚙️ 跟單參數設定", "callback_data": "cb_menu:settings"},
                ],
                [
                    {"text": "💼 我的帳戶／掛單", "callback_data": "cb_menu:my"},
                    {"text": toggle_label, "callback_data": "cb_menu:toggle_auto"},
                ],
            ]
        }

    async def send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        target_chat = chat_id or self.cfg.telegram_chat_id
        if not self.cfg.telegram_bot_token or not target_chat:
            logger.warning(
                "尚未設定 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，訊息輸出至 Console:\n%s",
                text,
            )
            return False

        parts = split_html_chunks(text)
        ok_all = True
        for idx, part in enumerate(parts):
            payload: Dict[str, Any] = {
                "chat_id": target_chat,
                "text": part,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup is not None and idx == len(parts) - 1:
                payload["reply_markup"] = reply_markup

            try:
                resp = await self.client.post(f"{self.api_base}/sendMessage", json=payload)
                if resp.status_code != 200:
                    logger.error("Telegram sendMessage 失敗 (%s): %s", resp.status_code, resp.text)
                    ok_all = False
            except Exception as exc:
                logger.error("Telegram 發送通知異常: %s", exc)
                ok_all = False
        return ok_all

    async def _answer_callback(self, callback_id: str, text: str = "") -> None:
        if not self.cfg.telegram_bot_token or not callback_id:
            return
        try:
            payload: Dict[str, Any] = {"callback_query_id": callback_id}
            if text:
                payload["text"] = text
            await self.client.post(f"{self.api_base}/answerCallbackQuery", json=payload)
        except Exception:
            pass

    async def register_commands(self, use_webhook: bool = False) -> None:
        if not self.cfg.telegram_bot_token:
            return
        if not use_webhook:
            try:
                await self.client.post(
                    f"{self.api_base}/deleteWebhook",
                    json={"drop_pending_updates": False},
                )
            except Exception:
                pass
        commands = [
            {"command": "start", "description": "開啟互動控制面板與系統狀態"},
            {"command": "trades", "description": "查看所有追蹤中交易員的最新交易與倉位佔比"},
            {"command": "top", "description": "查詢幣安 30 日高收益帶單員（附一鍵追蹤按鈕）"},
            {"command": "add", "description": "新增追蹤帶單員（貼網址或 portfolioId）"},
            {"command": "remove", "description": "移除追蹤中的帶單員"},
            {"command": "my", "description": "查看我的幣安合約餘額、持倉與待成交限價單"},
            {"command": "set", "description": "即時檢視或修改保證金、槓桿、滑點與超時秒數"},
            {"command": "autotrading", "description": "切換「限價自動跟單」開關"},
            {"command": "genkey", "description": "一鍵產生雲端免綁 IP 專用 Ed25519 金鑰"},
        ]
        try:
            await self.client.post(f"{self.api_base}/setMyCommands", json={"commands": commands})
        except Exception:
            pass

    async def poll_updates_once(self) -> None:
        if not self.cfg.telegram_bot_token:
            return
        try:
            resp = await self.client.get(
                f"{self.api_base}/getUpdates",
                params={"offset": self._update_offset, "timeout": 20},
            )
            if resp.status_code == 409:
                logger.warning("Telegram getUpdates 409 衝突: %s", resp.text)
                await asyncio.sleep(5.0)
                return
            if resp.status_code != 200:
                return
            data = resp.json()
            for upd in data.get("result", []):
                self._update_offset = int(upd["update_id"]) + 1
                await self.process_update(upd)
        except Exception as exc:
            logger.debug("Telegram getUpdates 輪詢略過: %s", exc)

    async def process_update(self, upd: Dict[str, Any]) -> None:
        if "callback_query" in upd:
            await self._handle_callback_query(upd["callback_query"])
            return
        msg = upd.get("message") or upd.get("edited_message")
        if msg:
            await self._handle_incoming_message(msg)

    def _authorize_actor(
        self, chat_id: str, user_id: str = "", bind_push_chat: bool = False
    ) -> bool:
        if not chat_id:
            return False
        if not self.cfg.telegram_chat_id and not self.cfg.admin_user_id:
            self.cfg.telegram_chat_id = chat_id
            if user_id:
                self.cfg.admin_user_id = user_id
            self.cfg.save_state()
            logger.info(
                "已自動綁定管理員 user_id=%s 及預設推播 chat_id=%s", user_id, chat_id
            )
            return True

        if user_id and self.cfg.admin_user_id and user_id == str(self.cfg.admin_user_id):
            if bind_push_chat and self.cfg.telegram_chat_id != chat_id:
                self.cfg.telegram_chat_id = chat_id
                self.cfg.save_state()
                logger.info("已切換預設推播聊天室為 chat_id=%s", chat_id)
            return True

        if self.cfg.telegram_chat_id and chat_id == str(self.cfg.telegram_chat_id):
            if user_id and not self.cfg.admin_user_id:
                self.cfg.admin_user_id = user_id
                self.cfg.save_state()
            return True

        return False

    async def _handle_callback_query(self, cq: Dict[str, Any]) -> None:
        cq_id = str(cq.get("id") or "")
        data = str(cq.get("data") or "")
        from_user_id = str((cq.get("from") or {}).get("id") or "")
        msg = cq.get("message") or {}
        chat_Obj = msg.get("chat") or {}
        chat_id = str(chat_Obj.get("id") or "")
        chat_type = str(chat_Obj.get("type") or "private")

        if not self._authorize_actor(chat_id, from_user_id):
            await self._answer_callback(cq_id, "⛔ 未授權的操作")
            return

        await self._answer_callback(cq_id)

        if data == "cb_menu:top":
            await self._cmd_top(chat_id)
        elif data in ("cb_menu:trades", "cb_menu:positions"):
            await self._cmd_trades(chat_id)
        elif data == "cb_menu:manage":
            await self._cmd_manage(chat_id)
        elif data == "cb_menu:my":
            await self._cmd_my_account(chat_id)
        elif data == "cb_menu:settings":
            await self._cmd_settings(chat_id, "")
        elif data == "cb_menu:toggle_auto":
            target = "off" if self.cfg.enable_auto_trade else "on"
            await self._cmd_autotrading(chat_id, target)
        elif data == "cb_cancel_all":
            note = await self.executor.cancel_all_pending_orders()
            await self.send_message(note, chat_id=chat_id)
        elif data.startswith("cb_add:"):
            pid = data.split(":", 1)[1]
            await self._cmd_add(chat_id, pid)
        elif data.startswith("cb_rm:"):
            pid = data.split(":", 1)[1]
            await self._cmd_remove(chat_id, pid)
        elif data == "cb_menu:start":
            await self._cmd_start(chat_id, chat_type)

    async def _handle_incoming_message(self, msg: Dict[str, Any]) -> None:
        chat_obj = msg.get("chat") or {}
        chat_id = str(chat_obj.get("id") or "")
        chat_type = str(chat_obj.get("type") or "private")
        from_user_id = str((msg.get("from") or {}).get("id") or "")
        text = str(msg.get("text") or "").strip()
        if not text or not chat_id:
            return

        # Allow pasting a Binance copy-trading URL (or App QR short-link) directly without typing `/add`!
        if not text.startswith("/"):
            if any(
                h in text.lower() for h in ("binance.com", "binance.me", "safu.im")
            ) and self._authorize_actor(chat_id, from_user_id):
                resolved_pid = await self.tracker.resolve_portfolio_id(text)
                if resolved_pid:
                    await self._cmd_add(chat_id, resolved_pid)
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        is_bind_cmd = cmd == "/start"
        if not self._authorize_actor(chat_id, from_user_id, bind_push_chat=is_bind_cmd):
            await self.send_message("⛔ 未授權的帳號或群組，僅限管理員操作。", chat_id=chat_id)
            return

        if cmd in ("/start", "/help", "/status"):
            await self._cmd_start(chat_id, chat_type)
        elif cmd == "/top":
            await self._cmd_top(chat_id)
        elif cmd == "/add":
            await self._cmd_add(chat_id, arg)
        elif cmd == "/remove":
            await self._cmd_remove(chat_id, arg)
        elif cmd in ("/trades", "/history", "/positions", "/list"):
            await self._cmd_trades(chat_id)
        elif cmd == "/manage":
            await self._cmd_manage(chat_id)
        elif cmd in ("/my", "/account", "/orders", "/balance"):
            await self._cmd_my_account(chat_id)
        elif cmd == "/cancelall":
            note = await self.executor.cancel_all_pending_orders()
            await self.send_message(note, chat_id=chat_id)
        elif cmd in ("/set", "/settings"):
            await self._cmd_settings(chat_id, arg)
        elif cmd in ("/autotrading", "/mode"):
            await self._cmd_autotrading(chat_id, arg)
        elif cmd == "/genkey":
            await self._cmd_genkey(chat_id, chat_type)
        else:
            await self.send_message(
                "❓ 未知指令，請輸入 <code>/start</code> 開啟功能控制面板。",
                chat_id=chat_id,
            )

    async def _cmd_start(self, chat_id: str, _chat_type: str = "private") -> None:
        mode_str = (
            "🟢 <b>自動限價跟單＋即時推播</b>"
            if self.cfg.enable_auto_trade
            else "🔔 <b>即時成交推播中</b> <i>(安全唯讀)</i>"
        )
        tracked_lines = [
            f"  {idx}. <a href=\"{BinanceCopyTracker.build_trader_url(pid)}\"><b>{html.escape(name)}</b></a>"
            for idx, (pid, name) in enumerate(self.cfg.tracked_portfolios.items(), 1)
        ]
        tracked_block = (
            "\n".join(tracked_lines)
            if tracked_lines
            else "  <i>（尚未新增，點下方排行或直接貼上帶單網址即可追蹤）</i>"
        )
        reply = (
            "🤖 <b>幣安帶單「最新紀錄」即時雷達</b>\n"
            "━━━━━━━━━━━━━━\n"
            f"📡 狀態：{mode_str}\n"
            f"⏱️ 掃描：每 <code>{self.cfg.poll_interval_sec:.0f}s</code> ｜ 顯示<b>佔總倉位百分比</b>\n"
            f"⚙️ 跟單：<code>{self.cfg.limit_order_mode}</code> ｜ <code>{self.cfg.trade_margin_usdt:g}U × {self.cfg.trade_leverage}x</code> ｜ 滑點 <code>{self.cfg.limit_slippage_pct}%</code>\n\n"
            f"👀 <b>監控名單（{len(self.cfg.tracked_portfolios)} 位）</b>\n{tracked_block}"
        )
        await self.send_message(reply, chat_id=chat_id, reply_markup=self._main_menu_keyboard())

    async def _cmd_manage(self, chat_id: str) -> None:
        if not self.cfg.tracked_portfolios:
            await self.send_message("📭 目前監控名單為空，可直接貼上幣安帶單網址新增。", chat_id=chat_id)
            return

        lines = [
            "🎛️ <b>管理監控名單</b>",
            "━━━━━━━━━━━━━━",
            "點擊下方按鈕可取消追蹤，或直接把新的幣安帶單網址貼進對話框即可新增：",
        ]
        items = list(self.cfg.tracked_portfolios.items())
        kb_rows: List[List[Dict[str, str]]] = []
        row: List[Dict[str, str]] = []
        for pid, name in items:
            row.append({"text": f"🗑️ {name[:12]}", "callback_data": f"cb_rm:{pid}"})
            if len(row) == 2:
                kb_rows.append(row)
                row = []
        if row:
            kb_rows.append(row)
        kb_rows.append([{"text": "⬅️ 返回主選單", "callback_data": "cb_menu:start"}])

        await self.send_message(
            "\n".join(lines),
            chat_id=chat_id,
            reply_markup={"inline_keyboard": kb_rows},
        )

    async def _cmd_top(self, chat_id: str) -> None:
        await self.send_message("🔍 正在抓取幣安近 30 日高收益帶單員...", chat_id=chat_id)
        top_list = await self.tracker.fetch_top_portfolios(limit=5)
        if not top_list:
            await self.send_message("⚠️ 暫時無法取得排行榜清單，請稍後再試。", chat_id=chat_id)
            return

        lines = ["🏆 <b>幣安跟單 30 日高收益排行</b>\n━━━━━━━━━━━━━━"]
        kb_rows: List[List[Dict[str, str]]] = []

        for idx, prof in enumerate(top_list, 1):
            roi_txt = f"+{prof.roi_30d:.1f}%" if prof.roi_30d is not None else "N/A"
            pnl_txt = _fmt_compact_usd(prof.pnl_30d) if prof.pnl_30d is not None else "N/A"
            already = " ✅" if prof.portfolio_id in self.cfg.tracked_portfolios else ""
            lines.append(
                f"{idx}. <a href=\"{prof.url}\"><b>{html.escape(prof.nickname)}</b></a>{already}\n"
                f"   30D ROI <code>{roi_txt}</code> ｜ 獲利 <code>{pnl_txt}</code>"
            )
            if prof.portfolio_id not in self.cfg.tracked_portfolios:
                kb_rows.append(
                    [
                        {
                            "text": f"➕ 一鍵追蹤 {prof.nickname[:16]}",
                            "callback_data": f"cb_add:{prof.portfolio_id}",
                        }
                    ]
                )

        markup = {"inline_keyboard": kb_rows} if kb_rows else None
        await self.send_message("\n".join(lines), chat_id=chat_id, reply_markup=markup)

    async def _cmd_add(self, chat_id: str, arg: str) -> None:
        pid = await self.tracker.resolve_portfolio_id(arg)
        if not pid:
            await self.send_message(
                "❓ 請直接貼上<b>幣安帶單員網址</b>（電腦版或手機 App 分享短網址皆可）或數字 ID！",
                chat_id=chat_id,
            )
            return
        profile = await self.tracker.fetch_trader_detail(pid)
        self.cfg.tracked_portfolios[pid] = profile.nickname
        self.tracker.remove_portfolio_state(pid)
        _, _, init_fills = await self.tracker.poll_portfolio(pid, profile.nickname)
        wm = self.tracker._last_order_time_ms.get(pid, 0)
        if wm > 0:
            self.cfg.last_order_time_ms[pid] = wm
        self.cfg.save_state()

        snap = format_recent_trades_html(profile, init_fills, max_items=5)
        markup = {
            "inline_keyboard": [
                [
                    {"text": "📜 查看全部最新紀錄", "callback_data": "cb_menu:trades"},
                    {"text": "🗑️ 取消追蹤", "callback_data": f"cb_rm:{pid}"},
                ]
            ]
        }
        await self.send_message(
            f"✅ <b>已加入即時監控！</b>\n━━━━━━━━━━━━━━\n{snap}\n\n"
            "<i>💡 歷史紀錄已設為基準，後續一有新成交將主動推播！</i>",
            chat_id=chat_id,
            reply_markup=markup,
        )

    async def _cmd_remove(self, chat_id: str, arg: str) -> None:
        pid = await self.tracker.resolve_portfolio_id(arg) or arg.strip()
        if pid in self.cfg.tracked_portfolios:
            removed_name = self.cfg.tracked_portfolios.pop(pid)
            self.cfg.last_order_time_ms.pop(pid, None)
            self.cfg.save_state()
            self.tracker.remove_portfolio_state(pid)
            await self.send_message(
                f"🗑️ 已停止追蹤 <b>{html.escape(removed_name)}</b>。",
                chat_id=chat_id,
            )
        else:
            await self.send_message(
                f"⚠️ 名單中找不到 <code>{html.escape(pid)}</code>。",
                chat_id=chat_id,
            )

    async def _cmd_trades(self, chat_id: str) -> None:
        if not self.cfg.tracked_portfolios:
            await self.send_message(
                "📭 目前尚未追蹤任何交易員，請點選下方按鈕挑選或直接貼上帶單網址新增。",
                chat_id=chat_id,
                reply_markup={
                    "inline_keyboard": [[{"text": "🏆 查看熱門帶單員", "callback_data": "cb_menu:top"}]]
                },
            )
            return

        items = list(self.cfg.tracked_portfolios.items())
        blocks: List[str] = []
        for idx, (pid, name) in enumerate(items):
            if idx > 0:
                await asyncio.sleep(0.2)
            prof = await self.tracker.get_cached_profile(pid, fallback_name=name)
            fills = await self.tracker.fetch_recent_orders(pid, limit=10, merge_bursts=False)
            blocks.append(format_recent_trades_html(prof, fills, max_items=5))
        header = "📜 <b>最新交易紀錄</b> <i>(倉位佔交易員總本金%)</i>\n━━━━━━━━━━━━━━\n"
        sep = "\n──────────────\n"
        kb = {
            "inline_keyboard": [
                [
                    {"text": "🔄 重新整理", "callback_data": "cb_menu:trades"},
                    {"text": "🎛️ 管理名單", "callback_data": "cb_menu:manage"},
                ]
            ]
        }
        await self.send_message(header + sep.join(blocks), chat_id=chat_id, reply_markup=kb)

    async def _cmd_my_account(self, chat_id: str) -> None:
        summary = await self.executor.fetch_account_summary()
        kb: List[List[Dict[str, str]]] = [
            [{"text": "🔄 重新整理帳戶", "callback_data": "cb_menu:my"}]
        ]
        if self.executor.pending_orders:
            kb.append([{"text": "🛑 一鍵撤銷全部待成交限價單", "callback_data": "cb_cancel_all"}])
        await self.send_message(
            summary,
            chat_id=chat_id,
            reply_markup={"inline_keyboard": kb},
        )

    async def _cmd_settings(self, chat_id: str, arg: str) -> None:
        if not arg:
            text = (
                "⚙️ <b>限價跟單與風控參數</b>\n"
                "━━━━━━━━━━━━━━\n"
                f"• 單筆保證金 (<code>margin</code>)：<code>{self.cfg.trade_margin_usdt:g} USDT</code>\n"
                f"• 最大保證金上限 (<code>maxmargin</code>)：<code>{self.cfg.max_margin_usdt:g} USDT</code>\n"
                f"• 合約槓桿 (<code>leverage</code>)：<code>{self.cfg.trade_leverage}x</code>\n"
                f"• 限價模式 (<code>mode</code>)：<code>{self.cfg.limit_order_mode}</code>\n"
                f"• 滑點上限 (<code>slippage</code>)：<code>{self.cfg.limit_slippage_pct}%</code>\n"
                f"• 超時撤單 (<code>timeout</code>)：<code>{self.cfg.limit_order_timeout_sec}s</code>\n"
                f"• 平倉模式 (<code>exit</code>)：<code>{self.cfg.exit_order_type}</code>\n\n"
                "✏️ <b>快速修改範例：</b>\n"
                "<code>/set margin 30</code> ｜ <code>/set leverage 15</code> ｜ <code>/set slippage 0.05</code>"
            )
            await self.send_message(text, chat_id=chat_id)
            return

        tokens = arg.split()
        if len(tokens) != 2:
            await self.send_message(
                "❓ 格式：<code>/set 參數名稱 數值</code>（輸入 <code>/set</code> 可看完整說明）。",
                chat_id=chat_id,
            )
            return

        key, val = tokens[0].lower(), tokens[1].strip()
        try:
            if key == "margin":
                v = float(val)
                if v < 5.0:
                    raise ValueError("單筆保證金最低建議 5 USDT")
                self.cfg.trade_margin_usdt = v
                if self.cfg.max_margin_usdt < v:
                    self.cfg.max_margin_usdt = max(v * 3.0, 50.0)
            elif key == "maxmargin":
                v = float(val)
                if v < self.cfg.trade_margin_usdt:
                    raise ValueError("最大安全保證金不可低於單筆保證金")
                self.cfg.max_margin_usdt = v
            elif key == "leverage":
                lev = int(val)
                if not (1 <= lev <= 125):
                    raise ValueError("槓桿倍數須介於 1 ~ 125")
                self.cfg.trade_leverage = lev
            elif key == "slippage":
                sl = float(val)
                if not (0.0 <= sl <= 5.0):
                    raise ValueError("滑點百分比須介於 0 ~ 5.0")
                self.cfg.limit_slippage_pct = sl
            elif key == "timeout":
                to_sec = int(val)
                if to_sec < 15:
                    raise ValueError("超時秒數至少需 15 秒")
                self.cfg.limit_order_timeout_sec = to_sec
            elif key == "mode":
                m_up = val.upper()
                if m_up not in ("TARGET_PRICE", "POST_ONLY_BBO"):
                    raise ValueError("mode 僅支援 TARGET_PRICE 或 POST_ONLY_BBO")
                self.cfg.limit_order_mode = m_up
            elif key == "exit":
                e_up = val.upper()
                if e_up not in ("MARKET", "LIMIT"):
                    raise ValueError("exit 僅支援 MARKET 或 LIMIT")
                self.cfg.exit_order_type = e_up
            else:
                await self.send_message(
                    f"⚠️ 不支援的參數 <code>{html.escape(key)}</code>，請輸入 <code>/set</code> 查看可用項目。",
                    chat_id=chat_id,
                )
                return
        except ValueError as err:
            await self.send_message(f"⚠️ 數值錯誤：{html.escape(str(err))}", chat_id=chat_id)
            return

        self.cfg.save_state()
        await self.send_message(
            f"✅ 已更新 <code>{html.escape(key)}</code> = <code>{html.escape(val.upper() if key in ('mode', 'exit') else val)}</code> 並儲存生效！",
            chat_id=chat_id,
        )

    async def _cmd_autotrading(self, chat_id: str, arg: str) -> None:
        sub = arg.lower().strip()
        if sub in ("on", "true", "1"):
            if not self.cfg.has_trading_credentials():
                await self.send_message(
                    "⚠️ <b>尚未設定幣安 API 金鑰，無法開啟自動跟單！</b>\n\n"
                    "1️⃣ 在一對一私訊輸入 <code>/genkey</code> 一鍵產生免綁 IP 的 Ed25519 公私鑰。\n"
                    "2️⃣ 將公鑰貼到幣安後台開啟「合約權限」，再把 <code>BINANCE_API_KEY</code> 與 <code>BINANCE_ED25519_PRIVATE_KEY</code> 填入 Render 環境變數即可！",
                    chat_id=chat_id,
                )
                return
            self.cfg.enable_auto_trade = True
            self.cfg.save_state()
            await self.send_message(
                "🟢 <b>已開啟「限價單自動跟單」模式！</b>\n"
                f"• 配置：<code>{self.cfg.trade_margin_usdt:g} USDT</code> × <code>{self.cfg.trade_leverage}x</code>\n"
                f"• 策略：<code>{self.cfg.limit_order_mode}</code>（滑點上限 <code>{self.cfg.limit_slippage_pct}%</code>｜超時 <code>{self.cfg.limit_order_timeout_sec}s</code> 撤單）",
                chat_id=chat_id,
            )
        elif sub in ("off", "false", "0"):
            self.cfg.enable_auto_trade = False
            self.cfg.save_state()
            await self.send_message(
                "🔔 已切換回「<b>純即時通知模式</b>」（不執行自動下單）。",
                chat_id=chat_id,
            )
        else:
            state_txt = "開啟中 🟢" if self.cfg.enable_auto_trade else "關閉中（純通知） 🔔"
            toggle_btn = "🔔 關閉自動跟單" if self.cfg.enable_auto_trade else "🟢 開啟自動跟單"
            await self.send_message(
                f"目前自動限價跟單狀態：{state_txt}",
                chat_id=chat_id,
                reply_markup={
                    "inline_keyboard": [[{"text": toggle_btn, "callback_data": "cb_menu:toggle_auto"}]]
                },
            )

    async def _cmd_genkey(self, chat_id: str, chat_type: str) -> None:
        if chat_type != "private":
            await self.send_message(
                "🔒 為保護你的資金安全，<code>/genkey</code> 僅限在與機器人的<b>一對一私訊</b>中使用！",
                chat_id=chat_id,
            )
            return

        pub_pem, _, env_priv = generate_ed25519_keypair()
        msg = (
            "🔑 <b>已為你即時產生免綁 IP 專用 Ed25519 金鑰組！</b>\n\n"
            "<b>步驟 1：複製下方公鑰貼到幣安「建立自生成 API 金鑰」</b>：\n"
            f"<code>{html.escape(pub_pem)}</code>\n\n"
            "<b>步驟 2：勾選「啟用合約」（切勿勾選提現！），將 API Key 與下方私鑰填入 Render 環境變數：</b>\n"
            f"<code>{html.escape(env_priv)}</code>\n"
            "<i>（變數名稱：<code>BINANCE_ED25519_PRIVATE_KEY</code>）</i>\n\n"
            "⚠️ <b>安全提醒：貼進 Render 後請長按刪除本則訊息！</b>"
        )
        await self.send_message(msg, chat_id=chat_id)
