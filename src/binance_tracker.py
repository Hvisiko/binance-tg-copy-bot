from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .config import extract_portfolio_id

logger = logging.getLogger(__name__)

BINANCE_WEB_BASE = "https://www.binance.com"
COPY_TRADE_PUBLIC = f"{BINANCE_WEB_BASE}/bapi/futures/v1/public/future/copy-trade"
COPY_TRADE_FRIENDLY = f"{BINANCE_WEB_BASE}/bapi/futures/v1/friendly/future/copy-trade"
LEADERBOARD_PUBLIC_V2 = f"{BINANCE_WEB_BASE}/bapi/futures/v2/public/future/leaderboard"
LEADERBOARD_PUBLIC_V1 = f"{BINANCE_WEB_BASE}/bapi/futures/v1/public/future/leaderboard"


@dataclass
class TraderProfile:
    portfolio_id: str
    nickname: str
    roi_30d: Optional[float] = None
    pnl_30d: Optional[float] = None
    aum: Optional[float] = None
    sharp_ratio: Optional[float] = None
    win_rate: Optional[float] = None
    url: str = ""


@dataclass
class TraderPosition:
    symbol: str
    position_side: str  # "LONG" or "SHORT"
    position_amt: float  # absolute quantity (> 0)
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: int
    margin_type: str = "cross"

    @property
    def key(self) -> Tuple[str, str]:
        return (self.symbol, self.position_side)


@dataclass
class TraderOrderFill:
    order_id: str
    symbol: str
    side: str  # "BUY" or "SELL"
    position_side: str  # "LONG" or "SHORT" or "BOTH"
    order_type: str
    avg_price: float
    executed_qty: float
    realized_pnl: float
    order_time_ms: int

    def infer_action_and_pos_side(self) -> Tuple[str, str]:
        """
        Infer ('OPEN' | 'CLOSE', 'LONG' | 'SHORT') from a single executed trade record.
        """
        s = self.side.upper()
        ps = self.position_side.upper()
        has_realized = abs(self.realized_pnl) > 1e-9

        if ps in ("LONG", "SHORT"):
            is_open = (s == "BUY" and ps == "LONG") or (s == "SELL" and ps == "SHORT")
            return ("OPEN" if is_open else "CLOSE"), ps

        # One-way mode ("BOTH" or empty): non-zero realized PnL indicates closing an existing position
        if has_realized:
            inferred_ps = "LONG" if s == "SELL" else "SHORT"
            return "CLOSE", inferred_ps

        inferred_ps = "LONG" if s == "BUY" else "SHORT"
        return "OPEN", inferred_ps


@dataclass
class PositionEvent:
    portfolio_id: str
    trader_name: str
    action: str  # "OPEN", "INCREASE", "DECREASE", "CLOSE"
    symbol: str
    position_side: str  # "LONG" or "SHORT"
    prev_qty: float
    curr_qty: float
    delta_qty: float
    ref_price: float  # Executed trade price for display & limit order calculation
    entry_price: float
    mark_price: float
    leverage: int
    unrealized_pnl: float = 0.0
    realized_pnl: Optional[float] = None
    event_time_ms: int = 0
    order_side: str = ""  # "BUY" or "SELL"
    order_type: str = "LIMIT"
    order_id: str = ""


