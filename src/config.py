from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()


def extract_portfolio_id(raw: str) -> Optional[str]:
    """Extract numeric portfolioId (or legacy encryptedUid) from a URL or raw ID string."""
    text = raw.strip()
    if not text:
        return None

    # Match ?portfolioId=123456..., ?leadPortfolioId=..., /lead-details/123456..., /portfolio/123456...
    m = re.search(
        r"(?:portfolioId=|leadPortfolioId=|/lead-details/|/portfolio/)(\d{8,30})",
        text,
    )
    if m:
        return m.group(1)

    # Pure numeric portfolioId
    if re.fullmatch(r"\d{8,30}", text):
        return text

    # Legacy Binance Leaderboard encryptedUid (hex/alphanumeric)
    m_uid = re.search(r"encryptedUid=([A-Za-z0-9]{16,64})", text)
    if m_uid:
        return f"uid:{m_uid.group(1)}"

    if re.fullmatch(r"[A-Fa-f0-9]{32}", text):
        return f"uid:{text}"

    return None


@dataclass
class AppConfig:
    telegram_bot_token: str
    telegram_chat_id: str
    admin_user_id: str = ""
    webhook_base_url: str = ""
    poll_interval_sec: float = 6.0
    binance_web_cookie: str = ""

    enable_auto_trade: bool = False
    binance_use_testnet: bool = False
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_ed25519_private_key: str = ""

    trade_margin_usdt: float = 20.0
    max_margin_usdt: float = 60.0
    trade_leverage: int = 10
    limit_order_mode: str = "TARGET_PRICE"  # TARGET_PRICE or POST_ONLY_BBO
    limit_slippage_pct: float = 0.08
    limit_order_timeout_sec: int = 180
    exit_order_type: str = "MARKET"  # MARKET or LIMIT

    state_file: Path = field(default_factory=lambda: Path("data/state.json"))
    tracked_portfolios: Dict[str, str] = field(default_factory=dict)  # id -> nickname
    saved_pending_orders: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_env(cls, state_path: Optional[Path] = None) -> "AppConfig":
        raw_portfolios = os.getenv("TRACKED_PORTFOLIOS", "")
        initial_map: Dict[str, str] = {}
        for item in raw_portfolios.split(","):
            pid = extract_portfolio_id(item)
            if pid:
                initial_map[pid] = pid

        ed_key = os.getenv("BINANCE_ED25519_PRIVATE_KEY", "").strip()
        if "\\n" in ed_key:
            ed_key = ed_key.replace("\\n", "\n")

        trade_margin = max(5.0, float(os.getenv("TRADE_MARGIN_USDT", "20")))
        max_margin_raw = os.getenv("MAX_MARGIN_USDT", "").strip()
        max_margin = (
            max(trade_margin, float(max_margin_raw))
            if max_margin_raw
            else max(trade_margin * 3.0, 50.0)
        )

        webhook_url = (
            os.getenv("WEBHOOK_BASE_URL", "").strip()
            or os.getenv("RENDER_EXTERNAL_URL", "").strip()
        ).rstrip("/")

        cfg = cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            admin_user_id=os.getenv("TELEGRAM_ADMIN_USER_ID", "").strip(),
            webhook_base_url=webhook_url,
            poll_interval_sec=max(3.0, float(os.getenv("POLL_INTERVAL_SEC", "6"))),
            binance_web_cookie=os.getenv("BINANCE_WEB_COOKIE", "").strip(),
            enable_auto_trade=os.getenv("ENABLE_AUTO_TRADE", "false").lower()
            in ("1", "true", "yes", "on"),
            binance_use_testnet=os.getenv("BINANCE_USE_TESTNET", "false").lower()
            in ("1", "true", "yes", "on"),
            binance_api_key=os.getenv("BINANCE_API_KEY", "").strip(),
            binance_api_secret=os.getenv("BINANCE_API_SECRET", "").strip(),
            binance_ed25519_private_key=ed_key,
            trade_margin_usdt=trade_margin,
            max_margin_usdt=max_margin,
            trade_leverage=max(1, min(125, int(os.getenv("TRADE_LEVERAGE", "10")))),
            limit_order_mode=os.getenv("LIMIT_ORDER_MODE", "TARGET_PRICE").strip().upper(),
            limit_slippage_pct=max(0.0, float(os.getenv("LIMIT_SLIPPAGE_PCT", "0.08"))),
            limit_order_timeout_sec=max(15, int(os.getenv("LIMIT_ORDER_TIMEOUT_SEC", "180"))),
            exit_order_type=os.getenv("EXIT_ORDER_TYPE", "MARKET").strip().upper(),
            state_file=state_path or Path(os.getenv("STATE_FILE_PATH", "data/state.json")),
            tracked_portfolios=initial_map,
        )
        cfg.load_state()
        return cfg

    def load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return

            if not self.telegram_chat_id and data.get("telegram_chat_id"):
                self.telegram_chat_id = str(data["telegram_chat_id"]).strip()
            if not self.admin_user_id and data.get("admin_user_id"):
                self.admin_user_id = str(data["admin_user_id"]).strip()

            saved_portfolios = data.get("tracked_portfolios", {})
            if isinstance(saved_portfolios, dict):
                for pid, name in saved_portfolios.items():
                    self.tracked_portfolios[str(pid)] = str(name)

            if isinstance(data.get("enable_auto_trade"), bool):
                self.enable_auto_trade = data["enable_auto_trade"]

            if isinstance(data.get("trade_margin_usdt"), (int, float)):
                self.trade_margin_usdt = max(5.0, float(data["trade_margin_usdt"]))
            if isinstance(data.get("max_margin_usdt"), (int, float)):
                self.max_margin_usdt = max(self.trade_margin_usdt, float(data["max_margin_usdt"]))
            if isinstance(data.get("trade_leverage"), (int, float)):
                self.trade_leverage = max(1, min(125, int(data["trade_leverage"])))
            if data.get("limit_order_mode") in ("TARGET_PRICE", "POST_ONLY_BBO"):
                self.limit_order_mode = str(data["limit_order_mode"])
            if isinstance(data.get("limit_slippage_pct"), (int, float)):
                self.limit_slippage_pct = max(0.0, float(data["limit_slippage_pct"]))
            if isinstance(data.get("limit_order_timeout_sec"), (int, float)):
                self.limit_order_timeout_sec = max(15, int(data["limit_order_timeout_sec"]))
            if data.get("exit_order_type") in ("MARKET", "LIMIT"):
                self.exit_order_type = str(data["exit_order_type"])

            pending_raw = data.get("pending_orders", [])
            if isinstance(pending_raw, list):
                self.saved_pending_orders = [row for row in pending_raw if isinstance(row, dict)]
        except Exception:
            pass

    def save_state(self, pending_orders: Optional[List[Dict[str, Any]]] = None) -> None:
        if pending_orders is not None:
            self.saved_pending_orders = pending_orders
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "telegram_chat_id": self.telegram_chat_id,
                "admin_user_id": self.admin_user_id,
                "tracked_portfolios": self.tracked_portfolios,
                "enable_auto_trade": self.enable_auto_trade,
                "trade_margin_usdt": self.trade_margin_usdt,
                "max_margin_usdt": self.max_margin_usdt,
                "trade_leverage": self.trade_leverage,
                "limit_order_mode": self.limit_order_mode,
                "limit_slippage_pct": self.limit_slippage_pct,
                "limit_order_timeout_sec": self.limit_order_timeout_sec,
                "exit_order_type": self.exit_order_type,
                "pending_orders": self.saved_pending_orders,
            }
            tmp_file = self.state_file.with_suffix(".json.tmp")
            tmp_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_file.replace(self.state_file)
        except Exception:
            pass

    def has_trading_credentials(self) -> bool:
        return bool(
            self.binance_api_key
            and (self.binance_api_secret or self.binance_ed25519_private_key)
        )

    def list_portfolio_ids(self) -> List[str]:
        return list(self.tracked_portfolios.keys())
