from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

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
    ref_price: float  # Reference price for display & limit order calculation
    entry_price: float
    mark_price: float
    leverage: int
    unrealized_pnl: float = 0.0
    realized_pnl: Optional[float] = None
    event_time_ms: int = 0


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


def diff_positions(
    portfolio_id: str,
    trader_name: str,
    prev_map: Dict[Tuple[str, str], TraderPosition],
    curr_map: Dict[Tuple[str, str], TraderPosition],
    recent_fills: Optional[List[TraderOrderFill]] = None,
    eps: float = 1e-9,
) -> List[PositionEvent]:
    """Compare previous and current open positions to emit precise trade events."""
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
    """Monitors Binance Copy-Trading Lead Portfolios (and legacy Leaderboard UIDs)."""

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
        self._positions_cache: Dict[str, Dict[Tuple[str, str], TraderPosition]] = {}
        self._seen_order_ids: Dict[str, set[str]] = {}
        self._last_order_time_ms: Dict[str, int] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @staticmethod
    def build_trader_url(portfolio_id: str) -> str:
        if portfolio_id.startswith("uid:"):
            uid = portfolio_id.split(":", 1)[1]
            return f"{BINANCE_WEB_BASE}/zh-TC/futures-activity/leaderboard/user/um?encryptedUid={uid}"
        return f"{BINANCE_WEB_BASE}/zh-TC/copy-trading/lead-details/{portfolio_id}"

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
    def _parse_position_rows(raw_rows: List[Dict[str, Any]]) -> List[TraderPosition]:
        parsed: List[TraderPosition] = []
        for item in raw_rows:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            raw_amt_field = (
                item.get("positionAmount")
                if item.get("positionAmount") is not None
                else item.get("positionAmt")
                if item.get("positionAmt") is not None
                else item.get("amount")
            )
            raw_amt = _safe_float_val(raw_amt_field, 0.0)
            if abs(raw_amt) <= 1e-9:
                continue

            raw_side = str(
                item.get("positionSide") or item.get("side") or ""
            ).strip().upper()
            if raw_side in ("LONG", "BUY"):
                p_side = "LONG" if raw_amt >= 0 else "SHORT"
                qty = abs(raw_amt)
            elif raw_side in ("SHORT", "SELL"):
                p_side = "SHORT"
                qty = abs(raw_amt)
            else:
                p_side = "LONG" if raw_amt > 0 else "SHORT"
                qty = abs(raw_amt)

            entry_price = _safe_float_val(
                item.get("entryPrice")
                if item.get("entryPrice") is not None
                else item.get("openPrice"),
                0.0,
            )
            mark_price = _safe_float_val(item.get("markPrice"), entry_price)
            upnl_field = (
                item.get("unrealizedProfit")
                if item.get("unrealizedProfit") is not None
                else item.get("unRealizedProfit")
                if item.get("unRealizedProfit") is not None
                else item.get("pnl")
            )
            upnl = _safe_float_val(upnl_field, 0.0)
            leverage = max(1, _safe_int(item.get("leverage"), 10))
            isolated_flag = item.get("isolated")
            margin_type = (
                "isolated"
                if isolated_flag is True
                or str(item.get("marginType", "")).lower() == "isolated"
                else "cross"
            )

            parsed.append(
                TraderPosition(
                    symbol=symbol,
                    position_side=p_side,
                    position_amt=qty,
                    entry_price=entry_price,
                    mark_price=mark_price,
                    unrealized_pnl=upnl,
                    leverage=leverage,
                    margin_type=margin_type,
                )
            )
        return parsed

    async def fetch_positions(self, portfolio_id: str) -> Optional[List[TraderPosition]]:
        """Fetch active open positions across all Binance Copy-Trading & Leaderboard routes."""
        if portfolio_id.startswith("uid:"):
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
                    if (
                        str(body.get("code", "000000")) != "000000"
                        and body.get("success") is False
                    ):
                        continue
                    data = body.get("data") or {}
                    rows = data.get("otherPositionRetList")
                    if isinstance(rows, list):
                        return self._parse_position_rows(rows)
                except Exception as exc:
                    logger.debug("Leaderboard getOtherPosition failed: %s", exc)
            return None

        # Try both primary `lead-data/positions` and `lead-portfolio/positions` endpoints
        candidate_urls = [
            f"{COPY_TRADE_FRIENDLY}/lead-data/positions",
            f"{COPY_TRADE_PUBLIC}/lead-data/positions",
            f"{COPY_TRADE_FRIENDLY}/lead-portfolio/positions",
            f"{COPY_TRADE_PUBLIC}/lead-portfolio/positions",
        ]
        for url in candidate_urls:
            try:
                resp = await self.client.get(url, params={"portfolioId": portfolio_id})
                if resp.status_code in (404, 405):
                    resp = await self.client.post(url, json={"portfolioId": portfolio_id})
                if resp.status_code != 200:
                    continue
                body = resp.json()
                if body.get("success") is False or (
                    body.get("code") and str(body.get("code")) != "000000"
                ):
                    continue
                data = body.get("data")
                if isinstance(data, list):
                    return self._parse_position_rows(data)
                if isinstance(data, dict):
                    rows = (
                        data.get("positions")
                        if isinstance(data.get("positions"), list)
                        else data.get("list")
                        if isinstance(data.get("list"), list)
                        else data.get("otherPositionRetList")
                    )
                    if isinstance(rows, list):
                        return self._parse_position_rows(rows)
                if data is None and str(body.get("code", "")) == "000000":
                    return []
            except Exception as exc:
                logger.debug("fetch_positions error (%s): %s", url, exc)
        return None

    async def fetch_recent_orders(
        self, portfolio_id: str, limit: int = 15
    ) -> List[TraderOrderFill]:
        """Fetch recent executed order fills (with position-history fallback) for a Copy-Trading portfolio."""
        if portfolio_id.startswith("uid:"):
            return []

        now_ms = int(time.time() * 1000)
        start_ms = now_ms - 24 * 3600 * 1000
        payload = {
            "portfolioId": portfolio_id,
            "startTime": start_ms,
            "endTime": now_ms,
            "pageSize": limit,
        }
        for base in (COPY_TRADE_FRIENDLY, COPY_TRADE_PUBLIC):
            url = f"{base}/lead-portfolio/order-history"
            try:
                resp = await self.client.post(url, json=payload)
                if resp.status_code != 200:
                    continue
                body = resp.json()
                data = body.get("data")
                rows: List[Dict[str, Any]] = []
                if isinstance(data, dict):
                    rows = (
                        data.get("list")
                        or data.get("owsOrderList")
                        or data.get("orders")
                        or []
                    )
                elif isinstance(data, list):
                    rows = data

                if not isinstance(rows, list):
                    continue

                fills: List[TraderOrderFill] = []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    qty = _safe_float_val(r.get("executedQty") or r.get("qty"), 0.0)
                    if qty <= 0:
                        continue
                    avg_p = _safe_float_val(r.get("avgPrice") or r.get("price"), 0.0)
                    ts = _safe_int(
                        r.get("orderTime") or r.get("orderUpdateTime") or r.get("time"), 0
                    )
                    oid = str(
                        r.get("orderId")
                        or r.get("id")
                        or f"{r.get('symbol')}-{r.get('side')}-{ts}-{qty}"
                    )
                    side = str(r.get("side") or "").upper()
                    pos_side = str(r.get("positionSide") or "BOTH").upper()
                    fills.append(
                        TraderOrderFill(
                            order_id=oid,
                            symbol=str(r.get("symbol") or "").upper(),
                            side=side,
                            position_side=pos_side,
                            order_type=str(r.get("type") or "MARKET").upper(),
                            avg_price=avg_p,
                            executed_qty=qty,
                            realized_pnl=_safe_float_val(
                                r.get("totalPnl")
                                if r.get("totalPnl") is not None
                                else r.get("realizedPnl"),
                                0.0,
                            ),
                            order_time_ms=ts,
                        )
                    )
                if fills:
                    fills.sort(key=lambda x: x.order_time_ms)
                    return fills
            except Exception as exc:
                logger.debug("fetch_recent_orders error (%s): %s", url, exc)

        # Fallback: check closed position-history if order-history was empty or hidden
        pos_hist_payload = {
            "portfolioId": portfolio_id,
            "pageNumber": 1,
            "pageSize": min(10, limit),
        }
        for base in (COPY_TRADE_FRIENDLY, COPY_TRADE_PUBLIC):
            url = f"{base}/lead-portfolio/position-history"
            try:
                resp = await self.client.post(url, json=pos_hist_payload)
                if resp.status_code != 200:
                    continue
                body = resp.json()
                data = body.get("data")
                rows = data.get("list") if isinstance(data, dict) else data
                if not isinstance(rows, list) or not rows:
                    continue
                fills = []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    closed_ts = _safe_int(r.get("closedTime") or r.get("updateTime"), 0)
                    if closed_ts <= 0:
                        continue
                    qty = _safe_float_val(
                        r.get("closedVolume") or r.get("maxOpenInterest"), 0.0
                    )
                    close_p = _safe_float_val(r.get("avgClosePrice"), 0.0)
                    if qty <= 0 or close_p <= 0:
                        continue
                    p_side = str(r.get("side") or r.get("positionSide") or "LONG").upper()
                    if p_side not in ("LONG", "SHORT"):
                        p_side = "LONG"
                    close_order_side = "SELL" if p_side == "LONG" else "BUY"
                    oid = str(r.get("id") or f"poshist-{r.get('symbol')}-{p_side}-{closed_ts}")
                    fills.append(
                        TraderOrderFill(
                            order_id=oid,
                            symbol=str(r.get("symbol") or "").upper(),
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
                    return fills
            except Exception as exc:
                logger.debug("position-history fallback error (%s): %s", url, exc)

        return []

    def remove_portfolio_state(self, portfolio_id: str) -> None:
        self._initialized.pop(portfolio_id, None)
        self._positions_cache.pop(portfolio_id, None)
        self._seen_order_ids.pop(portfolio_id, None)
        self._last_order_time_ms.pop(portfolio_id, None)

    async def poll_portfolio(
        self, portfolio_id: str, trader_name: str
    ) -> Tuple[bool, List[PositionEvent], Optional[List[TraderPosition]]]:
        """
        Poll a single trader once.
        Returns: (was_initial_baseline, emitted_events, current_positions)
        """
        positions = await self.fetch_positions(portfolio_id)
        fills = await self.fetch_recent_orders(portfolio_id)

        is_first = not self._initialized.get(portfolio_id, False)
        seen_ids = self._seen_order_ids.setdefault(portfolio_id, set())

        if is_first:
            if positions is None and not fills:
                return (True, [], None)
            self._initialized[portfolio_id] = True
            if positions is not None:
                self._positions_cache[portfolio_id] = {p.key: p for p in positions}
            baseline_ts = int(time.time() * 1000)
            for f in fills:
                seen_ids.add(f.order_id)
                baseline_ts = max(baseline_ts, f.order_time_ms)
            self._last_order_time_ms[portfolio_id] = baseline_ts
            return (True, [], positions if positions is not None else [])

        new_fills: List[TraderOrderFill] = []
        baseline_cutoff = self._last_order_time_ms.get(portfolio_id, 0) - 60_000
        for f in fills:
            if f.order_id not in seen_ids and f.order_time_ms >= baseline_cutoff:
                seen_ids.add(f.order_id)
                new_fills.append(f)
                if f.order_time_ms > self._last_order_time_ms.get(portfolio_id, 0):
                    self._last_order_time_ms[portfolio_id] = f.order_time_ms

        if len(seen_ids) > 500:
            recent_keep = {f.order_id for f in fills}
            self._seen_order_ids[portfolio_id] = recent_keep

        events: List[PositionEvent] = []
        handled_symbols_sides: set[Tuple[str, str]] = set()

        if positions is not None:
            prev_map = self._positions_cache.get(portfolio_id, {})
            curr_map = {p.key: p for p in positions}
            events = diff_positions(portfolio_id, trader_name, prev_map, curr_map, new_fills)
            self._positions_cache[portfolio_id] = curr_map
            for ev in events:
                handled_symbols_sides.add((ev.symbol, ev.position_side))

        # Fallback / Fast-scalp detection: if trader hid open positions OR opened & closed between polls
        for f in new_fills:
            inferred_pside = f.position_side
            if inferred_pside not in ("LONG", "SHORT"):
                if abs(f.realized_pnl) > 1e-9:
                    inferred_pside = "LONG" if f.side == "SELL" else "SHORT"
                else:
                    inferred_pside = "LONG" if f.side == "BUY" else "SHORT"

            if (f.symbol, inferred_pside) in handled_symbols_sides:
                continue

            is_opening = (f.side == "BUY" and inferred_pside == "LONG") or (
                f.side == "SELL" and inferred_pside == "SHORT"
            )
            action = "OPEN" if is_opening else "CLOSE"
            events.append(
                PositionEvent(
                    portfolio_id=portfolio_id,
                    trader_name=trader_name,
                    action=action,
                    symbol=f.symbol,
                    position_side=inferred_pside,
                    prev_qty=0.0 if is_opening else f.executed_qty,
                    curr_qty=f.executed_qty if is_opening else 0.0,
                    delta_qty=f.executed_qty,
                    ref_price=f.avg_price,
                    entry_price=f.avg_price,
                    mark_price=f.avg_price,
                    leverage=10,
                    realized_pnl=f.realized_pnl if not is_opening else None,
                    event_time_ms=f.order_time_ms,
                )
            )

        return (False, events, positions)
