"""
Capital.com Demo Mean-Reversion Grid Bot
Python 3.10+ | Railway worker | SQLite persistence

IMPORTANT:
- Demo environment only. This strategy can lose money and grid sizing increases exposure.
- Verify epic, dealing rules, min deal size, currency, and API behavior in your own demo account.
- Capital.com broker positions are authoritative; SQLite is local recovery/audit state.
"""
from __future__ import annotations

import json
import logging
import math
import os
import signal
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

CONFIG = {
    # Set INSTRUMENT_EPIC to pin a verified market. Otherwise the bot discovers it
    # from Capital.com's Demo market-search endpoint using the name below.
    "instrument_epic": os.getenv("INSTRUMENT_EPIC", "").strip(),
    "instrument_search_term": os.getenv("INSTRUMENT_SEARCH_TERM", "Gold").strip(),
    "instrument_name": os.getenv("INSTRUMENT_NAME", "Gold Spot").strip(),
    "base_lot_size": 0.1,
    "lot_multiplier": 1.3,
    "max_grid_levels": 3,                 # additional grid trades; 1 initial + 3 grid = 4 max
    "grid_step_percent": 0.003,
    "tp_target": 8.00,
    "sl_target": -8.00,
    "profit_lock_activation": 3.00,
    "profit_lock_trail_drop": 1.00,
    "pause_duration_seconds": 3600,
    "risk_per_trade_percent": 1.0,        # informational guardrail; broker contract sizing still needs validation
    "poll_seconds": 15,
    "price_resolution": "MINUTE",
    "price_bars": 50,
    "sma_period": 20,
    "sd_multiplier": 2.0,
    "request_timeout_seconds": 20,
    "max_api_retries": 2,
}