def _safe_float(val: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if val is None or val == "":
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_float_val(val: Any, default: float = 0.0) -> float:
    res = _safe_float(val, default)
    return default if res is None else res


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        if val is None or val == "":
            return default
        return int(float(val))
    except (TypeError, ValueError):
        return default


def aggregate_burst_fills(
    fills: List[TraderOrderFill], window_ms: int = 5000
) -> List[TraderOrderFill]:
    """
    Merge matching-engine partial fills of the same order/burst (same symbol, side, position_side
    within `window_ms`) into a single consolidated trade with volume-weighted average price.
    Prevents 1 large order split into 10 micro-fills from spamming 10 Telegram alerts or 10 copy orders!
    """
    if not fills:
        return []

    sorted_fills = sorted(fills, key=lambda x: x.order_time_ms)
    merged: List[TraderOrderFill] = []

    for f in sorted_fills:
        if merged:
            prev = merged[-1]
            same_dir = (
                prev.symbol == f.symbol
                and prev.side == f.side
                and prev.position_side == f.position_side
            )
            within_window = abs(f.order_time_ms - prev.order_time_ms) <= window_ms
            if same_dir and within_window:
                total_qty = prev.executed_qty + f.executed_qty
                vwap = (
                    (prev.avg_price * prev.executed_qty + f.avg_price * f.executed_qty)
                    / total_qty
                    if total_qty > 0
                    else f.avg_price
                )
                prev.avg_price = vwap
                prev.executed_qty = total_qty
                prev.realized_pnl += f.realized_pnl
                prev.order_time_ms = max(prev.order_time_ms, f.order_time_ms)
                continue

        merged.append(
            TraderOrderFill(
                order_id=f.order_id,
                symbol=f.symbol,
                side=f.side,
                position_side=f.position_side,
                order_type=f.order_type,
                avg_price=f.avg_price,
                executed_qty=f.executed_qty,
                realized_pnl=f.realized_pnl,
                order_time_ms=f.order_time_ms,
            )
        )
    return merged


def fill_to_event(
    portfolio_id: str, trader_name: str, fill: TraderOrderFill, default_leverage: int = 10
) -> PositionEvent:
    action, p_side = fill.infer_action_and_pos_side()
    is_opening = action == "OPEN"
    return PositionEvent(
        portfolio_id=portfolio_id,
        trader_name=trader_name,
        action=action,
        symbol=fill.symbol,
        position_side=p_side,
        prev_qty=0.0 if is_opening else fill.executed_qty,
        curr_qty=fill.executed_qty if is_opening else 0.0,
        delta_qty=fill.executed_qty,
        ref_price=fill.avg_price,
        entry_price=fill.avg_price,
        mark_price=fill.avg_price,
        leverage=default_leverage,
        realized_pnl=fill.realized_pnl if (not is_opening or abs(fill.realized_pnl) > 1e-9) else None,
        event_time_ms=fill.order_time_ms,
        order_side=fill.side,
        order_type=fill.order_type,
        order_id=fill.order_id,
    )


def diff_positions(
    portfolio_id: str,
    trader_name: str,
    prev_map: Dict[Tuple[str, str], TraderPosition],
    curr_map: Dict[Tuple[str, str], TraderPosition],
    recent_fills: Optional[List[TraderOrderFill]] = None,
    eps: float = 1e-9,
) -> List[PositionEvent]:
    """Compare previous and current open positions (kept for legacy UID compatibility & tests)."""
    events: List[PositionEvent] = []
    now_ms = int(time.time() * 1000)
    fills = recent_fills or []

    def _find_matching_fill(sym: str, p_side: str, is_increase: bool) -> Optional[TraderOrderFill]:
        expected_side = (
            ("BUY" if p_side == "LONG" else "SELL")
            if is_increase
            else ("SELL" if p_side == "LONG" else "BUY")
        )
        candidates = [
            f
            for f in fills
            if f.symbol == sym
            and f.side == expected_side
            and (f.position_side in (p_side, "BOTH", ""))
            and f.avg_price > 0
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x.order_time_ms, reverse=True)
        return candidates[0]

    all_keys = set(prev_map.keys()) | set(curr_map.keys())
    for key in sorted(all_keys):
        prev_pos = prev_map.get(key)
        curr_pos = curr_map.get(key)

        if prev_pos is None and curr_pos is not None and curr_pos.position_amt > eps:
            fill = _find_matching_fill(curr_pos.symbol, curr_pos.position_side, True)
            ref_price = fill.avg_price if fill else (curr_pos.entry_price or curr_pos.mark_price)
            events.append(
                PositionEvent(
                    portfolio_id=portfolio_id,
                    trader_name=trader_name,
                    action="OPEN",
                    symbol=curr_pos.symbol,
                    position_side=curr_pos.position_side,
                    prev_qty=0.0,
                    curr_qty=curr_pos.position_amt,
                    delta_qty=curr_pos.position_amt,
                    ref_price=ref_price,
                    entry_price=curr_pos.entry_price,
                    mark_price=curr_pos.mark_price,
                    leverage=curr_pos.leverage,
                    unrealized_pnl=curr_pos.unrealized_pnl,
                    event_time_ms=fill.order_time_ms if fill else now_ms,
                )
            )
        elif prev_pos is not None and (curr_pos is None or curr_pos.position_amt <= eps):
            fill = _find_matching_fill(prev_pos.symbol, prev_pos.position_side, False)
            ref_price = fill.avg_price if fill else (prev_pos.mark_price or prev_pos.entry_price)
            events.append(
                PositionEvent(
                    portfolio_id=portfolio_id,
                    trader_name=trader_name,
                    action="CLOSE",
                    symbol=prev_pos.symbol,
                    position_side=prev_pos.position_side,
                    prev_qty=prev_pos.position_amt,
                    curr_qty=0.0,
                    delta_qty=prev_pos.position_amt,
                    ref_price=ref_price,
                    entry_price=prev_pos.entry_price,
                    mark_price=ref_price,
                    leverage=prev_pos.leverage,
                    unrealized_pnl=0.0,
                    realized_pnl=fill.realized_pnl if fill else None,
                    event_time_ms=fill.order_time_ms if fill else now_ms,
                )
            )
        elif prev_pos is not None and curr_pos is not None:
            diff = curr_pos.position_amt - prev_pos.position_amt
            if abs(diff) <= eps:
                continue
            if diff > eps:
                fill = _find_matching_fill(curr_pos.symbol, curr_pos.position_side, True)
                est_price = curr_pos.entry_price
                if prev_pos.entry_price > 0 and curr_pos.entry_price > 0:
                    est_calc = (
                        curr_pos.position_amt * curr_pos.entry_price
                        - prev_pos.position_amt * prev_pos.entry_price
                    ) / diff
                    if est_calc > 0:
                        est_price = est_calc
                ref_price = fill.avg_price if fill else est_price
                events.append(
                    PositionEvent(
                        portfolio_id=portfolio_id,
                        trader_name=trader_name,
                        action="INCREASE",
                        symbol=curr_pos.symbol,
                        position_side=curr_pos.position_side,
                        prev_qty=prev_pos.position_amt,
                        curr_qty=curr_pos.position_amt,
                        delta_qty=diff,
                        ref_price=ref_price,
                        entry_price=curr_pos.entry_price,
                        mark_price=curr_pos.mark_price,
                        leverage=curr_pos.leverage,
                        unrealized_pnl=curr_pos.unrealized_pnl,
                        event_time_ms=fill.order_time_ms if fill else now_ms,
                    )
                )
            else:
                delta = abs(diff)
                fill = _find_matching_fill(curr_pos.symbol, curr_pos.position_side, False)
                ref_price = fill.avg_price if fill else (curr_pos.mark_price or curr_pos.entry_price)
                events.append(
                    PositionEvent(
                        portfolio_id=portfolio_id,
                        trader_name=trader_name,
                        action="DECREASE",
                        symbol=curr_pos.symbol,
                        position_side=curr_pos.position_side,
                        prev_qty=prev_pos.position_amt,
                        curr_qty=curr_pos.position_amt,
                        delta_qty=delta,
                        ref_price=ref_price,
                        entry_price=curr_pos.entry_price,
                        mark_price=curr_pos.mark_price,
                        leverage=curr_pos.leverage,
                        unrealized_pnl=curr_pos.unrealized_pnl,
                        realized_pnl=fill.realized_pnl if fill else None,
                        event_time_ms=fill.order_time_ms if fill else now_ms,
                    )
                )
    return events


class BinanceCopyTracker:
    """Monitors Binance Copy-Trading Lead Portfolios directly via their Latest Trade / Order History."""

    def __init__(self, cookie: str = "", client: Optional[httpx.AsyncClient] = None) -> None:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "clienttype": "web",
            "lang": "zh-TC",
            "Origin": BINANCE_WEB_BASE,
            "Referer": f"{BINANCE_WEB_BASE}/zh-TC/copy-trading",
        }
        if cookie:
            headers["Cookie"] = cookie

        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            headers=headers, timeout=15.0, follow_redirects=True
        )

        # Runtime baseline & tracking state per portfolio_id
        self._initialized: Dict[str, bool] = {}
        self._seen_order_ids: Dict[str, set[str]] = {}
        self._last_order_time_ms: Dict[str, int] = {}
        self._positions_cache: Dict[str, Dict[Tuple[str, str], TraderPosition]] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @staticmethod
    def build_trader_url(portfolio_id: str) -> str:
        if portfolio_id.startswith("uid:"):
            uid = portfolio_id.split(":", 1)[1]
            return f"{BINANCE_WEB_BASE}/zh-TC/futures-activity/leaderboard/user/um?encryptedUid={uid}"
        return f"{BINANCE_WEB_BASE}/zh-TC/copy-trading/lead-details/{portfolio_id}"

    async def resolve_portfolio_id(self, raw: str) -> Optional[str]:
        """
        Extract portfolioId directly, OR resolve Binance App share short-links
        (e.g. https://app.binance.com/zh-TW/qr/dplk... or safu.im/...).
        """
        direct = extract_portfolio_id(raw)
        if direct:
            return direct

        text = raw.strip()
        if not text.startswith(("http://", "https://")):
            return None
        if not any(host in text.lower() for host in ("binance.com", "binance.me", "safu.im")):
            return None

        try:
            resp = await self.client.get(text, follow_redirects=True)
            pid = extract_portfolio_id(str(resp.url))
            if pid:
                return pid
            pid_body = extract_portfolio_id(resp.text[:50000])
            if pid_body:
                return pid_body
        except Exception as exc:
            logger.debug("解析幣安短網址失敗 (%s): %s", text, exc)
        return None

    async def fetch_top_portfolios(self, limit: int = 5) -> List[TraderProfile]:
        """Fetch top-performing public Copy-Trading lead traders (30D ROI)."""
        payload = {
            "pageNumber": 1,
            "pageSize": max(1, min(20, limit)),
            "timeRange": "30D",
            "dataType": "ROI",
            "favoriteOnly": False,
            "hideFull": False,
            "nickname": "",
            "order": "DESC",
            "userAsset": 0,
            "portfolioType": "PUBLIC",
        }
        for base in (COPY_TRADE_FRIENDLY, COPY_TRADE_PUBLIC):
            url = f"{base}/home-page/query-list"
            try:
                resp = await self.client.post(url, json=payload)
                if resp.status_code != 200:
                    continue
                body = resp.json()
                data = body.get("data") or {}
                rows = data.get("list") or []
                if not rows:
                    continue
                profiles: List[TraderProfile] = []
                for row in rows[:limit]:
                    pid = str(row.get("leadPortfolioId") or row.get("portfolioId") or "").strip()
                    if not pid:
                        continue
                    profiles.append(
                        TraderProfile(
                            portfolio_id=pid,
                            nickname=str(
                                row.get("nickname")
                                or row.get("nickName")
                                or f"Trader-{pid[-6:]}"
                            ),
                            roi_30d=_safe_float(row.get("roi"), None),
                            pnl_30d=_safe_float(row.get("pnl"), None),
                            aum=_safe_float(row.get("aum") or row.get("aumAmount"), None),
                            sharp_ratio=_safe_float(row.get("sharpRatio"), None),
                            win_rate=_safe_float(row.get("winRate"), None),
                            url=self.build_trader_url(pid),
                        )
                    )
                if profiles:
                    return profiles
            except Exception as exc:
                logger.warning("fetch_top_portfolios failed on %s: %s", url, exc)
        return []

    async def fetch_trader_detail(self, portfolio_id: str) -> TraderProfile:
        """Fetch nickname & performance summary for a portfolioId (or legacy encryptedUid)."""
        if portfolio_id.startswith("uid:"):
            uid = portfolio_id.split(":", 1)[1]
            for base in (LEADERBOARD_PUBLIC_V2, LEADERBOARD_PUBLIC_V1):
                try:
                    resp = await self.client.post(
                        f"{base}/getOtherLeaderboardBaseInfo",
                        json={"encryptedUid": uid},
                    )
                    if resp.status_code == 200:
                        data = (resp.json() or {}).get("data") or {}
                        nick = str(data.get("nickName") or f"UID-{uid[:6]}")
                        return TraderProfile(
                            portfolio_id=portfolio_id,
                            nickname=nick,
                            url=self.build_trader_url(portfolio_id),
                        )
                except Exception:
                    pass
            return TraderProfile(
                portfolio_id=portfolio_id,
                nickname=f"UID-{uid[:6]}",
                url=self.build_trader_url(portfolio_id),
            )

        detail_routes = [
            f"{COPY_TRADE_FRIENDLY}/lead-portfolio/detail",
            f"{COPY_TRADE_PUBLIC}/lead-portfolio/detail",
            f"{COPY_TRADE_FRIENDLY}/lead-data/detail",
        ]
        for url in detail_routes:
            try:
                resp = await self.client.get(url, params={"portfolioId": portfolio_id})
                if resp.status_code in (404, 405):
                    resp = await self.client.post(url, json={"portfolioId": portfolio_id})
                if resp.status_code != 200:
                    continue
                body = resp.json()
                data = body.get("data")
                if isinstance(data, dict) and data:
                    nick = str(
                        data.get("nickname")
                        or data.get("nickName")
                        or data.get("name")
                        or f"Trader-{portfolio_id[-6:]}"
                    )
                    return TraderProfile(
                        portfolio_id=portfolio_id,
                        nickname=nick,
                        roi_30d=_safe_float(data.get("roi"), None),
                        pnl_30d=_safe_float(
                            data.get("pnl") or data.get("netProfitAmount"), None
                        ),
                        aum=_safe_float(
                            data.get("aumAmount") or data.get("currentCopyAmount"), None
                        ),
                        sharp_ratio=_safe_float(data.get("sharpRatio"), None),
                        win_rate=_safe_float(data.get("winRate"), None),
                        url=self.build_trader_url(portfolio_id),
                    )
            except Exception as exc:
                logger.debug("fetch_trader_detail error (%s): %s", url, exc)

        return TraderProfile(
            portfolio_id=portfolio_id,
            nickname=f"Trader-{portfolio_id[-6:]}",
            url=self.build_trader_url(portfolio_id),
        )

    @staticmethod
    def _extract_list_rows(data: Any) -> Optional[List[Dict[str, Any]]]:
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            for key in ("list", "rows", "owsOrderList", "orders", "trades", "data"):
                val = data.get(key)
                if isinstance(val, list):
                    return [r for r in val if isinstance(r, dict)]
        return None

    @staticmethod
    def _parse_fill_rows(rows: List[Dict[str, Any]]) -> List[TraderOrderFill]:
        fills: List[TraderOrderFill] = []
        for r in rows:
            symbol = str(r.get("symbol") or r.get("pair") or "").strip().upper()
            if not symbol:
                continue
            qty_raw = (
                r.get("executedQty")
                if r.get("executedQty") is not None
                else r.get("qty")
                if r.get("qty") is not None
                else r.get("amount")
                if r.get("amount") is not None
                else r.get("volume")
            )
            qty = abs(_safe_float_val(qty_raw, 0.0))
            if qty <= 1e-9:
                continue

            price_raw = (
                r.get("avgPrice")
                if _safe_float_val(r.get("avgPrice"), 0.0) > 0
                else r.get("price")
                if _safe_float_val(r.get("price"), 0.0) > 0
                else r.get("avgCost")
            )
            avg_p = _safe_float_val(price_raw, 0.0)
            if avg_p <= 0:
                continue

            ts_raw = (
                r.get("orderTime")
                or r.get("time")
                or r.get("tradeTime")
                or r.get("orderUpdateTime")
                or r.get("updateTime")
            )
            ts = _safe_int(ts_raw, 0)

            side = str(r.get("side") or r.get("action") or "").strip().upper()
            pos_side = str(r.get("positionSide") or "BOTH").strip().upper()
            if side not in ("BUY", "SELL"):
                if pos_side == "SHORT":
                    side = "SELL"
                else:
                    side = "BUY"

            pnl_raw = (
                r.get("totalPnl")
                if r.get("totalPnl") is not None
                else r.get("realizedPnl")
                if r.get("realizedPnl") is not None
                else r.get("realizedProfit")
                if r.get("realizedProfit") is not None
                else r.get("pnl")
            )
            rpnl = _safe_float_val(pnl_raw, 0.0)

            oid = str(
                r.get("orderId")
                or r.get("tradeId")
                or r.get("id")
                or f"{symbol}-{side}-{pos_side}-{ts}-{qty:g}-{avg_p:g}"
            )
            otype = str(r.get("type") or r.get("orderType") or "LIMIT").strip().upper()

            fills.append(
                TraderOrderFill(
                    order_id=oid,
                    symbol=symbol,
                    side=side,
                    position_side=pos_side,
                    order_type=otype,
                    avg_price=avg_p,
                    executed_qty=qty,
                    realized_pnl=rpnl,
                    order_time_ms=ts,
                )
            )
        return fills

    async def fetch_recent_orders(
        self, portfolio_id: str, limit: int = 50, merge_bursts: bool = False
    ) -> Optional[List[TraderOrderFill]]:
        """
        Fetch latest executed trade / order history records for a Copy-Trading portfolio.
        Returns:
          - List[TraderOrderFill] sorted ascending by order_time_ms (oldest -> newest) when reachable
          - None if all history endpoints were unreachable / unauthorized
        """
        if portfolio_id.startswith("uid:"):
            return None

        now_ms = int(time.time() * 1000)
        start_30d_ms = now_ms - 30 * 86400 * 1000
        payload_with_time = {
            "portfolioId": portfolio_id,
            "startTime": start_30d_ms,
            "endTime": now_ms,
            "pageSize": limit,
            "pageNumber": 1,
        }
        payload_paged = {
            "portfolioId": portfolio_id,
            "pageNumber": 1,
            "pageSize": limit,
        }

        trade_and_order_urls = [
            f"{COPY_TRADE_FRIENDLY}/lead-portfolio/trade-history",
            f"{COPY_TRADE_PUBLIC}/lead-portfolio/trade-history",
            f"{COPY_TRADE_FRIENDLY}/lead-data/trade-history",
            f"{COPY_TRADE_FRIENDLY}/lead-portfolio/order-history",
            f"{COPY_TRADE_PUBLIC}/lead-portfolio/order-history",
            f"{COPY_TRADE_FRIENDLY}/lead-data/order-history",
        ]

        saw_valid_empty = False

        for url in trade_and_order_urls:
            for req_body in (payload_with_time, payload_paged):
                try:
                    resp = await self.client.post(url, json=req_body)
                    if resp.status_code != 200:
                        continue
                    body = resp.json()
                    if body.get("success") is False or (
                        body.get("code") and str(body.get("code")) != "000000"
                    ):
                        continue
                    rows = self._extract_list_rows(body.get("data"))
                    if rows is None:
                        continue
                    if not rows:
                        saw_valid_empty = True
                        break
                    fills = self._parse_fill_rows(rows)
                    if fills:
                        fills.sort(key=lambda x: x.order_time_ms)
                        return aggregate_burst_fills(fills) if merge_bursts else fills
                    saw_valid_empty = True
                    break
                except Exception as exc:
                    logger.debug("fetch_recent_orders error (%s): %s", url, exc)

        # Fallback: check position-history (emits opening + closing records from historical positions)
        for base in (COPY_TRADE_FRIENDLY, COPY_TRADE_PUBLIC):
            url = f"{base}/lead-portfolio/position-history"
            try:
                resp = await self.client.post(url, json=payload_paged)
                if resp.status_code != 200:
                    continue
                body = resp.json()
                if body.get("success") is False or (
                    body.get("code") and str(body.get("code")) != "000000"
                ):
                    continue
                rows = self._extract_list_rows(body.get("data"))
                if rows is None:
                    continue
                if not rows:
                    saw_valid_empty = True
                    continue
                fills: List[TraderOrderFill] = []
                for r in rows:
                    symbol = str(r.get("symbol") or "").strip().upper()
                    if not symbol:
                        continue
                    p_side = str(r.get("side") or r.get("positionSide") or "LONG").upper()
                    if p_side not in ("LONG", "SHORT"):
                        p_side = "LONG"
                    opened_ts = _safe_int(r.get("opened") or r.get("openTime"), 0)
                    closed_ts = _safe_int(r.get("closed") or r.get("closedTime") or r.get("updateTime"), 0)
                    qty = abs(
                        _safe_float_val(
                            r.get("closedVolume") or r.get("maxOpenInterest") or r.get("volume"),
                            0.0,
                        )
                    )
                    open_p = _safe_float_val(r.get("avgCost") or r.get("entryPrice"), 0.0)
                    close_p = _safe_float_val(r.get("avgClosePrice"), 0.0)
                    row_id = str(r.get("id") or f"{symbol}-{p_side}-{opened_ts}")

                    if opened_ts > 0 and open_p > 0 and qty > 0:
                        open_order_side = "BUY" if p_side == "LONG" else "SELL"
                        fills.append(
                            TraderOrderFill(
                                order_id=f"open-{row_id}",
                                symbol=symbol,
                                side=open_order_side,
                                position_side=p_side,
                                order_type="LIMIT",
                                avg_price=open_p,
                                executed_qty=qty,
                                realized_pnl=0.0,
                                order_time_ms=opened_ts,
                            )
                        )
                    if closed_ts > 0 and close_p > 0 and qty > 0:
                        close_order_side = "SELL" if p_side == "LONG" else "BUY"
                        fills.append(
                            TraderOrderFill(
                                order_id=f"close-{row_id}",
                                symbol=symbol,
                                side=close_order_side,
                                position_side=p_side,
                                order_type="MARKET",
                                avg_price=close_p,
                                executed_qty=qty,
                                realized_pnl=_safe_float_val(r.get("closingPnl"), 0.0),
                                order_time_ms=closed_ts,
                            )
                        )
                if fills:
                    fills.sort(key=lambda x: x.order_time_ms)
                    return aggregate_burst_fills(fills) if merge_bursts else fills
                saw_valid_empty = True
            except Exception as exc:
                logger.debug("position-history fallback error (%s): %s", url, exc)

        return [] if saw_valid_empty else None

    async def fetch_positions(self, portfolio_id: str) -> Optional[List[TraderPosition]]:
        """Optional helper for legacy encryptedUid leaderboard accounts that have no order-history."""
        if not portfolio_id.startswith("uid:"):
            return None
        uid = portfolio_id.split(":", 1)[1]
        for base in (LEADERBOARD_PUBLIC_V2, LEADERBOARD_PUBLIC_V1):
            try:
                resp = await self.client.post(
                    f"{base}/getOtherPosition",
                    json={"encryptedUid": uid, "tradeType": "PERPETUAL"},
                )
                if resp.status_code != 200:
                    continue
                body = resp.json()
                data = body.get("data") or {}
                rows = data.get("otherPositionRetList")
                if isinstance(rows, list):
                    parsed: List[TraderPosition] = []
                    for item in rows:
                        sym = str(item.get("symbol") or "").upper()
                        amt = _safe_float_val(item.get("amount"), 0.0)
                        if not sym or abs(amt) <= 1e-9:
                            continue
                        parsed.append(
                            TraderPosition(
                                symbol=sym,
                                position_side="LONG" if amt > 0 else "SHORT",
                                position_amt=abs(amt),
                                entry_price=_safe_float_val(item.get("entryPrice"), 0.0),
                                mark_price=_safe_float_val(item.get("markPrice"), 0.0),
                                unrealized_pnl=_safe_float_val(item.get("pnl"), 0.0),
                                leverage=max(1, _safe_int(item.get("leverage"), 10)),
                            )
                        )
                    return parsed
            except Exception:
                pass
        return None

    def seed_watermarks(self, persisted_watermarks: Dict[str, int]) -> None:
        """Seed persisted last_order_time_ms so trades occurring during a brief container nap are still caught!"""
        for pid, ts in persisted_watermarks.items():
            if ts > 0:
                self._initialized[pid] = True
                self._last_order_time_ms[pid] = int(ts)
                self._seen_order_ids.setdefault(pid, set())

    def remove_portfolio_state(self, portfolio_id: str) -> None:
        self._initialized.pop(portfolio_id, None)
        self._seen_order_ids.pop(portfolio_id, None)
        self._last_order_time_ms.pop(portfolio_id, None)
        self._positions_cache.pop(portfolio_id, None)

    async def poll_portfolio(
        self, portfolio_id: str, trader_name: str
    ) -> Tuple[bool, List[PositionEvent], Optional[List[TraderOrderFill]]]:
        """
        Poll a single trader's latest executed trade / order records!
        Returns: (was_initial_baseline, emitted_events, recent_fills)
        """
        # Legacy leaderboard UID fallback
        if portfolio_id.startswith("uid:"):
            positions = await self.fetch_positions(portfolio_id)
            is_first_uid = not self._initialized.get(portfolio_id, False)
            if is_first_uid:
                if positions is None:
                    return (True, [], None)
                self._initialized[portfolio_id] = True
                self._positions_cache[portfolio_id] = {p.key: p for p in positions}
                return (True, [], [])
            if positions is None:
                return (False, [], None)
            prev_map = self._positions_cache.get(portfolio_id, {})
            curr_map = {p.key: p for p in positions}
            evs = diff_positions(portfolio_id, trader_name, prev_map, curr_map)
            self._positions_cache[portfolio_id] = curr_map
            return (False, evs, [])

        raw_fills = await self.fetch_recent_orders(portfolio_id, limit=50, merge_bursts=False)
        is_first = not self._initialized.get(portfolio_id, False)
        seen_ids = self._seen_order_ids.setdefault(portfolio_id, set())

        if is_first:
            if raw_fills is None:
                return (True, [], None)
            self._initialized[portfolio_id] = True
            baseline_ts = int(time.time() * 1000)
            for f in raw_fills:
                seen_ids.add(f.order_id)
                baseline_ts = max(baseline_ts, f.order_time_ms)
            self._last_order_time_ms[portfolio_id] = baseline_ts
            return (True, [], aggregate_burst_fills(raw_fills))

        if raw_fills is None:
            return (False, [], None)

        new_raw: List[TraderOrderFill] = []
        saved_wm = self._last_order_time_ms.get(portfolio_id, 0)
        # If seen_ids is empty right after a container restart with a persisted watermark,
        # strictly require order_time_ms > saved_wm so historical fills don't re-fire!
        cutoff = (saved_wm + 1) if not seen_ids else (saved_wm - 300_000)
        for f in raw_fills:
            if f.order_id not in seen_ids:
                seen_ids.add(f.order_id)
                if f.order_time_ms >= cutoff:
                    if f.order_time_ms > self._last_order_time_ms.get(portfolio_id, 0):
                        self._last_order_time_ms[portfolio_id] = f.order_time_ms
                    new_raw.append(f)

        events = [
            fill_to_event(portfolio_id, trader_name, mf)
            for mf in aggregate_burst_fills(new_raw)
        ]

        if len(seen_ids) > 1000:
            # Retain recent order IDs so memory never grows unbounded
            recent_keep = {f.order_id for f in raw_fills}
            self._seen_order_ids[portfolio_id] = recent_keep

        return (False, events, aggregate_burst_fills(raw_fills))
