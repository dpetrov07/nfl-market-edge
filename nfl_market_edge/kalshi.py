"""Shared Kalshi authentication and REST client helpers."""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


API_BASE = "https://external-api.kalshi.com"
API_ROOT = "/trade-api/v2"
REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"
WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def number(value, cents: bool = False) -> float | None:
    try:
        parsed = float(value)
        return parsed / 100 if cents else parsed
    except (TypeError, ValueError):
        return None


def dollar_value(msg: dict, dollars_key: str, cents_key: str) -> float | None:
    value = number(msg.get(dollars_key))
    return value if value is not None else number(msg.get(cents_key), cents=True)


class MarketState:
    """Maintain full books and emit compact top-of-book/lifecycle records."""

    def __init__(self, tickers: list[str], top_size_change: float = 10.0):
        self.tickers = set(tickers)
        self.top_size_change = top_size_change
        self.books = {ticker: {"yes": {}, "no": {}} for ticker in tickers}
        self.initialized: set[str] = set()
        self.last_top: dict[str, tuple] = {}
        self.last_status: dict[str, str] = {}
        self.last_lifecycle: dict[str, tuple] = {}
        self.ticker_stats: dict[str, dict] = {}

    def add_tickers(self, tickers: list[str]) -> None:
        for ticker in tickers:
            if ticker not in self.tickers:
                self.tickers.add(ticker)
                self.books[ticker] = {"yes": {}, "no": {}}

    @staticmethod
    def _levels(msg: dict, side: str) -> dict[float, float]:
        values = msg.get(f"{side}_dollars_fp") or msg.get(f"{side}_dollars")
        cents = False
        if values is None:
            values, cents = msg.get(side, []), True
        levels = {}
        for price, size in values or []:
            parsed_price, parsed_size = number(price, cents=cents), number(size)
            if parsed_price is not None and parsed_size and parsed_size > 0:
                levels[parsed_price] = round(parsed_size, 4)
        return levels

    def _top_values(self, ticker: str) -> dict:
        book = self.books[ticker]
        yes_bid = max(book["yes"], default=None)
        yes_ask = min(book["no"], default=None)
        return {
            "yes_bid_dollars": yes_bid,
            "yes_bid_size": book["yes"].get(yes_bid),
            "yes_ask_dollars": yes_ask,
            "yes_ask_size": book["no"].get(yes_ask),
            "no_bid_dollars": round(1 - yes_ask, 10) if yes_ask is not None else None,
            "no_bid_size": book["no"].get(yes_ask),
            "no_ask_dollars": round(1 - yes_bid, 10) if yes_bid is not None else None,
            "no_ask_size": book["yes"].get(yes_bid),
            "spread_dollars": round(yes_ask - yes_bid, 10)
            if yes_bid is not None and yes_ask is not None
            else None,
        }

    def _top_record(
        self, ticker: str, received_at: str, raw: dict, reason: str
    ) -> dict | None:
        top = self._top_values(ticker)
        signature = tuple(top.values())
        previous = self.last_top.get(ticker)
        names = tuple(top)
        prices = {
            "yes_bid_dollars", "yes_ask_dollars", "no_bid_dollars",
            "no_ask_dollars", "spread_dollars",
        }
        changed = list(names) if previous is None else [
            name
            for name, old, new in zip(names, previous, signature)
            if old != new
            and (
                name in prices
                or old is None
                or new is None
                or abs(new - old) >= self.top_size_change
            )
        ]
        if not changed:
            return None
        self.last_top[ticker] = signature
        msg = raw.get("msg", {})
        return {
            "record_type": "top_of_book",
            "received_at": received_at,
            "market_ticker": ticker,
            "exchange_timestamp": msg.get("time") or msg.get("ts"),
            "exchange_ts_ms": msg.get("ts_ms"),
            "sid": raw.get("sid"),
            "seq": raw.get("seq"),
            "reason": reason,
            "changed_fields": changed,
            **top,
        }

    def process(self, raw: dict, received_at: str) -> list[dict]:
        message_type = raw.get("type")
        msg = raw.get("msg") if isinstance(raw.get("msg"), dict) else {}
        ticker = msg.get("market_ticker") or msg.get("ticker")
        common = {
            "received_at": received_at,
            "market_ticker": ticker,
            "exchange_timestamp": msg.get("time") or msg.get("ts"),
            "exchange_ts_ms": msg.get("ts_ms"),
            "sid": raw.get("sid"),
            "seq": raw.get("seq"),
        }

        if message_type == "orderbook_snapshot" and ticker in self.tickers:
            self.books[ticker] = {
                "yes": self._levels(msg, "yes"),
                "no": self._levels(msg, "no"),
            }
            self.initialized.add(ticker)
            record = self._top_record(ticker, received_at, raw, "snapshot")
            return [record] if record else []

        if message_type == "orderbook_delta" and ticker in self.tickers:
            side = msg.get("side")
            price = dollar_value(msg, "price_dollars", "price")
            delta = number(msg.get("delta_fp"))
            if delta is None:
                delta = number(msg.get("delta"))
            if side in {"yes", "no"} and price is not None and delta is not None:
                levels = self.books[ticker][side]
                size = round(levels.get(price, 0) + delta, 4)
                if size > 0:
                    levels[price] = size
                else:
                    levels.pop(price, None)
                if ticker in self.initialized:
                    record = self._top_record(
                        ticker, received_at, raw, "orderbook_delta"
                    )
                    return [record] if record else []
            return []

        if message_type == "trade" and ticker in self.tickers:
            count = (
                msg.get("count_fp")
                if msg.get("count_fp") is not None
                else msg.get("count")
            )
            return [{
                "record_type": "trade",
                **common,
                "trade_id": msg.get("trade_id"),
                "yes_price_dollars": dollar_value(
                    msg, "yes_price_dollars", "yes_price"
                ),
                "no_price_dollars": dollar_value(
                    msg, "no_price_dollars", "no_price"
                ),
                "count": number(count),
                "taker_outcome_side": msg.get("taker_outcome_side")
                or msg.get("taker_side"),
                "taker_book_side": msg.get("taker_book_side"),
                "is_block_trade": msg.get("is_block_trade", False),
            }]

        if message_type == "ticker" and ticker in self.tickers:
            volume = (
                msg.get("volume_fp")
                if msg.get("volume_fp") is not None
                else msg.get("volume")
            )
            interest = (
                msg.get("open_interest_fp")
                if msg.get("open_interest_fp") is not None
                else msg.get("open_interest")
            )
            self.ticker_stats[ticker] = {
                "last_price_dollars": dollar_value(
                    msg, "price_dollars", "price"
                ),
                "volume": number(volume),
                "open_interest": number(interest),
            }
            status = msg.get("status") or msg.get("market_status")
            if status and self.last_status.get(ticker) != status:
                self.last_status[ticker] = status
                return [{"record_type": "market_status", **common, "status": status}]
            return []

        if (
            message_type in {"market_lifecycle_v2", "multivariate_market_lifecycle"}
            and ticker in self.tickers
        ):
            event_type = msg.get("event_type")
            signature = (
                event_type,
                msg.get("result"),
                msg.get("open_ts"),
                msg.get("close_ts"),
                msg.get("determination_ts"),
                msg.get("settled_ts") or msg.get("settlement_ts"),
                msg.get("settlement_value_dollars")
                or msg.get("settlement_value"),
            )
            if self.last_lifecycle.get(ticker) == signature:
                return []
            self.last_lifecycle[ticker] = signature
            if event_type:
                self.last_status[ticker] = event_type
            return [{
                "record_type": "market_status",
                **common,
                "status": event_type,
                "result": msg.get("result"),
                "open_ts": msg.get("open_ts"),
                "close_ts": msg.get("close_ts"),
                "determination_ts": msg.get("determination_ts"),
                "settled_ts": msg.get("settled_ts")
                or msg.get("settlement_ts"),
                "settlement_value_dollars": msg.get("settlement_value_dollars")
                or msg.get("settlement_value"),
            }]

        if message_type in {"subscribed", "error"}:
            return [{
                "record_type": "websocket_control",
                "received_at": received_at,
                "message_type": message_type,
                "id": raw.get("id"),
                "sid": raw.get("sid") or msg.get("sid"),
                "channel": msg.get("channel"),
                "code": msg.get("code"),
                "message": msg.get("msg"),
            }]
        return []


