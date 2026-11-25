from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

CLOUD_STATE_ENV_KEY = "BOT_PERSISTED_STATE"


def extract_portfolio_id(raw: str) -> Optional[str]:
    """Extract numeric portfolioId (or legacy encryptedUid) from a URL, decoded redirect, or raw ID string."""
    text = urllib.parse.unquote(raw.strip())
    if not text:
        return None

    # Match ?portfolioId=..., ?leadPortfolioId=..., /lead-details/..., /portfolio/..., or JSON "portfolioId":"..."
    m = re.search(
        r'(?:portfolioId["\s:=]+|leadPortfolioId["\s:=]+|/lead-details/|/portfolio/)(\d{8,30})',
        text,
    )
    if m:
        return m.group(1)

    # Pure numeric portfolioId
    if re.fullmatch(r"\d{8,30}", text):
        return text

    # Legacy Binance Leaderboard encryptedUid
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
    render_api_key: str = ""
    render_service_id: str = ""
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
    last_order_time_ms: Dict[str, int] = field(default_factory=dict)  # id -> watermark ms
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
            render_api_key=os.getenv("RENDER_API_KEY", "").strip(),
            render_service_id=os.getenv("RENDER_SERVICE_ID", "").strip(),
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

    def _apply_state_dict(self, data: Dict[str, Any]) -> None:
        if not self.telegram_chat_id and data.get("telegram_chat_id"):
            self.telegram_chat_id = str(data["telegram_chat_id"]).strip()
        if not self.admin_user_id and data.get("admin_user_id"):
            self.admin_user_id = str(data["admin_user_id"]).strip()

        if "tracked_portfolios" in data and isinstance(data["tracked_portfolios"], dict):
            # Replace with authoritative persisted map so both /add AND /remove persist across restarts!
            self.tracked_portfolios = {
                str(pid): str(name) for pid, name in data["tracked_portfolios"].items()
            }

        if isinstance(data.get("last_order_time_ms"), dict):
            for pid, ts in data["last_order_time_ms"].items():
                try:
                    self.last_order_time_ms[str(pid)] = int(ts)
                except (TypeError, ValueError):
                    pass

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

    def _fetch_cloud_state_json(self) -> Optional[Dict[str, Any]]:
        if self.render_api_key and self.render_service_id:
            url = (
                f"https://api.render.com/v1/services/{self.render_service_id}"
                f"/env-vars/{CLOUD_STATE_ENV_KEY}"
            )
            try:
                resp = httpx.get(
                    url,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self.render_api_key}",
                    },
                    timeout=8.0,
                )
                if resp.status_code == 200:
                    val_str = (resp.json() or {}).get("value", "")
                    if val_str:
                        parsed = json.loads(val_str)
                        if isinstance(parsed, dict):
                            return parsed
            except Exception as exc:
                logger.warning("讀取 Render 雲端永久狀態略過: %s", exc)

        env_fallback = os.getenv(CLOUD_STATE_ENV_KEY, "").strip()
        if env_fallback:
            try:
                parsed = json.loads(env_fallback)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        return None

    def _push_cloud_state_json(self, payload: Dict[str, Any]) -> None:
        if not (self.render_api_key and self.render_service_id):
            return
        url = (
            f"https://api.render.com/v1/services/{self.render_service_id}"
            f"/env-vars/{CLOUD_STATE_ENV_KEY}"
        )
        try:
            compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            resp = httpx.put(
                url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.render_api_key}",
                },
                json={"value": compact},
                timeout=8.0,
            )
            if resp.status_code >= 400:
                logger.warning("同步 Render 雲端永久狀態失敗 (%s): %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.warning("同步 Render 雲端永久狀態異常: %s", exc)

    def load_state(self) -> None:
        # 1. Try local state.json first (if present)
        if self.state_file.exists():
            try:
                local_data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(local_data, dict):
                    self._apply_state_dict(local_data)
            except Exception:
                pass

        # 2. Authoritative cloud state from Render API overrides ephemeral container resets!
        cloud_data = self._fetch_cloud_state_json()
        if cloud_data:
            self._apply_state_dict(cloud_data)

    def build_state_payload(self) -> Dict[str, Any]:
        return {
            "telegram_chat_id": self.telegram_chat_id,
            "admin_user_id": self.admin_user_id,
            "tracked_portfolios": self.tracked_portfolios,
            "last_order_time_ms": self.last_order_time_ms,
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

    def save_state(self, pending_orders: Optional[List[Dict[str, Any]]] = None) -> None:
        if pending_orders is not None:
            self.saved_pending_orders = pending_orders
        payload = self.build_state_payload()
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.state_file.with_suffix(".json.tmp")
            tmp_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_file.replace(self.state_file)
        except Exception:
            pass

        self._push_cloud_state_json(payload)

    def has_trading_credentials(self) -> bool:
        return bool(
            self.binance_api_key
            and (self.binance_api_secret or self.binance_ed25519_private_key)
        )

    def list_portfolio_ids(self) -> List[str]:
        return list(self.tracked_portfolios.keys())