DB_PATH = os.getenv("SQLITE_PATH", "bot_state.sqlite3")
BASE_URL = os.getenv("CAPITAL_BASE_URL", "https://demo-api-capital.backend-capital.com/").rstrip("/") + "/"
API_KEY = os.getenv("CAPITAL_API_KEY", "")
IDENTIFIER = os.getenv("CAPITAL_IDENTIFIER", "")
PASSWORD = os.getenv("CAPITAL_PASSWORD", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("capitalcom-bot")
STOP_REQUESTED = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def notify(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message[:3900]},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:
        log.warning("Telegram notification failed: %s", exc)


class CapitalAPIError(RuntimeError):
    pass


class CapitalClient:
    """Small native REST client with CST/X-SECURITY-TOKEN session handling."""

    def __init__(self) -> None:
        if not (API_KEY and IDENTIFIER and PASSWORD):
            raise RuntimeError("Set CAPITAL_API_KEY, CAPITAL_IDENTIFIER, and CAPITAL_PASSWORD.")
        if "demo-api-capital.backend-capital.com" not in BASE_URL.lower():
            raise RuntimeError("Safety stop: this bot is configured for the Capital.com DEMO endpoint only.")
        self.session = requests.Session()
        self.cst: Optional[str] = None
        self.security_token: Optional[str] = None
        self.last_authenticated_at = 0.0
        self.last_request_at = 0.0

    def authenticate(self) -> None:
        url = BASE_URL + "api/v1/session"
        headers = {"X-CAP-API-KEY": API_KEY, "Content-Type": "application/json"}
        try:
            response = self.session.post(
                url,
                headers=headers,
                json={"identifier": IDENTIFIER, "password": PASSWORD},
                timeout=CONFIG["request_timeout_seconds"],
            )
        except requests.RequestException as exc:
            raise CapitalAPIError(f"Authentication network error: {exc}") from exc
        if response.status_code >= 400:
            raise CapitalAPIError(f"Capital.com authentication failed ({response.status_code}): {response.text[:500]}")
        self.cst = response.headers.get("CST")
        self.security_token = response.headers.get("X-SECURITY-TOKEN")
        if not self.cst or not self.security_token:
            raise CapitalAPIError("Authentication response did not contain CST and X-SECURITY-TOKEN headers.")
        self.last_authenticated_at = time.monotonic()
        self.last_request_at = self.last_authenticated_at
        log.info("Capital.com demo session authenticated.")

    def _ensure_session(self) -> None:
        # API sessions can expire after inactivity. Refresh proactively at 8 minutes.
        if (
            not self.cst
            or not self.security_token
            or time.monotonic() - self.last_request_at >= 8 * 60
        ):
            self.authenticate()

    def request(self, method: str, path: str, *, params=None, payload=None, allow_retry=True) -> Any:
        self._ensure_session()
        url = BASE_URL + path.lstrip("/")
        headers = {
            "X-CAP-API-KEY": API_KEY,
            "CST": self.cst or "",
            "X-SECURITY-TOKEN": self.security_token or "",
            "Content-Type": "application/json",
        }
        try:
            response = self.session.request(
                method.upper(), url, headers=headers, params=params, json=payload,
                timeout=CONFIG["request_timeout_seconds"],
            )
            self.last_request_at = time.monotonic()
        except requests.RequestException as exc:
            raise CapitalAPIError(f"{method} {path} network error: {exc}") from exc

        if response.status_code in (401, 403) and allow_retry:
            log.warning("Capital.com session rejected; re-authenticating and retrying once.")
            self.authenticate()
            return self.request(method, path, params=params, payload=payload, allow_retry=False)

        if response.status_code >= 400:
            raise CapitalAPIError(f"{method} {path} failed ({response.status_code}): {response.text[:700]}")
        if not response.text.strip():
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw_text": response.text}

    def search_markets(self, search_term: str) -> list[dict[str, Any]]:
        """Search the authenticated account's available markets; never invent an epic."""
        data = self.request("GET", "api/v1/markets", params={"searchTerm": search_term})
        return data.get("markets", []) if isinstance(data, dict) else []

    def positions(self) -> list[dict[str, Any]]:
        data = self.request("GET", "api/v1/positions")
        return data.get("positions", []) if isinstance(data, dict) else []

    def prices(self, epic: str) -> list[dict[str, Any]]:
        data = self.request(
            "GET", f"api/v1/prices/{epic}",
            params={"resolution": CONFIG["price_resolution"], "max": CONFIG["price_bars"]},
        )
        return data.get("prices", []) if isinstance(data, dict) else []

    def open_position(self, epic: str, direction: str, size: float) -> dict[str, Any]:
        payload = {"epic": epic, "direction": direction, "size": size}
        # Stops/limits are basket-managed; broker-side per-trade levels are intentionally not guessed.
        return self.request("POST", "api/v1/positions", payload=payload)

    def close_position(self, deal_id: str) -> dict[str, Any]:
        # Capital.com REST close endpoint is DELETE /api/v1/positions/{dealId}.
        return self.request("DELETE", f"api/v1/positions/{deal_id}")


class Store:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self._init_db()

    def _init_db(self) -> None:
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    basket_id TEXT,
                    direction TEXT,
                    anchor_price REAL,
                    max_basket_pl REAL NOT NULL DEFAULT 0,
                    profit_lock_active INTEGER NOT NULL DEFAULT 0,
                    is_paused INTEGER NOT NULL DEFAULT 0,
                    pause_until REAL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO bot_state (id, updated_at) VALUES (1, CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS open_positions (
                    deal_id TEXT PRIMARY KEY,
                    basket_id TEXT NOT NULL,
                    epic TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL,
                    size REAL NOT NULL,
                    level INTEGER NOT NULL,
                    opened_at TEXT NOT NULL,
                    raw_json TEXT
                );
                CREATE TABLE IF NOT EXISTS trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    basket_id TEXT,
                    deal_id TEXT,
                    event TEXT NOT NULL,
                    direction TEXT,
                    size REAL,
                    price REAL,
                    basket_pl REAL,
                    details TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_positions_basket ON open_positions(basket_id);
                CREATE INDEX IF NOT EXISTS idx_history_created ON trade_history(created_at);
            """)

    def state(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM bot_state WHERE id=1").fetchone()
        return dict(row)

    def update_state(self, **fields: Any) -> None:
        allowed = {
            "basket_id", "direction", "anchor_price", "max_basket_pl",
            "profit_lock_active", "is_paused", "pause_until", "last_error",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        updates["updated_at"] = utc_now()
        sql = ", ".join(f"{key}=?" for key in updates)
        with self.conn:
            self.conn.execute(f"UPDATE bot_state SET {sql} WHERE id=1", list(updates.values()))

    def open_positions(self, basket_id: Optional[str] = None) -> list[dict[str, Any]]:
        if basket_id:
            rows = self.conn.execute(
                "SELECT * FROM open_positions WHERE basket_id=? ORDER BY level", (basket_id,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM open_positions ORDER BY opened_at").fetchall()
        return [dict(row) for row in rows]

    def save_position(self, pos: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO open_positions
                (deal_id,basket_id,epic,direction,entry_price,size,level,opened_at,raw_json)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                pos["deal_id"], pos["basket_id"], pos["epic"], pos["direction"],
                pos.get("entry_price"), pos["size"], pos["level"], pos.get("opened_at", utc_now()),
                json.dumps(pos.get("raw", {}), default=str),
            ))

    def remove_position(self, deal_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM open_positions WHERE deal_id=?", (deal_id,))

    def log_event(self, event: str, *, basket_id=None, deal_id=None, direction=None,
                  size=None, price=None, basket_pl=None, details=None) -> None:
        with self.conn:
            self.conn.execute("""
                INSERT INTO trade_history
                (basket_id,deal_id,event,direction,size,price,basket_pl,details,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (basket_id, deal_id, event, direction, size, price, basket_pl,
                  json.dumps(details, default=str) if details is not None else None, utc_now()))

    def close(self) -> None:
        self.conn.close()


def _nested_price(price_obj: Any, side: str) -> Optional[float]:
    if not isinstance(price_obj, dict):
        return None
    value = price_obj.get(side)
    if isinstance(value, dict):
        value = value.get("bid" if side == "bid" else "ask")
    return as_float(value, float("nan")) if value is not None else None


def parse_price_bar(bar: dict[str, Any]) -> Optional[float]:
    close = bar.get("closePrice") or bar.get("close") or {}
    if isinstance(close, dict):
        bid = as_float(close.get("bid"), float("nan"))
        ask = as_float(close.get("ask"), float("nan"))
        if math.isfinite(bid) and math.isfinite(ask):
            return (bid + ask) / 2
        if math.isfinite(bid):
            return bid
        if math.isfinite(ask):
            return ask
    if isinstance(close, (int, float)):
        return float(close)
    return None


def extract_position(p: dict[str, Any]) -> dict[str, Any]:
    market = p.get("market") or {}
    position = p.get("position") or {}
    return {
        "deal_id": position.get("dealId") or p.get("dealId"),
        "epic": market.get("epic") or p.get("epic"),
        "direction": (position.get("方向") or position.get("direction") or "").upper(),
        "size": as_float(position.get("size"), 0.0),
        "entry_price": as_float(position.get("level"), 0.0),
        "profit": as_float(position.get("profit"), 0.0),
        "raw": p,
    }


@dataclass
class MarketSnapshot:
    price: float
    bid: float
    ask: float
    sma: float
    sd: float


class TradingBot:
    def __init__(self) -> None:
        self.api = CapitalClient()
        self.store = Store(DB_PATH)
        self.epic = CONFIG["instrument_epic"]
        if self.epic == "YOUR_EPIC_HERE":
            self.epic = ""

    @staticmethod
    def _normalise_market_name(value: Any) -> str:
        return " ".join(str(value or "").casefold().replace("-", " ").split())

    def resolve_instrument(self) -> str:
        """Resolve a market from Capital.com's own response, failing closed if ambiguous."""
        if self.epic:
            log.info("Using explicitly configured market epic: %s", self.epic)
            return self.epic

        search_term = CONFIG["instrument_search_term"]
        target_name = self._normalise_market_name(CONFIG["instrument_name"])
        if not search_term or not target_name:
            raise RuntimeError("Set INSTRUMENT_SEARCH_TERM and INSTRUMENT_NAME, or provide a verified INSTRUMENT_EPIC.")

        markets = self.api.search_markets(search_term)
        if not markets:
            raise CapitalAPIError(f"Capital.com returned no markets for search term {search_term!r}.")

        # Capital.com may label this instrument 'Gold' while the platform subtitle says
        # 'Gold Spot'. Match exact names only; do not choose a similar instrument by guess.
        accepted_names = {target_name}
        if target_name == "gold spot":
            accepted_names.add("gold")
        candidates = []
        for market in markets:
            if not isinstance(market, dict):
                continue
            epic = str(market.get("epic") or "").strip()
            names = {
                self._normalise_market_name(market.get("instrumentName")),
                self._normalise_market_name(market.get("marketName")),
            }
            if epic and names.intersection(accepted_names):
                candidates.append(market)

        # Prefer markets explicitly reported as tradeable if that field is present.
        tradeable = [m for m in candidates if str(m.get("marketStatus", "")).upper() == "TRADEABLE"]
        if tradeable:
            candidates = tradeable

        # Deduplicate repeated API records for the same epic.
        unique: dict[str, dict[str, Any]] = {}
        for market in candidates:
            unique[str(market["epic"]).strip()] = market

        if len(unique) != 1:
            summary = [
                {
                    "instrumentName": m.get("instrumentName"),
                    "marketName": m.get("marketName"),
                    "epic": m.get("epic"),
                    "marketStatus": m.get("marketStatus"),
                }
                for m in unique.values()
            ]
            if not summary:
                # Return concise candidate names for troubleshooting, but never guess one.
                summary = [
                    {"instrumentName": m.get("instrumentName"), "marketName": m.get("marketName"),
                     "epic": m.get("epic"), "marketStatus": m.get("marketStatus")}
                    for m in markets[:12] if isinstance(m, dict)
                ]
            raise CapitalAPIError(
                f"Could not uniquely verify {CONFIG['instrument_name']!r} from Capital.com Demo. "
                f"Matching/candidate results: {json.dumps(summary, ensure_ascii=False)[:1800]}. "
                "No trade will be started. Set INSTRUMENT_EPIC only after verifying the exact market."
            )

        selected = next(iter(unique.values()))
        epic = str(selected["epic"]).strip()
        log.info(
            "Auto-discovered Capital.com Demo market: name=%r, epic=%s, status=%s",
            selected.get("instrumentName") or selected.get("marketName"),
            epic,
            selected.get("marketStatus", "not supplied"),
        )
        return epic

    def get_market(self) -> MarketSnapshot:
        bars = self.api.prices(self.epic)
        closes = [v for bar in bars if (v := parse_price_bar(bar)) is not None and math.isfinite(v)]
        period = CONFIG["sma_period"]
        if len(closes) < period:
            raise CapitalAPIError(f"Not enough price bars: got {len(closes)}, need at least {period}.")
        sample = closes[-period:]
        sma = sum(sample) / period
        variance = sum((x - sma) ** 2 for x in sample) / period
        sd = math.sqrt(variance)
        # Current live bid/ask are represented by the latest returned bar when available.
        latest = bars[-1] if bars else {}
        close = latest.get("closePrice") or {}
        bid = as_float(close.get("bid"), closes[-1]) if isinstance(close, dict) else closes[-1]
        ask = as_float(close.get("ask"), closes[-1]) if isinstance(close, dict) else closes[-1]
        price = (bid + ask) / 2
        return MarketSnapshot(price=price, bid=bid, ask=ask, sma=sma, sd=sd)

    def broker_positions_for_epic(self) -> list[dict[str, Any]]:
        result = []
        for item in self.api.positions():
            p = extract_position(item)
            if p["deal_id"] and p["epic"] == self.epic:
                result.append(p)
        return result

    def reconcile(self) -> list[dict[str, Any]]:
        """Rebuild local open-position records from broker positions for this epic."""
        broker = self.broker_positions_for_epic()
        state = self.store.state()
        local = self.store.open_positions()
        broker_ids = {p["deal_id"] for p in broker}
        local_ids = {p["deal_id"] for p in local}
        # Remove local rows no longer open at the broker.
        for old in local:
            if old["deal_id"] not in broker_ids:
                self.store.log_event("POSITION_MISSING_AT_BROKER", basket_id=old["basket_id"],
                                     deal_id=old["deal_id"], details={"reconciled": True})
                self.store.remove_position(old["deal_id"])
        # Adopt broker positions not in local state, but do not silently mix multiple directions.
        if broker and not state.get("basket_id"):
            direction = broker[0]["direction"]
            basket_id = f"recovered-{int(time.time())}"
            self.store.update_state(basket_id=basket_id, direction=direction,
                                    anchor_price=broker[0]["entry_price"], max_basket_pl=0.0,
                                    profit_lock_active=0)
            state = self.store.state()
            log.warning("Recovered broker positions into basket %s; inspect logs and account.", basket_id)
        basket_id = state.get("basket_id")
        for p in broker:
            if p["deal_id"] not in local_ids and basket_id:
                same_direction = p["direction"] == (state.get("direction") or "")
                if not same_direction:
                    log.error("Untracked opposite-direction position %s found; will not trade until resolved.", p["deal_id"])
                    continue
                existing = self.store.open_positions(basket_id)
                level = max([int(x["level"]) for x in existing], default=-1) + 1
                self.store.save_position({
                    "deal_id": p["deal_id"], "basket_id": basket_id, "epic": self.epic,
                    "direction": p["direction"], "entry_price": p["entry_price"],
                    "size": p["size"], "level": level, "raw": p["raw"],
                })
        return self.broker_positions_for_epic()

    def basket_pl(self, positions: list[dict[str, Any]]) -> float:
        # The API's position.profit is the broker-reported floating P/L in account currency.
        return sum(as_float(p.get("profit"), 0.0) for p in positions)

    def start_basket(self, direction: str, price: float) -> None:
        state = self.store.state()
        if state.get("is_paused"):
            return
        if self.store.open_positions(state.get("basket_id")):
            return
        basket_id = f"basket-{int(time.time())}"
        self.store.update_state(
            basket_id=basket_id, direction=direction, anchor_price=price,
            max_basket_pl=0.0, profit_lock_active=0, last_error=None,
        )
        try:
            response = self.api.open_position(self.epic, direction, CONFIG["base_lot_size"])
            deal_reference = response.get("dealReference") if isinstance(response, dict) else None
            confirmed = self.confirm_deal(deal_reference) if deal_reference else {}
            deal_id = confirmed.get("dealId") or response.get("dealId")
            if not deal_id:
                raise CapitalAPIError(
                    f"Open request did not yield a confirmed dealId. Response={response}; confirm={confirmed}"
                )
            entry = as_float((confirmed.get("level") if confirmed else None), price)
            self.store.save_position({
                "deal_id": deal_id, "basket_id": basket_id, "epic": self.epic,
                "direction": direction, "entry_price": entry, "size": CONFIG["base_lot_size"],
                "level": 0, "raw": {"open": response, "confirm": confirmed},
            })
            self.store.log_event("BASKET_OPENED", basket_id=basket_id, deal_id=deal_id,
                                 direction=direction, size=CONFIG["base_lot_size"], price=entry)
            notify(f"Capital.com DEMO: opened {direction} basket {basket_id} at ~{entry}.")
            log.info("Opened %s basket %s; dealId=%s", direction, basket_id, deal_id)
        except Exception:
            self.store.update_state(basket_id=None, direction=None, anchor_price=None)
            raise

    def confirm_deal(self, deal_reference: Optional[str]) -> dict[str, Any]:
        if not deal_reference:
            return {}
        try:
            data = self.api.request("GET", f"api/v1/confirms/{deal_reference}")
            return data if isinstance(data, dict) else {}
        except CapitalAPIError as exc:
            log.warning("Could not confirm deal reference %s: %s", deal_reference, exc)
            return {}

    def open_grid_trade(self, state: dict[str, Any], positions: list[dict[str, Any]], price: float) -> None:
        basket_id = state.get("basket_id")
        local = self.store.open_positions(basket_id)
        if not local or len(local) >= 1 + CONFIG["max_grid_levels"]:
            return
        direction = state.get("direction")
        last = max(local, key=lambda p: int(p["level"]))
        entry = as_float(last.get("entry_price"), 0.0)
        if entry <= 0 or direction not in ("BUY", "SELL"):
            return
        adverse = price <= entry * (1 - CONFIG["grid_step_percent"]) if direction == "BUY" else price >= entry * (1 + CONFIG["grid_step_percent"])
        if not adverse:
            return
        # Do not stack multiple grid trades on a single unchanged tick.
        size = round(as_float(last["size"]) * CONFIG["lot_multiplier"], 8)
        next_level = int(last["level"]) + 1
        response = self.api.open_position(self.epic, direction, size)
        deal_reference = response.get("dealReference") if isinstance(response, dict) else None
        confirmed = self.confirm_deal(deal_reference) if deal_reference else {}
        deal_id = confirmed.get("dealId") or response.get("dealId")
        if not deal_id:
            raise CapitalAPIError(f"Grid open did not return a confirmed dealId: {response}")
        entry_price = as_float(confirmed.get("level"), price)
        self.store.save_position({
            "deal_id": deal_id, "basket_id": basket_id, "epic": self.epic,
            "direction": direction, "entry_price": entry_price, "size": size,
            "level": next_level, "raw": {"open": response, "confirm": confirmed},
        })
        self.store.log_event("GRID_OPENED", basket_id=basket_id, deal_id=deal_id,
                             direction=direction, size=size, price=entry_price,
                             details={"level": next_level})
        notify(f"Capital.com DEMO: grid level {next_level}/{CONFIG['max_grid_levels']} opened; size={size}.")

    def close_basket(self, positions: list[dict[str, Any]], reason: str, basket_pl: float) -> bool:
        state = self.store.state()
        basket_id = state.get("basket_id")
        all_closed = True
        for p in positions:
            deal_id = p.get("deal_id")
            if not deal_id:
                continue
            try:
                self.api.close_position(str(deal_id))
                # Verify closure with a fresh broker position list before deleting local state.
                remaining_ids = {x["deal_id"] for x in self.broker_positions_for_epic()}
                if deal_id in remaining_ids:
                    all_closed = False
                    log.error("Close requested but position %s still appears open.", deal_id)
                    continue
                self.store.log_event("POSITION_CLOSED", basket_id=basket_id, deal_id=deal_id,
                                     direction=p.get("direction"), basket_pl=basket_pl,
                                     details={"reason": reason})
                self.store.remove_position(str(deal_id))
            except Exception as exc:
                all_closed = False
                self.store.log_event("CLOSE_FAILED", basket_id=basket_id, deal_id=deal_id,
                                     basket_pl=basket_pl, details={"reason": reason, "error": str(exc)})
                log.exception("Failed closing deal %s", deal_id)
        if all_closed:
            self.store.log_event("BASKET_CLOSED", basket_id=basket_id, basket_pl=basket_pl,
                                 details={"reason": reason})
            pause = reason == "HARD_STOP_LOSS"
            self.store.update_state(
                basket_id=None, direction=None, anchor_price=None,
                max_basket_pl=0.0, profit_lock_active=0,
                is_paused=1 if pause else 0,
                pause_until=time.time() + CONFIG["pause_duration_seconds"] if pause else None,
                last_error=None,
            )
            notify(f"Capital.com DEMO: basket closed ({reason}); basket P/L snapshot={basket_pl:.2f}.")
        else:
            log.error("Basket remains active because one or more positions could not be verified closed.")
        return all_closed

    def tick(self) -> None:
        state = self.store.state()
        now = time.time()
        if state.get("is_paused"):
            pause_until = as_float(state.get("pause_until"), 0.0)
            if now < pause_until:
                log.info("Paused after hard stop; %.0f seconds remaining.", pause_until - now)
                return
            self.store.update_state(is_paused=0, pause_until=None)
            self.store.log_event("PAUSE_EXPIRED", details={"resumed_at": utc_now()})
            notify("Capital.com DEMO: one-hour hard-stop pause expired; bot resumed.")
            state = self.store.state()

        market = self.get_market()
        broker_positions = self.reconcile()
        state = self.store.state()
        local = self.store.open_positions(state.get("basket_id"))
        if not local and not broker_positions:
            # Clear stale basket state safely; no live broker position is attached to this epic.
            if state.get("basket_id"):
                self.store.update_state(basket_id=None, direction=None, anchor_price=None,
                                        max_basket_pl=0.0, profit_lock_active=0)
            if state.get("is_paused"):
                return
            if market.price < market.sma - CONFIG["sd_multiplier"] * market.sd:
                self.start_basket("BUY", market.ask)
            elif market.price > market.sma + CONFIG["sd_multiplier"] * market.sd:
                self.start_basket("SELL", market.bid)
            else:
                log.info("No entry. price=%.5f SMA=%.5f SD=%.5f", market.price, market.sma, market.sd)
            return

        # If broker has positions that could not be safely associated with our basket, fail closed.
        state = self.store.state()
        if not state.get("basket_id") or not local:
            log.error("Broker/local position mismatch. No new trades will be opened; inspect account and logs.")
            return

        # Re-query broker position profit as close as possible to decision time.
        broker_positions = self.broker_positions_for_epic()
        basket_ids = {p["deal_id"] for p in local}
        basket_positions = [p for p in broker_positions if p["deal_id"] in basket_ids]
        if not basket_positions:
            log.warning("No broker positions match current local basket; awaiting reconciliation.")
            return
        total_pl = self.basket_pl(basket_positions)
        max_pl = max(as_float(state.get("max_basket_pl"), 0.0), total_pl)
        lock_active = bool(state.get("profit_lock_active")) or max_pl >= CONFIG["profit_lock_activation"]
        self.store.update_state(max_basket_pl=max_pl, profit_lock_active=1 if lock_active else 0)
        log.info("Basket %s | open=%d | P/L=%.2f | peak=%.2f | lock=%s | price=%.5f",
                 state.get("basket_id"), len(basket_positions), total_pl, max_pl, lock_active, market.price)

        if total_pl >= CONFIG["tp_target"]:
            self.close_basket(basket_positions, "TAKE_PROFIT", total_pl)
            return
        if total_pl <= CONFIG["sl_target"]:
            self.close_basket(basket_positions, "HARD_STOP_LOSS", total_pl)
            return
        if lock_active and total_pl <= max_pl - CONFIG["profit_lock_trail_drop"]:
            self.close_basket(basket_positions, "PROFIT_LOCK", total_pl)
            return

        # Grid count is based on persisted basket positions. Never exceed initial + max_grid_levels.
        self.open_grid_trade(state, basket_positions, market.price)

    def run(self) -> None:
        self.api.authenticate()
        self.epic = self.resolve_instrument()
        # Verify the selected epic actually serves price history before entering the loop.
        initial_prices = self.api.prices(self.epic)
        if len(initial_prices) < CONFIG["sma_period"]:
            raise CapitalAPIError(
                f"Resolved market epic {self.epic!r}, but only {len(initial_prices)} price bars were returned; "
                f"at least {CONFIG['sma_period']} are required. No trading started."
            )
        log.info("Worker started for verified epic=%s (DEMO). Poll interval=%ss", self.epic, CONFIG["poll_seconds"])
        notify(f"Capital.com DEMO bot started for epic {self.epic}.")
        while not STOP_REQUESTED:
            try:
                self.tick()
                # Persist state after every tick, including state timestamps.
                self.store.update_state()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                log.exception("Tick failed: %s", exc)
                self.store.update_state(last_error=str(exc)[:1000])
                notify(f"Capital.com DEMO bot tick error: {str(exc)[:500]}")
            for _ in range(CONFIG["poll_seconds"]):
                if STOP_REQUESTED:
                    break
                time.sleep(1)
        self.store.close()
        log.info("Worker stopped.")


def request_shutdown(signum, frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    log.info("Shutdown signal received (%s).", signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    TradingBot().run()