def load_local_env(path: Path = Path(".env")) -> None:
    """Load only collector settings, including an unquoted multiline PEM."""
    if not path.exists():
        return
    text = path.read_text()
    keys = (
        "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PEM",
        "KALSHI_PRIVATE_KEY_B64", "KALSHI_PRIVATE_KEY_PATH", "KALSHI_GAME",
        "KALSHI_GAMES", "KALSHI_GAME_DATE", "KALSHI_OUTPUT_DIR",
        "KALSHI_CHANNELS", "KALSHI_HEARTBEAT_SECONDS", "KALSHI_TOP_SIZE_CHANGE",
        "SLATE_MANIFEST_JSON", "COLLECTOR_OUTPUT_ROOT",
    )
    for key in keys:
        if key in os.environ:
            continue
        match = re.search(rf"(?m)^{re.escape(key)}=(.*)$", text)
        if not match:
            continue
        value = match.group(1).strip()
        if "PRIVATE_KEY" in key and ("-----BEGIN" in value or not value):
            start = text.find("-----BEGIN", match.start(1))
            if start >= 0:
                end = re.search(r"-----END (?:RSA )?PRIVATE KEY-----", text[start:])
                if end:
                    value = text[start : start + end.end()].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value.replace("\\n", "\n")


def load_private_key(args: argparse.Namespace):
    value = os.getenv("KALSHI_PRIVATE_KEY_PEM") or os.getenv("KALSHI_PRIVATE_KEY")
    if value and "BEGIN" in value:
        pem_bytes = value.replace("\\n", "\n").encode()
    else:
        path = args.private_key_path or os.getenv("KALSHI_PRIVATE_KEY_PATH") or value
        if path:
            pem_bytes = Path(path).expanduser().read_bytes()
        else:
            encoded = os.getenv("KALSHI_PRIVATE_KEY_B64")
            if not encoded:
                raise SystemExit(
                    "set KALSHI_PRIVATE_KEY, KALSHI_PRIVATE_KEY_PATH, or "
                    "KALSHI_PRIVATE_KEY_B64"
                )
            try:
                pem_bytes = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise SystemExit("KALSHI_PRIVATE_KEY_B64 is not valid base64") from exc
    return serialization.load_pem_private_key(pem_bytes, password=None)


