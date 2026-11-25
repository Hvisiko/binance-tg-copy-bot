from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Dict, List, Optional, Tuple

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .binance_tracker import PositionEvent
from .config import AppConfig

logger = logging.getLogger(__name__)

FAPI_MAINNET = "https://fapi.binance.com"
FAPI_TESTNET = "https://testnet.binancefuture.com"


def round_step(value: float | Decimal, step_str: str, round_up: bool = False) -> str:
    """Round price or quantity strictly to Binance tickSize / stepSize without float drift."""
    d_val = value if isinstance(value, Decimal) else Decimal(str(value))
    d_step = Decimal(str(step_str))
    if d_step <= 0:
        s = format(d_val, "f")
        return s.rstrip("0").rstrip(".") if "." in s else s
    rounding_mode = ROUND_UP if round_up else ROUND_DOWN
    quotient = (d_val / d_step).to_integral_value(rounding=rounding_mode)
    res = quotient * d_step
    s = format(res, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def compute_limit_entry_price(
    ref_price: float,
    side: str,
    slippage_pct: float,
    tick_size: str,
) -> str:
    """
    Calculate target limit price with bounded slippage tolerance using exact Decimal math.
    - BUY:  ref_price * (1 + slippage_pct / 100), rounded DOWN to tick_size
    - SELL: ref_price * (1 - slippage_pct / 100), rounded UP to tick_size
    """
    d_ref = Decimal(str(ref_price))
    d_factor = Decimal(str(max(0.0, slippage_pct))) / Decimal("100")
    if side.upper() == "BUY":
        raw_target = d_ref * (Decimal("1") + d_factor)
        return round_step(raw_target, tick_size, round_up=False)
    mult = max(Decimal("0.01"), Decimal("1") - d_factor)
    raw_target = d_ref * mult
    return round_step(raw_target, tick_size, round_up=True)


@dataclass
class SymbolFilters:
    symbol: str
    tick_size: str = "0.01"
    step_size: str = "0.001"
    min_qty: float = 0.001
    min_notional: float = 5.0


@dataclass
class PendingLimitOrder:
    order_id: int
    portfolio_id: str
    trader_name: str
    symbol: str
    side: str  # BUY or SELL
    position_side: str  # LONG or SHORT
    price_str: str
    qty_str: str
    created_at: float


@dataclass
class ExecutionResult:
    ok: bool
    summary: str
    order_id: Optional[int] = None


class BinanceAPIError(RuntimeError):
    def __init__(self, code: int, msg: str) -> None:
        super().__init__(f"Binance API 錯誤 [{code}]: {msg}")
        self.code = code
        self.api_msg = msg


class BinanceFuturesExecutor:
    """Executes Limit-Order entries, auto-cancel timeouts, and safe exits on Binance USDT-M Futures."""

    def __init__(self, config: AppConfig, client: Optional[httpx.AsyncClient] = None) -> None:
        self.cfg = config
        self.base_url = FAPI_TESTNET if config.binance_use_testnet else FAPI_MAINNET
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=15.0)

        self._ed25519_key: Optional[Ed25519PrivateKey] = None
        if config.binance_ed25519_private_key:
            try:
                key_bytes = config.binance_ed25519_private_key.encode("utf-8")
                loaded = serialization.load_pem_private_key(key_bytes, password=None)
                if isinstance(loaded, Ed25519PrivateKey):
                    self._ed25519_key = loaded
            except Exception as exc:
                logger.error("解析 BINANCE_ED25519_PRIVATE_KEY 失敗: %s", exc)

        self._symbol_filters: Dict[str, SymbolFilters] = {}
        self._dual_side_position: Optional[bool] = None
        self._leverage_configured: set[Tuple[str, int]] = set()
        self.pending_orders: Dict[int, PendingLimitOrder] = {}
        self._restore_pending_orders()

    def _restore_pending_orders(self) -> None:
        for row in self.cfg.saved_pending_orders:
            try:
                oid = int(row["order_id"])
                self.pending_orders[oid] = PendingLimitOrder(
                    order_id=oid,
                    portfolio_id=str(row["portfolio_id"]),
                    trader_name=str(row.get("trader_name", "")),
                    symbol=str(row["symbol"]),
                    side=str(row["side"]),
                    position_side=str(row["position_side"]),
                    price_str=str(row["price_str"]),
                    qty_str=str(row["qty_str"]),
                    created_at=float(row.get("created_at", time.time())),
                )
            except Exception:
                continue

    def _persist_pending_orders(self) -> None:
        serialized = [asdict(po) for po in self.pending_orders.values()]
        self.cfg.save_state(pending_orders=serialized)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _sign_query(self, params: Dict[str, Any]) -> str:
        clean = {k: str(v) for k, v in params.items() if v is not None}
        clean["timestamp"] = str(int(time.time() * 1000))
        clean.setdefault("recvWindow", "10000")
        query_str = urllib.parse.urlencode(clean)

        if self._ed25519_key is not None:
            sig_bytes = self._ed25519_key.sign(query_str.encode("ascii"))
            sig_b64 = base64.b64encode(sig_bytes).decode("ascii")
            return f"{query_str}&signature={urllib.parse.quote(sig_b64, safe='')}"

        sig_hex = hmac.new(
            self.cfg.binance_api_secret.encode("utf-8"),
            query_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query_str}&signature={sig_hex}"

    async def _signed_request(
        self, method: str, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Any:
        qs = self._sign_query(params or {})
        url = f"{self.base_url}{path}?{qs}"
        headers = {"X-MBX-APIKEY": self.cfg.binance_api_key}
        resp = await self.client.request(method, url, headers=headers)
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code >= 400:
            code = int(data.get("code", resp.status_code)) if isinstance(data, dict) else resp.status_code
            msg = data.get("msg", resp.text) if isinstance(data, dict) else resp.text
            raise BinanceAPIError(code, str(msg))
        return data

    async def ensure_metadata(self, symbol: str) -> SymbolFilters:
        if not self._symbol_filters:
            resp = await self.client.get(f"{self.base_url}/fapi/v1/exchangeInfo")
            resp.raise_for_status()
            info = resp.json()
            for sym_obj in info.get("symbols", []):
                s_name = sym_obj.get("symbol", "")
                tick_s = "0.01"
                step_s = "0.001"
                min_q = 0.001
                min_notional = 5.0
                for flt in sym_obj.get("filters", []):
                    ftype = flt.get("filterType")
                    if ftype == "PRICE_FILTER":
                        tick_s = flt.get("tickSize", tick_s)
                    elif ftype == "LOT_SIZE":
                        step_s = flt.get("stepSize", step_s)
                        min_q = float(flt.get("minQty", min_q))
                    elif ftype == "MIN_NOTIONAL":
                        raw_notional = flt.get("notional") or flt.get("minNotional")
                        if raw_notional is not None:
                            min_notional = float(raw_notional)
                self._symbol_filters[s_name] = SymbolFilters(
                    symbol=s_name,
                    tick_size=tick_s,
                    step_size=step_s,
                    min_qty=min_q,
                    min_notional=min_notional,
                )

        if self._dual_side_position is None:
            dual_data = await self._signed_request("GET", "/fapi/v1/positionSide/dual")
            self._dual_side_position = bool(
                dual_data.get("dualSidePosition", False)
                if isinstance(dual_data, dict)
                else False
            )

        return self._symbol_filters.get(symbol, SymbolFilters(symbol=symbol))

    async def _ensure_leverage(self, symbol: str, leverage: int) -> None:
        key = (symbol, leverage)
        if key in self._leverage_configured:
            return
        try:
            await self._signed_request(
                "POST",
                "/fapi/v1/leverage",
                {"symbol": symbol, "leverage": leverage},
            )
            self._leverage_configured.add(key)
        except Exception as exc:
            logger.warning("設定槓桿失敗 (%s %sx): %s", symbol, leverage, exc)

    async def _fetch_book_ticker(self, symbol: str) -> Tuple[float, float]:
        resp = await self.client.get(
            f"{self.base_url}/fapi/v1/ticker/bookTicker",
            params={"symbol": symbol},
        )
        resp.raise_for_status()
        d = resp.json()
        return float(d["bidPrice"]), float(d["askPrice"])

    async def _cancel_matching_pending_orders(
        self, portfolio_id: str, symbol: str, position_side: str
    ) -> List[str]:
        canceled_notes: List[str] = []
        to_cancel = [
            po
            for po in list(self.pending_orders.values())
            if po.portfolio_id == portfolio_id
            and po.symbol == symbol
            and po.position_side == position_side
        ]
        if not to_cancel:
            return canceled_notes

        for po in to_cancel:
            try:
                await self._signed_request(
                    "DELETE",
                    "/fapi/v1/order",
                    {"symbol": po.symbol, "orderId": po.order_id},
                )
                canceled_notes.append(
                    f"🛑 已自動撤銷尚未成交的開倉限價單：<code>{po.symbol}</code> #{po.order_id} @ {po.price_str}"
                )
            except Exception as exc:
                logger.debug("撤銷掛單 #%s 時略過: %s", po.order_id, exc)
            finally:
                self.pending_orders.pop(po.order_id, None)

        self._persist_pending_orders()
        return canceled_notes

    async def cancel_all_pending_orders(self) -> str:
        """Emergency cancel all bot-tracked pending limit entry orders."""
        if not self.pending_orders:
            return "📭 目前沒有任何待成交的跟單限價掛單。"
        if not self.cfg.has_trading_credentials():
            self.pending_orders.clear()
            self._persist_pending_orders()
            return "🧹 已清除本地掛單紀錄（尚未設定幣安 API Key）。"

        canceled_count = 0
        for oid, po in list(self.pending_orders.items()):
            try:
                await self._signed_request(
                    "DELETE",
                    "/fapi/v1/order",
                    {"symbol": po.symbol, "orderId": oid},
                )
                canceled_count += 1
            except Exception as exc:
                logger.debug("批量撤單 #%s 略過: %s", oid, exc)
            finally:
                self.pending_orders.pop(oid, None)

        self._persist_pending_orders()
        return f"🛑 已全數撤銷待成交限價跟單（共成功撤銷 <code>{canceled_count}</code> 筆）。"

    async def fetch_account_summary(self) -> str:
        """Fetch user's own Binance USDT-M Futures balance, active positions, and pending limit orders."""
        pending_lines = [
            f"  • <code>{po.symbol}</code> {po.position_side} #{po.order_id}｜委託價 <code>{po.price_str}</code> × <code>{po.qty_str}</code>"
            for po in self.pending_orders.values()
        ]
        pending_block = "\n".join(pending_lines) if pending_lines else "  （無待成交限價單）"

        if not self.cfg.has_trading_credentials():
            return (
                "💼 <b>我的跟單帳戶狀態</b>\n"
                "⚠️ 尚未設定 <code>BINANCE_API_KEY</code>，目前為純通知唯讀模式。\n"
                "💡 如需雲端免綁 IP 自動跟單，可直接在私訊輸入 <code>/genkey</code> 產生 Ed25519 金鑰！\n\n"
                f"📝 <b>機器人追蹤中限價掛單（{len(self.pending_orders)} 筆）：</b>\n{pending_block}"
            )

        try:
            balances = await self._signed_request("GET", "/fapi/v2/balance")
            risks = await self._signed_request("GET", "/fapi/v2/positionRisk")

            wallet_bal = 0.0
            avail_bal = 0.0
            if isinstance(balances, list):
                for b in balances:
                    if str(b.get("asset", "")).upper() == "USDT":
                        wallet_bal = float(b.get("balance", 0.0))
                        avail_bal = float(b.get("availableBalance", 0.0))
                        break

            pos_lines: List[str] = []
            total_upnl = 0.0
            if isinstance(risks, list):
                for r in risks:
                    amt = float(r.get("positionAmt", 0.0))
                    if abs(amt) <= 1e-9:
                        continue
                    sym = str(r.get("symbol", ""))
                    p_side = str(r.get("positionSide", "BOTH")).upper()
                    if p_side == "BOTH":
                        p_side = "LONG" if amt > 0 else "SHORT"
                    entry_p = float(r.get("entryPrice", 0.0))
                    mark_p = float(r.get("markPrice", entry_p))
                    upnl = float(r.get("unRealizedProfit", 0.0))
                    lev = int(float(r.get("leverage", self.cfg.trade_leverage)))
                    total_upnl += upnl
                    sign = "+" if upnl >= 0 else ""
                    tag = "🟢多" if p_side == "LONG" else "🔴空"
                    pos_lines.append(
                        f"  • <b>{sym}</b> {tag} <code>{lev}x</code>｜數量 <code>{abs(amt)}</code>\n"
                        f"    均價 <code>{entry_p:g}</code> → 標記 <code>{mark_p:g}</code>｜浮盈 <code>{sign}{upnl:,.2f} U</code>"
                    )

            pos_block = "\n".join(pos_lines) if pos_lines else "  （目前空倉中）"
            net_env = "🧪 模擬網 Testnet" if self.cfg.binance_use_testnet else "🌐 正式網 Mainnet"
            upnl_sign = "+" if total_upnl >= 0 else ""

            return (
                f"💼 <b>我的幣安合約帳戶（{net_env}）</b>\n"
                f"• 總餘額：<code>{wallet_bal:,.2f} USDT</code>（可用：<code>{avail_bal:,.2f} USDT</code>）\n"
                f"• 總浮動盈虧：<code>{upnl_sign}{total_upnl:,.2f} USDT</code>\n\n"
                f"📊 <b>當前持倉（{len(pos_lines)} 筆）：</b>\n{pos_block}\n\n"
                f"📝 <b>待成交限價掛單（{len(self.pending_orders)} 筆）：</b>\n{pending_block}"
            )
        except Exception as exc:
            return f"❌ <b>查詢幣安帳戶失敗</b>：{exc}"

    async def _get_my_open_qty(self, symbol: str, position_side: str) -> float:
        risks = await self._signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if not isinstance(risks, list):
            return 0.0
        for row in risks:
            amt = float(row.get("positionAmt", 0.0))
            row_side = str(row.get("positionSide", "BOTH")).upper()
            if self._dual_side_position:
                if row_side == position_side and abs(amt) > 1e-9:
                    return abs(amt)
            else:
                if position_side == "LONG" and amt > 1e-9:
                    return amt
                if position_side == "SHORT" and amt < -1e-9:
                    return abs(amt)
        return 0.0

    async def _place_entry_limit_order(
        self,
        symbol: str,
        order_side: str,
        position_side: str,
        notional: float,
        ref_price: float,
        filters: SymbolFilters,
    ) -> Tuple[Dict[str, Any], str, str, str]:
        """
        Place a LIMIT entry order.
        In POST_ONLY_BBO mode, retry fresh BBO up to 2 times if GTX is rejected (-5022 or EXPIRED),
        then fall back to TARGET_PRICE GTC within slippage tolerance so valid signals aren't dropped.
        """
        mode = self.cfg.limit_order_mode

        if mode == "POST_ONLY_BBO":
            for attempt in range(2):
                bid, ask = await self._fetch_book_ticker(symbol)
                anchor = bid if order_side == "BUY" else ask
                price_str = round_step(
                    anchor, filters.tick_size, round_up=(order_side == "SELL")
                )
                qty_str = self._calc_qty_str(notional, float(price_str), filters)
                params: Dict[str, Any] = {
                    "symbol": symbol,
                    "side": order_side,
                    "type": "LIMIT",
                    "timeInForce": "GTX",
                    "quantity": qty_str,
                    "price": price_str,
                }
                if self._dual_side_position:
                    params["positionSide"] = position_side
                try:
                    resp = await self._signed_request("POST", "/fapi/v1/order", params)
                    if str(resp.get("status", "NEW")) != "EXPIRED":
                        return resp, price_str, qty_str, "GTX"
                except BinanceAPIError as api_err:
                    if api_err.code != -5022:
                        raise
                if attempt == 0:
                    await asyncio.sleep(0.15)

        # TARGET_PRICE (or fallback when BBO moved too fast for GTX)
        effective_ref = ref_price
        if effective_ref <= 0:
            bid, ask = await self._fetch_book_ticker(symbol)
            effective_ref = ask if order_side == "BUY" else bid
        price_str = compute_limit_entry_price(
            effective_ref, order_side, self.cfg.limit_slippage_pct, filters.tick_size
        )
        qty_str = self._calc_qty_str(notional, float(price_str), filters)
        params = {
            "symbol": symbol,
            "side": order_side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": qty_str,
            "price": price_str,
        }
        if self._dual_side_position:
            params["positionSide"] = position_side
        resp = await self._signed_request("POST", "/fapi/v1/order", params)
        return resp, price_str, qty_str, "GTC"

    @staticmethod
    def _calc_qty_str(notional: float, limit_price: float, filters: SymbolFilters) -> str:
        raw_qty = notional / max(limit_price, 1e-9)
        qty_str = round_step(raw_qty, filters.step_size, round_up=False)
        if float(qty_str) < filters.min_qty:
            qty_str = round_step(filters.min_qty, filters.step_size, round_up=True)
        if float(qty_str) * limit_price < filters.min_notional:
            min_needed_qty = (filters.min_notional * 1.01) / max(limit_price, 1e-9)
            qty_str = round_step(
                max(filters.min_qty, min_needed_qty), filters.step_size, round_up=True
            )
        return qty_str

    async def handle_event(self, event: PositionEvent) -> ExecutionResult:
        """Translate a lead-trader PositionEvent into a Limit-Order entry or safe exit."""
        if not self.cfg.enable_auto_trade:
            return ExecutionResult(ok=True, summary="")
        if not self.cfg.has_trading_credentials():
            return ExecutionResult(
                ok=False,
                summary="⚠️ 已開啟自動跟單但尚未設定 <code>BINANCE_API_KEY</code> 金鑰，略過下單。",
            )

        try:
            filters = await self.ensure_metadata(event.symbol)
            leverage = self.cfg.trade_leverage
            await self._ensure_leverage(event.symbol, leverage)

            if event.action in ("OPEN", "INCREASE"):
                order_side = "BUY" if event.position_side == "LONG" else "SELL"
                margin = (
                    self.cfg.trade_margin_usdt
                    if event.action == "OPEN"
                    else max(5.0, self.cfg.trade_margin_usdt * 0.5)
                )
                target_notional = margin * leverage
                min_req_notional = filters.min_notional * 1.02

                if target_notional < min_req_notional:
                    needed_margin = min_req_notional / leverage
                    if needed_margin > self.cfg.max_margin_usdt:
                        return ExecutionResult(
                            ok=False,
                            summary=(
                                f"⚠️ <b>風控攔截：標的最低名目價值過高</b>\n"
                                f"• <code>{event.symbol}</code> 最低下單門檻為 <code>{filters.min_notional:g} USDT</code> "
                                f"（在 <code>{leverage}x</code> 下需約 <code>{needed_margin:.1f} U</code> 保證金，超過安全上限 <code>{self.cfg.max_margin_usdt:g} U</code>）。\n"
                                f"• 如需跟單大幣種，可輸入 <code>/set leverage 20</code> 或 <code>/set margin {int(needed_margin) + 1}</code>。"
                            ),
                        )
                    notional = min_req_notional
                else:
                    notional = target_notional

                order_resp, price_str, qty_str, tif = await self._place_entry_limit_order(
                    symbol=event.symbol,
                    order_side=order_side,
                    position_side=event.position_side,
                    notional=notional,
                    ref_price=event.ref_price,
                    filters=filters,
                )
                oid = int(order_resp["orderId"])
                status = str(order_resp.get("status", "NEW"))

                if status in ("NEW", "PARTIALLY_FILLED"):
                    self.pending_orders[oid] = PendingLimitOrder(
                        order_id=oid,
                        portfolio_id=event.portfolio_id,
                        trader_name=event.trader_name,
                        symbol=event.symbol,
                        side=order_side,
                        position_side=event.position_side,
                        price_str=price_str,
                        qty_str=qty_str,
                        created_at=time.time(),
                    )
                    self._persist_pending_orders()
                    return ExecutionResult(
                        ok=True,
                        order_id=oid,
                        summary=(
                            f"📝 <b>已送出限價跟單（{tif}）</b>\n"
                            f"• 訂單號：<code>#{oid}</code>（狀態：{status}）\n"
                            f"• 委託價：<code>{price_str}</code> ｜ 數量：<code>{qty_str}</code>\n"
                            f"• 超時保護：{self.cfg.limit_order_timeout_sec} 秒未成交或帶單員平倉將自動撤單"
                        ),
                    )
                return ExecutionResult(
                    ok=True,
                    order_id=oid,
                    summary=(
                        f"✅ <b>限價跟單已立即成交！</b>\n"
                        f"• 訂單號：<code>#{oid}</code> ｜ 限價上限：<code>{price_str}</code> ｜ 數量：<code>{qty_str}</code>"
                    ),
                )

            # CLOSE or DECREASE
            notes = await self._cancel_matching_pending_orders(
                event.portfolio_id, event.symbol, event.position_side
            )
            my_qty = await self._get_my_open_qty(event.symbol, event.position_side)
            if my_qty <= 1e-9:
                extra = "\n".join(notes)
                msg = extra if extra else "ℹ️ 你的帳戶目前無對應持倉，無需執行平倉。"
                return ExecutionResult(ok=True, summary=msg)

            if event.action == "DECREASE" and event.prev_qty > 0:
                ratio = min(1.0, max(0.1, event.delta_qty / event.prev_qty))
                close_qty = my_qty * ratio
            else:
                close_qty = my_qty

            qty_str = round_step(close_qty, filters.step_size, round_up=False)
            if float(qty_str) <= 0:
                qty_str = round_step(my_qty, filters.step_size, round_up=False)

            close_side = "SELL" if event.position_side == "LONG" else "BUY"
            params: Dict[str, Any] = {
                "symbol": event.symbol,
                "side": close_side,
                "quantity": qty_str,
            }
            if self._dual_side_position:
                params["positionSide"] = event.position_side
            else:
                params["reduceOnly"] = "true"

            if self.cfg.exit_order_type == "LIMIT":
                bid, ask = await self._fetch_book_ticker(event.symbol)
                ref_p = (
                    event.ref_price
                    if event.ref_price > 0
                    else (bid if close_side == "SELL" else ask)
                )
                price_str = compute_limit_entry_price(
                    ref_p, close_side, self.cfg.limit_slippage_pct, filters.tick_size
                )
                params["type"] = "LIMIT"
                params["timeInForce"] = "GTC"
                params["price"] = price_str
            else:
                params["type"] = "MARKET"

            order_resp = await self._signed_request("POST", "/fapi/v1/order", params)
            oid = int(order_resp["orderId"])
            prefix = ("\n".join(notes) + "\n") if notes else ""
            return ExecutionResult(
                ok=True,
                order_id=oid,
                summary=(
                    f"{prefix}🛡️ <b>已同步執行平倉／減倉（{params['type']}）</b>\n"
                    f"• 訂單號：<code>#{oid}</code> ｜ 數量：<code>{qty_str}</code>"
                ),
            )
        except Exception as exc:
            logger.error("執行跟單失敗: %s", exc)
            return ExecutionResult(ok=False, summary=f"❌ <b>自動跟單執行失敗</b>：{exc}")

    async def check_pending_orders(self) -> List[str]:
        """Inspect open limit orders; notify on fill or auto-cancel on timeout."""
        if not self.pending_orders or not self.cfg.has_trading_credentials():
            return []

        notifications: List[str] = []
        now = time.time()
        changed = False

        for oid, po in list(self.pending_orders.items()):
            try:
                status_data = await self._signed_request(
                    "GET",
                    "/fapi/v1/order",
                    {"symbol": po.symbol, "orderId": oid},
                )
                st = str(status_data.get("status", ""))
                exec_qty = float(status_data.get("executedQty", 0.0))
                avg_price = status_data.get("avgPrice", po.price_str)

                if st == "FILLED":
                    self.pending_orders.pop(oid, None)
                    changed = True
                    notifications.append(
                        f"✅ <b>限價跟單已成交！</b>\n"
                        f"• 帶單員：{po.trader_name}\n"
                        f"• 標的：<code>{po.symbol}</code> ({po.position_side})\n"
                        f"• 成交均價：<code>{avg_price}</code> ｜ 成交數量：<code>{exec_qty}</code>"
                    )
                elif st in ("CANCELED", "EXPIRED", "REJECTED"):
                    self.pending_orders.pop(oid, None)
                    changed = True
                elif now - po.created_at >= self.cfg.limit_order_timeout_sec:
                    await self._signed_request(
                        "DELETE",
                        "/fapi/v1/order",
                        {"symbol": po.symbol, "orderId": oid},
                    )
                    self.pending_orders.pop(oid, None)
                    changed = True
                    notifications.append(
                        f"⏱️ <b>限價單超時未完全成交，已自動撤單</b>\n"
                        f"• 標的：<code>{po.symbol}</code> ({po.position_side}) #{oid}\n"
                        f"• 原委託價：<code>{po.price_str}</code>（已成交數量：<code>{exec_qty}</code>）"
                    )
            except Exception as exc:
                logger.warning("檢查限價單 #%s 狀態失敗: %s", oid, exc)

        if changed:
            self._persist_pending_orders()
        return notifications
