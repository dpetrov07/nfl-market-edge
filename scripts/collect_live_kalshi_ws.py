"""Record raw Kalshi WebSocket data for one NFL receiving/rushing game."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
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
CHANNELS = ("ticker", "orderbook_delta", "trade")
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
        "KALSHI_OUTPUT_DIR", "KALSHI_CHANNELS",
    )
    for key in keys:
        if key in os.environ:
            continue
        match = re.search(rf"(?m)^{re.escape(key)}=(.*)$", text)
        if not match:
            continue
        value = match.group(1).strip()
        if "PRIVATE_KEY" in key and "-----BEGIN" in value and "-----END" not in value:
            end = re.search(r"-----END (?:RSA )?PRIVATE KEY-----", text[match.start(1) :])
            if end:
                value = text[match.start(1) : match.start(1) + end.end()].strip()
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
    def __init__(self, output_dir: Path, game: str):
        output_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).date().isoformat()
        game_key = "_".join(requested_pair(game))
        self.path = output_dir / f"kalshi_ws_{day}_{game_key}.jsonl"
        self.handle = self.path.open("a", encoding="utf-8", buffering=1)

    def write(self, record: dict) -> None:
        self.handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")

    def status(self, status: str, **details) -> None:
        self.write({"record_type": "collector_status", "received_at": utc_now(), "status": status, **details})

    def close(self) -> None:
        self.handle.close()


def websocket_record(raw_text: str, connection_id: str) -> dict:
    received_at = utc_now()
    try:
        raw = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        return {
            "record_type": "malformed_ws_message",
            "received_at": received_at,
            "connection_id": connection_id,
            "error": str(exc),
            "raw_text": raw_text,
        }
    msg = raw.get("msg") if isinstance(raw.get("msg"), dict) else {}
    return {
        "record_type": "ws_message",
        "received_at": received_at,
        "connection_id": connection_id,
        "message_type": raw.get("type"),
        "market_ticker": msg.get("market_ticker") or msg.get("ticker"),
        "exchange_timestamp": msg.get("time") or msg.get("ts"),
        "exchange_ts_ms": msg.get("ts_ms"),
        "sid": raw.get("sid") or msg.get("sid"),
        "seq": raw.get("seq"),
        "raw": raw,
    }


async def subscribe(ws, tickers: list[str], channels: list[str], writer: JsonlWriter) -> None:
    for command_id, channel in enumerate(channels, start=1):
        command = {
            "id": command_id,
            "cmd": "subscribe",
            "params": {"channels": [channel], "market_tickers": tickers},
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
                while not STOP:
                    if args.duration and time.monotonic() - started >= args.duration:
                        writer.status("stopped", reason="duration_reached")
                        return
                    try:
                        raw_text = await asyncio.wait_for(ws.recv(), timeout=1)
                    except asyncio.TimeoutError:
                        continue
                    record = websocket_record(raw_text, connection_id)
                    sid, seq = record.get("sid"), record.get("seq")
                    if isinstance(sid, int) and isinstance(seq, int):
                        previous = last_sequence.get(sid)
                        if previous is not None and seq != previous + 1:
                            writer.status(
                                "sequence_gap",
                                connection_id=connection_id,
                                sid=sid,
                                expected_seq=previous + 1,
                                received_seq=seq,
                                market_ticker=record.get("market_ticker"),
                            )
                        last_sequence[sid] = seq
                    writer.write(record)
                    if record.get("message_type") in {"subscribed", "error"}:
                        print(json.dumps(record["raw"]), flush=True)
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
    args = parser.parse_args()
    args.channels = [item.strip() for item in args.channels.split(",") if item.strip()]
    unknown = set(args.channels) - set(CHANNELS)
    if unknown:
        parser.error(f"unsupported channels: {', '.join(sorted(unknown))}")
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

    writer = JsonlWriter(args.output_dir, args.game)
    writer.write(
        {
            "record_type": "market_discovery",
            "received_at": utc_now(),
            "game": args.game,
            "game_date": args.date,
            "channels": args.channels,
            "markets": markets,
        }
    )
    print(f"Saving raw events to {writer.path}", flush=True)
    try:
        asyncio.run(collect(args, markets, writer))
    finally:
        writer.close()


if __name__ == "__main__":
    main()