def auth_headers(key_id: str, private_key) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}GET{WS_PATH}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    }


class KalshiClient:
    def __init__(self, authenticated: bool):
        self.key_id = None
        self.private_key = None
        self.denied_paths = set()
        self.sessions = threading.local()
        if authenticated:
            load_local_env()
            self.key_id = os.getenv("KALSHI_API_KEY_ID")
            if self.key_id:
                self.private_key = load_private_key(
                    argparse.Namespace(private_key_path=None)
                )

    def get(self, path: str, params: dict | None = None, auth: bool = False):
        if path in self.denied_paths:
            return {}
        url = API_BASE + path
        if not hasattr(self.sessions, "session"):
            self.sessions.session = requests.Session()
        for attempt in range(6):
            try:
                headers = {"User-Agent": "nfl-market-edge/combo-discovery"}
                if auth:
                    if not self.key_id or not self.private_key:
                        return {}
                    timestamp = str(int(time.time() * 1000))
                    signature = self.private_key.sign(
                        f"{timestamp}GET{path}".encode(),
                        padding.PSS(
                            mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.DIGEST_LENGTH,
                        ),
                        hashes.SHA256(),
                    )
                    headers.update(
                        {
                            "KALSHI-ACCESS-KEY": self.key_id,
                            "KALSHI-ACCESS-TIMESTAMP": timestamp,
                            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(
                                signature
                            ).decode(),
                        }
                    )
                response = self.sessions.session.get(
                    url, params=params, headers=headers, timeout=120
                )
                response.raise_for_status()
                return response.json()
            except requests.HTTPError as exc:
                status = exc.response.status_code
                if auth and status in (401, 403):
                    self.denied_paths.add(path)
                    return {}
                if status not in (429, 500, 502, 503, 504) or attempt == 5:
                    raise
                time.sleep(float(exc.response.headers.get("Retry-After") or 2**attempt))
            except requests.RequestException:
                if attempt == 5:
                    raise
                time.sleep(2**attempt)
        return {}

    def paginate(
        self,
        path: str,
        params: dict,
        field: str,
        auth: bool = False,
        row_filter=None,
        progress_label: str | None = None,
        max_pages: int | None = None,
    ):
        rows, cursor, pages, scanned = [], None, 0, 0
        seen_cursors = set()
        while True:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            payload = self.get(path, page_params, auth=auth)
            page_rows = payload.get(field, [])
            scanned += len(page_rows)
            rows.extend(
                row for row in page_rows if row_filter is None or row_filter(row)
            )
            pages += 1
            if progress_label and pages % 25 == 0:
                print(
                    f"{progress_label}: {scanned} markets scanned ({pages} pages), "
                    f"{len(rows)} Sunday traded 2/3-leg combos retained",
                    file=sys.stderr,
                    flush=True,
                )
            if max_pages and pages >= max_pages:
                return rows, pages, scanned
            cursor = payload.get("cursor")
            if not cursor:
                return rows, pages, scanned
            if cursor in seen_cursors:
                raise RuntimeError(f"repeated pagination cursor from {path}")
            seen_cursors.add(cursor)
