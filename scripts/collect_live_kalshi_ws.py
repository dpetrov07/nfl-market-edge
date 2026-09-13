"""Record compact, latency-safe Kalshi data for one NFL game."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import gzip
import json
import os
import re
import signal
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"
WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"
SERIES = {
    "KXNFLRECYDS": "receiving_yards",
    "KXNFLRSHYDS": "rushing_yards",
    "KXNFLGAME": "moneyline",
    "KXNFLSPREAD": "spread",
}
CHANNELS = ("ticker", "orderbook_delta", "trade", "market_lifecycle_v2")
ET = ZoneInfo("America/New_York")
TEAM_CODES = sorted(
    {
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL",
        "DEN", "DET", "GB", "HOU", "IND", "JAC", "JAX", "KC", "LA",
        "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG", "NYJ",
        "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS", "WSH",
    },
    key=len,
    reverse=True,
)
ALIASES = {"JAX": "JAC", "LA": "LAR", "WSH": "WAS"}
STOP = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_local_env(path: Path = Path(".env")) -> None:
    """Load only this collector's settings, including an unquoted multiline PEM."""
    if not path.exists():
        return
    text = path.read_text()
    keys = (
        "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY", "KALSHI_PRIVATE_KEY_PEM",
        "KALSHI_PRIVATE_KEY_B64",
        "KALSHI_PRIVATE_KEY_PATH", "KALSHI_GAME", "KALSHI_GAME_DATE",
        "KALSHI_OUTPUT_DIR", "KALSHI_CHANNELS", "KALSHI_HEARTBEAT_SECONDS",
        "KALSHI_TOP_SIZE_CHANGE",
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


def requested_pair(value: str) -> tuple[str, str]:
    parts = [part for part in re.split(r"[^A-Za-z]+", value.upper()) if part]
    if len(parts) != 2:
        raise SystemExit("--game must use team abbreviations, for example SF_LAR")
    return tuple(ALIASES.get(part, part) for part in parts)


def event_pair(event_ticker: str) -> tuple[str, str] | None:
    suffix = event_ticker.split("-", 1)[-1]
    match = re.fullmatch(r"\d{2}[A-Z]{3}\d{2}([A-Z]+)", suffix)
    if not match:
        return None
    pair = match.group(1)
    for away in TEAM_CODES:
        home = pair[len(away) :] if pair.startswith(away) else ""
        if home in TEAM_CODES:
            return ALIASES.get(away, away), ALIASES.get(home, home)
    return None


def event_date(event_ticker: str):
    try:
        token = event_ticker.split("-", 1)[1][:7]
        return datetime.strptime(token, "%y%b%d").date()
    except (IndexError, ValueError):
        return None


def get_json(path: str, params: dict) -> dict:
    url = f"{REST_BASE}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "nfl-market-edge/0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def discover_markets(game: str, date_text: str) -> list[dict]:
    pair = requested_pair(game)
    wanted_date = datetime.strptime(date_text, "%Y-%m-%d").date()
    found = []
    for series_ticker, prop_type in SERIES.items():
        cursor = None
        while True:
            params = {"series_ticker": series_ticker, "status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            payload = get_json("/markets", params)
            for market in payload.get("markets", []):
                if event_pair(market.get("event_ticker", "")) != pair:
                    continue
                if event_date(market.get("event_ticker", "")) != wanted_date:
                    continue
                label = market.get("yes_sub_title") or market.get("title", "")
                player = label.split(":", 1)[0].strip() if prop_type.endswith("_yards") else None
                outcome_team = None
                if prop_type == "moneyline":
                    outcome_team = label
                elif prop_type == "spread":
                    outcome_team = label.split(" wins", 1)[0]
                found.append(
                    {
                        **market,
                        "game": f"{pair[0]} @ {pair[1]}",
                        "market_kind": "player_prop" if prop_type.endswith("_yards") else prop_type,
                        "prop_type": prop_type,
                        "player": player,
                        "player_id": market.get("custom_strike", {}).get("football_player"),
                        "threshold": market.get("floor_strike"),
                        "outcome_team": outcome_team,
                    }
                )
            cursor = payload.get("cursor")
            if not cursor:
                break
    return sorted(found, key=lambda row: row["ticker"])


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
                    "set KALSHI_PRIVATE_KEY, KALSHI_PRIVATE_KEY_PATH, or KALSHI_PRIVATE_KEY_B64"
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
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    }


class JsonlWriter:
    def __init__(self, output_dir: Path, game: str, game_date: str):
        output_dir.mkdir(parents=True, exist_ok=True)
        game_key = "_".join(requested_pair(game))
        self.path = output_dir / f"kalshi_ws_{game_date}_{game_key}.jsonl.gz"
        self.discovery_signature = self._last_discovery_signature()
        self.handle = gzip.open(self.path, "at", encoding="utf-8", compresslevel=6)

    @staticmethod
    def _discovery_signature(record: dict) -> str:
        payload = {key: value for key, value in record.items() if key != "received_at"}
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

    def _last_discovery_signature(self) -> str | None:
        if not self.path.exists():
            return None
        signature = None
        try:
            with gzip.open(self.path, "rt", encoding="utf-8") as existing:
                for line in existing:
                    record = json.loads(line)
                    if record.get("record_type") == "market_discovery":
                        signature = self._discovery_signature(record)
        except (EOFError, gzip.BadGzipFile, json.JSONDecodeError, UnicodeDecodeError):
            pass
        return signature

    def write(self, record: dict) -> None:
        self.handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")

    def write_discovery(self, record: dict) -> bool:
        signature = self._discovery_signature(record)
        if signature == self.discovery_signature:
            return False
        self.write(record)
        self.discovery_signature = signature
        return True

    def status(self, status: str, **details) -> None:
        self.write({"record_type": "collector_status", "received_at": utc_now(), "status": status, **details})

    def close(self) -> None:
        self.handle.close()

    def flush(self) -> None:
        self.handle.flush()


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
    """Maintain full books in memory and return only compact records worth saving."""

    def __init__(self, tickers: list[str], top_size_change: float = 10.0):
        self.tickers = set(tickers)
        self.top_size_change = top_size_change
        # use_yes_price=true puts YES bids and YES asks on the same price scale.
        self.books = {ticker: {"yes": {}, "no": {}} for ticker in tickers}
        self.initialized: set[str] = set()
        self.last_top: dict[str, tuple] = {}
        self.last_status: dict[str, str] = {}
        self.last_lifecycle: dict[str, tuple] = {}
        self.ticker_stats: dict[str, dict] = {}

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
            if yes_bid is not None and yes_ask is not None else None,
        }

    def _top_record(self, ticker: str, received_at: str, raw: dict, reason: str) -> dict | None:
        top = self._top_values(ticker)
        signature = tuple(top.values())
        previous = self.last_top.get(ticker)
        names = tuple(top)
        prices = {
            "yes_bid_dollars", "yes_ask_dollars", "no_bid_dollars",
            "no_ask_dollars", "spread_dollars",
        }
        changed = list(names) if previous is None else [
            name for name, old, new in zip(names, previous, signature)
            if old != new and (
                name in prices or old is None or new is None
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
                    record = self._top_record(ticker, received_at, raw, "orderbook_delta")
                    return [record] if record else []
            return []

        if message_type == "trade" and ticker in self.tickers:
            count = msg.get("count_fp") if msg.get("count_fp") is not None else msg.get("count")
            return [{
                "record_type": "trade",
                **common,
                "trade_id": msg.get("trade_id"),
                "yes_price_dollars": dollar_value(msg, "yes_price_dollars", "yes_price"),
                "no_price_dollars": dollar_value(msg, "no_price_dollars", "no_price"),
                "count": number(count),
                "taker_outcome_side": msg.get("taker_outcome_side") or msg.get("taker_side"),
                "taker_book_side": msg.get("taker_book_side"),
                "is_block_trade": msg.get("is_block_trade", False),
            }]

        if message_type == "ticker" and ticker in self.tickers:
            volume = msg.get("volume_fp") if msg.get("volume_fp") is not None else msg.get("volume")
            interest = (
                msg.get("open_interest_fp") if msg.get("open_interest_fp") is not None
                else msg.get("open_interest")
            )
            self.ticker_stats[ticker] = {
                "last_price_dollars": dollar_value(msg, "price_dollars", "price"),
                "volume": number(volume),
                "open_interest": number(interest),
            }
            status = msg.get("status") or msg.get("market_status")
            if status and self.last_status.get(ticker) != status:
                self.last_status[ticker] = status
                return [{"record_type": "market_status", **common, "status": status}]
            return []

        if message_type == "market_lifecycle_v2" and ticker in self.tickers:
            event_type = msg.get("event_type")
            signature = (
                event_type, msg.get("result"), msg.get("open_ts"), msg.get("close_ts"),
                msg.get("determination_ts"), msg.get("settled_ts") or msg.get("settlement_ts"),
                msg.get("settlement_value"),
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
                "settled_ts": msg.get("settled_ts") or msg.get("settlement_ts"),
                "settlement_value_dollars": msg.get("settlement_value"),
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


async def subscribe(ws, tickers: list[str], channels: list[str], writer: JsonlWriter) -> None:
    for command_id, channel in enumerate(channels, start=1):
        params = {"channels": [channel]}
        if channel != "market_lifecycle_v2":
            params["market_tickers"] = tickers
        if channel == "orderbook_delta":
            params["use_yes_price"] = True
        command = {
            "id": command_id,
            "cmd": "subscribe",
            "params": params,
        }
        await ws.send(json.dumps(command))
        writer.status("subscription_sent", channel=channel, market_count=len(tickers))


async def collect(args: argparse.Namespace, markets: list[dict], writer: JsonlWriter) -> None:
    key_id = args.key_id or os.getenv("KALSHI_API_KEY_ID")
    if not key_id:
        raise SystemExit("set KALSHI_API_KEY_ID")
    private_key = load_private_key(args)
    tickers = [market["ticker"] for market in markets]
    started = time.monotonic()
    attempt, backoff = 0, 1
    while not STOP:
        if args.duration and time.monotonic() - started >= args.duration:
            break
        attempt += 1
        connection_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}-{attempt}"
        try:
            writer.status(
                "connecting", attempt=attempt, connection_id=connection_id, market_count=len(tickers)
            )
            async with websockets.connect(
                WS_URL,
                additional_headers=auth_headers(key_id, private_key),
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
                max_queue=10000,
            ) as ws:
                writer.status("connected", attempt=attempt, connection_id=connection_id)
                print(f"Connected; subscribing to {len(tickers)} markets", flush=True)
                await subscribe(ws, tickers, args.channels, writer)
                backoff = 1
                last_sequence = {}
                state = MarketState(tickers, args.top_size_change)
                connected_at = time.monotonic()
                last_heartbeat = 0.0
                last_message_at = None
                message_count = 0
                while not STOP:
                    if args.duration and time.monotonic() - started >= args.duration:
                        writer.status("stopped", reason="duration_reached")
                        return
                    now = time.monotonic()
                    if now - last_heartbeat >= args.heartbeat_seconds:
                        writer.write({
                            "record_type": "heartbeat",
                            "received_at": utc_now(),
                            "connection_id": connection_id,
                            "connected_seconds": round(now - connected_at, 3),
                            "market_count": len(tickers),
                            "books_initialized": len(state.initialized),
                            "ticker_markets_seen": len(state.ticker_stats),
                            "messages_received": message_count,
                            "last_message_at": last_message_at,
                        })
                        writer.flush()
                        last_heartbeat = now
                    try:
                        raw_text = await asyncio.wait_for(ws.recv(), timeout=1)
                    except asyncio.TimeoutError:
                        continue
                    received_at = utc_now()
                    last_message_at = received_at
                    message_count += 1
                    try:
                        raw = json.loads(raw_text)
                    except (json.JSONDecodeError, TypeError) as exc:
                        writer.status(
                            "malformed_ws_message",
                            connection_id=connection_id,
                            error=str(exc),
                            message_bytes=len(raw_text),
                        )
                        continue
                    msg = raw.get("msg") if isinstance(raw.get("msg"), dict) else {}
                    sid, seq = raw.get("sid") or msg.get("sid"), raw.get("seq")
                    if isinstance(sid, int) and isinstance(seq, int):
                        previous = last_sequence.get(sid)
                        if previous is not None and seq != previous + 1:
                            writer.status(
                                "sequence_gap",
                                connection_id=connection_id,
                                sid=sid,
                                expected_seq=previous + 1,
                                received_seq=seq,
                                market_ticker=msg.get("market_ticker") or msg.get("ticker"),
                            )
                            raise RuntimeError(
                                f"sequence gap on sid {sid}; reconnecting for fresh snapshots"
                            )
                        last_sequence[sid] = seq
                    for record in state.process(raw, received_at):
                        record["connection_id"] = connection_id
                        writer.write(record)
                    if raw.get("type") in {"subscribed", "error"}:
                        print(json.dumps(raw), flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            writer.status(
                "disconnected",
                attempt=attempt,
                connection_id=connection_id,
                error=f"{type(exc).__name__}: {exc}",
                reconnect_in_seconds=backoff,
            )
            print(f"Kalshi WebSocket disconnected: {exc}; retrying in {backoff}s", flush=True)
            if args.duration and time.monotonic() - started + backoff >= args.duration:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
    writer.status("stopped")


def parse_args() -> argparse.Namespace:
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    default_output = os.getenv("KALSHI_OUTPUT_DIR") or (
        str(Path(volume) / "kalshi_live") if volume else "data/raw/kalshi_live"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", default=os.getenv("KALSHI_GAME", "SF_LAR"))
    parser.add_argument("--date", default=os.getenv("KALSHI_GAME_DATE", datetime.now(ET).date().isoformat()))
    parser.add_argument("--output-dir", type=Path, default=Path(default_output))
    parser.add_argument("--key-id", help="Prefer KALSHI_API_KEY_ID")
    parser.add_argument("--private-key-path", help="Prefer KALSHI_PRIVATE_KEY_PATH")
    parser.add_argument(
        "--channels",
        default=os.getenv("KALSHI_CHANNELS", ",".join(CHANNELS)),
        help="Comma-separated WebSocket channels",
    )
    parser.add_argument("--list-only", action="store_true", help="Discover markets without connecting")
    parser.add_argument("--duration", type=float, help="Stop after N seconds; omit to run continuously")
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=float(os.getenv("KALSHI_HEARTBEAT_SECONDS", "5")),
    )
    parser.add_argument(
        "--top-size-change",
        type=float,
        default=float(os.getenv("KALSHI_TOP_SIZE_CHANGE", "10")),
        help="Persist same-price top-size changes after this many contracts",
    )
    args = parser.parse_args()
    args.channels = [item.strip() for item in args.channels.split(",") if item.strip()]
    unknown = set(args.channels) - set(CHANNELS)
    if unknown:
        parser.error(f"unsupported channels: {', '.join(sorted(unknown))}")
    if args.top_size_change < 0:
        parser.error("--top-size-change must be non-negative")
    if "market_lifecycle_v2" not in args.channels:
        args.channels.append("market_lifecycle_v2")
    return args


def request_stop(_signum, _frame) -> None:
    global STOP
    STOP = True


def main() -> None:
    load_local_env()
    args = parse_args()
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    markets = discover_markets(args.game, args.date)
    counts = {prop: sum(row["prop_type"] == prop for row in markets) for prop in SERIES.values()}
    event_tickers = sorted({row["event_ticker"] for row in markets})
    print(f"Discovered {len(markets)} active markets: {counts}; events={event_tickers}", flush=True)
    if not markets:
        raise SystemExit("No matching active NFL markets found")
    if args.list_only:
        return

    writer = JsonlWriter(args.output_dir, args.game, args.date)
    compact_markets = [{
        key: market.get(key) for key in (
            "ticker", "market_id", "event_ticker", "game", "market_kind", "prop_type",
            "player", "player_id", "threshold", "outcome_team", "title", "yes_sub_title",
            "status", "open_time", "close_time",
        )
    } for market in markets]
    writer.write_discovery({
        "record_type": "market_discovery",
        "received_at": utc_now(),
        "game": args.game,
        "game_date": args.date,
        "channels": args.channels,
        "markets": compact_markets,
    })
    print(f"Saving compact market events to {writer.path}", flush=True)
    try:
        asyncio.run(collect(args, markets, writer))
    finally:
        writer.close()


if __name__ == "__main__":
    main()
