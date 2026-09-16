"""Capture one prospective NFL/CFB combo slate and its standalone leg books."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import websockets

from nfl_market_edge.kalshi import (
    API_ROOT,
    KalshiClient,
    MarketState,
    WS_URL,
    auth_headers,
    dollar_value,
    load_local_env,
    load_private_key,
    number,
    utc_now,
)
from nfl_market_edge.health import emit_health, health_record


BOOK_CHANNELS = ("orderbook_delta",)
COMBO_CHANNELS = ("trade", "ticker")
GLOBAL_CHANNELS = ("communications", "market_lifecycle_v2", "multivariate_market_lifecycle")
COMMUNICATION_TYPES = {
    "rfq_created",
    "rfq_deleted",
    "quote_created",
    "quote_accepted",
    "quote_executed",
}
STOP = False


def compact_market(market: dict) -> dict:
    fields = (
        "ticker", "market_ticker", "market_id", "event_ticker", "title", "subtitle",
        "yes_sub_title", "no_sub_title", "status", "created_time", "open_time",
        "close_time", "expected_expiration_time", "settlement_ts", "result",
        "settlement_value_dollars", "mve_collection_ticker", "mve_selected_legs",
        "floor_strike", "cap_strike", "custom_strike", "strike_type",
    )
    return {key: market.get(key) for key in fields if market.get(key) is not None}


def event_key(ticker: str | None) -> str | None:
    """Collapse Kalshi's per-series event tickers to the dated game suffix."""
    if not ticker or "-" not in ticker:
        return None
    return ticker.split("-", 1)[1]


def read_manifest(path: Path | None) -> dict:
    raw = os.getenv("SLATE_MANIFEST_JSON")
    if path:
        manifest = json.loads(path.read_text())
    elif raw:
        manifest = json.loads(raw)
    else:
        raise ValueError("set --manifest or SLATE_MANIFEST_JSON")
    missing = [key for key in ("slate_id", "league", "date", "events") if not manifest.get(key)]
    if missing:
        raise ValueError(f"manifest is missing: {', '.join(missing)}")
    if manifest["league"].lower() not in {"nfl", "cfb"}:
        raise ValueError("league must be nfl or cfb")
    events = {}
    for value in manifest["events"]:
        if isinstance(value, str):
            ticker, game = value, value
        else:
            ticker = value["ticker"]
            game = value.get("game") or ticker
        key = event_key(ticker)
        if not key:
            raise ValueError(f"invalid Kalshi event ticker: {ticker!r}")
        if key in events and events[key] != game:
            raise ValueError(f"conflicting game names for event suffix {key}")
        events[key] = game
    manifest["event_games"] = events
    return manifest


def selected_legs(row: dict) -> list[dict]:
    return row.get("mve_selected_legs") or row.get("selected_markets") or []


def is_slate_combo(row: dict, event_games: dict[str, str]) -> bool:
    legs = selected_legs(row)
    games = [event_games.get(event_key(leg.get("event_ticker"))) for leg in legs]
    return (
        len(legs) in (2, 3)
        and all(games)
        and len(set(games)) > 1
    )


class Writer:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.health_path = path.parent / "health.json"
        self.handle = gzip.open(path, "at", encoding="utf-8", compresslevel=6)

    def write(self, record: dict) -> None:
        self.handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")

    def status(self, status: str, **details) -> None:
        record = health_record("kalshi", status, **details)
        self.write(record)
        self.handle.flush()
        emit_health(record, self.health_path)

    def close(self) -> None:
        self.handle.close()


class SlateRegistry:
    def __init__(self, manifest: dict, writer: Writer):
        self.manifest = manifest
        self.writer = writer
        self.event_games = manifest["event_games"]
        self.combos: dict[str, dict] = {}
        self.components: set[str] = set()
        self.component_metadata: set[str] = set()

    @property
    def tickers(self) -> set[str]:
        return set(self.combos) | self.components

    def role(self, ticker: str | None) -> str | None:
        if ticker in self.combos:
            return "combo"
        if ticker in self.components:
            return "component"
        return None

    def add_combo(self, row: dict, source: str) -> list[str]:
        if not is_slate_combo(row, self.event_games):
            return []
        ticker = row.get("ticker") or row.get("market_ticker")
        if not ticker:
            return []
        legs = [
            {
                **leg,
                "game": self.event_games[event_key(leg.get("event_ticker"))],
            }
            for leg in selected_legs(row)
        ]
        before = self.tickers
        existing = self.combos.get(ticker, {})
        self.combos[ticker] = {
            **existing,
            **compact_market(row),
            "ticker": ticker,
            "event_ticker": row.get("event_ticker") or existing.get("event_ticker"),
            "mve_collection_ticker": row.get("mve_collection_ticker")
            or row.get("collection_ticker")
            or existing.get("mve_collection_ticker"),
            "mve_selected_legs": legs,
        }
        self.components.update(leg["market_ticker"] for leg in legs)
        self.writer.write(
            {
                "record_type": "combo_discovery",
                "received_at": utc_now(),
                "source": source,
                "slate_id": self.manifest["slate_id"],
                "combo": self.combos[ticker],
            }
        )
        return sorted(self.tickers - before)

    def add_component_metadata(self, markets: list[dict]) -> None:
        fresh = [compact_market(row) for row in markets if row.get("ticker") not in self.component_metadata]
        if not fresh:
            return
        self.component_metadata.update(row["ticker"] for row in fresh)
        self.writer.write(
            {
                "record_type": "component_discovery",
                "received_at": utc_now(),
                "slate_id": self.manifest["slate_id"],
                "markets": fresh,
            }
        )


def open_combo_markets(client: KalshiClient, registry: SlateRegistry) -> list[dict]:
    # MVE markets are created around RFQ activity. A short lookback recovers
    # recent markets after a restart without exhaustively crawling all open MVEs.
    recent = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
    markets, _, _ = client.paginate(
        API_ROOT + "/markets",
        {
            "mve_filter": "only",
            "status": "open",
            "min_created_ts": recent,
            "limit": 1000,
        },
        "markets",
        row_filter=lambda row: is_slate_combo(row, registry.event_games),
        max_pages=2,
    )
    return markets


def open_rfqs(client: KalshiClient, registry: SlateRegistry) -> list[dict]:
    rows, _, _ = client.paginate(
        API_ROOT + "/communications/rfqs",
        {"status": "open", "limit": 100},
        "rfqs",
        auth=True,
        row_filter=lambda row: is_slate_combo(row, registry.event_games),
        max_pages=2,
    )
    return rows


def open_quotes(client: KalshiClient, registry: SlateRegistry) -> list[dict]:
    rows, _, _ = client.paginate(
        API_ROOT + "/communications/quotes",
        {"status": "open", "limit": 500},
        "quotes",
        auth=True,
        row_filter=lambda row: row.get("market_ticker") in registry.combos,
        max_pages=2,
    )
    return rows


def recent_fills(client: KalshiClient, min_ts: int) -> list[dict]:
    rows, _, _ = client.paginate(
        API_ROOT + "/portfolio/fills",
        {"min_ts": min_ts, "limit": 1000},
        "fills",
        auth=True,
        max_pages=2,
    )
    return rows


def fill_timestamp(row: dict) -> int | None:
    value = row.get("ts") or row.get("created_ts") or row.get("created_time")
    try:
        parsed = float(value)
        return int(parsed / 1000 if parsed > 10_000_000_000 else parsed)
    except (TypeError, ValueError):
        pass
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def fill_record(row: dict, slate_id: str) -> dict:
    ticker = row.get("market_ticker") or row.get("ticker")
    count = row.get("count_fp") if row.get("count_fp") is not None else row.get("count")
    return {
        "record_type": "fill",
        "received_at": utc_now(),
        "exchange_timestamp": row.get("created_time") or row.get("ts"),
        "slate_id": slate_id,
        "market_ticker": ticker,
        "trade_id": row.get("trade_id"),
        "fill_id": row.get("fill_id") or row.get("id"),
        "order_id": row.get("order_id"),
        "side": row.get("side"),
        "action": row.get("action"),
        "count": number(count),
        "yes_price_dollars": dollar_value(row, "yes_price_dollars", "yes_price"),
        "no_price_dollars": dollar_value(row, "no_price_dollars", "no_price"),
        "is_taker": row.get("is_taker"),
        "payload": row,
    }


def market_metadata(client: KalshiClient, tickers: set[str]) -> list[dict]:
    rows = []
    for index in range(0, len(tickers), 50):
        chunk = sorted(tickers)[index : index + 50]
        rows.extend(
            client.get(API_ROOT + "/markets", {"tickers": ",".join(chunk), "limit": len(chunk)})
            .get("markets", [])
        )
    return rows


async def subscribe(ws, command_id: int, channel: str, tickers: list[str] | None = None) -> int:
    params = {"channels": [channel]}
    if tickers:
        params["market_tickers"] = tickers
    if channel == "orderbook_delta":
        params["use_yes_price"] = True
    await ws.send(json.dumps({"id": command_id, "cmd": "subscribe", "params": params}))
    return command_id + 1


async def subscribe_channels(
    ws, command_id: int, tickers: list[str], channels: tuple[str, ...]
) -> int:
    for index in range(0, len(tickers), 200):
        chunk = tickers[index : index + 200]
        for channel in channels:
            command_id = await subscribe(ws, command_id, channel, chunk)
    return command_id


async def subscribe_market_data(
    ws, command_id: int, registry: SlateRegistry, tickers: list[str]
) -> int:
    tickers = sorted(set(tickers))
    if tickers:
        command_id = await subscribe_channels(
            ws, command_id, tickers, BOOK_CHANNELS
        )
    combo_tickers = [ticker for ticker in tickers if registry.role(ticker) == "combo"]
    if combo_tickers:
        command_id = await subscribe_channels(
            ws, command_id, combo_tickers, COMBO_CHANNELS
        )
    return command_id


async def fetch_new_market(client: KalshiClient, ticker: str) -> dict:
    """A lifecycle event can arrive briefly before its REST market is readable."""
    for delay in (0, 0.25, 0.75):
        if delay:
            await asyncio.sleep(delay)
        try:
            payload = await asyncio.to_thread(
                client.get, API_ROOT + f"/markets/{ticker}"
            )
            return payload.get("market", {})
        except Exception:
            pass
    return {}


def communication_record(raw: dict, received_at: str) -> dict:
    msg = raw.get("msg") if isinstance(raw.get("msg"), dict) else {}
    return {
        "record_type": "communication",
        "received_at": received_at,
        "communication_type": raw.get("type"),
        "market_ticker": msg.get("market_ticker"),
        "rfq_id": msg.get("rfq_id") or (msg.get("id") if raw.get("type", "").startswith("rfq_") else None),
        "quote_id": msg.get("quote_id") or (msg.get("id") if raw.get("type", "").startswith("quote_") else None),
        "exchange_timestamp": msg.get("created_ts") or msg.get("updated_ts") or msg.get("executed_ts") or msg.get("deleted_ts"),
        "contracts": msg.get("contracts_fp") or msg.get("contracts_accepted_fp"),
        "yes_contracts": msg.get("yes_contracts_offered_fp") or msg.get("yes_contracts_fp"),
        "no_contracts": msg.get("no_contracts_offered_fp") or msg.get("no_contracts_fp"),
        "contracts_accepted": msg.get("contracts_accepted_fp"),
        "yes_bid_dollars": msg.get("yes_bid_dollars"),
        "no_bid_dollars": msg.get("no_bid_dollars"),
        "target_cost_dollars": msg.get("target_cost_dollars") or msg.get("rfq_target_cost_dollars"),
        "accepted_side": msg.get("accepted_side"),
        "status": msg.get("status"),
        "sid": raw.get("sid"),
        "seq": raw.get("seq"),
        "payload": msg,
    }


async def collect(args: argparse.Namespace, manifest: dict, writer: Writer) -> None:
    key_id = args.key_id or os.getenv("KALSHI_API_KEY_ID")
    if not key_id:
        raise SystemExit("set KALSHI_API_KEY_ID")
    private_key = load_private_key(args)
    client = KalshiClient(authenticated=True)
    client.key_id = key_id
    client.private_key = private_key
    registry = SlateRegistry(manifest, writer)
    fill_since = int(time.time()) - 300
    seen_fills: set[str] = set()
    fills_received = 0
    fill_api_available = True

    async def poll_fills() -> None:
        nonlocal fill_since, fills_received, fill_api_available
        if not fill_api_available:
            return
        rows = await asyncio.to_thread(recent_fills, client, fill_since)
        fill_api_available = API_ROOT + "/portfolio/fills" not in client.denied_paths
        newest = fill_since
        for row in rows:
            timestamp = fill_timestamp(row)
            if timestamp is not None:
                newest = max(newest, timestamp)
            ticker = row.get("market_ticker") or row.get("ticker")
            identity = str(
                row.get("fill_id")
                or row.get("id")
                or (row.get("trade_id"), row.get("order_id"), row.get("ts"))
            )
            if ticker not in registry.combos or identity in seen_fills:
                continue
            seen_fills.add(identity)
            writer.write(fill_record(row, manifest["slate_id"]))
            fills_received += 1
        fill_since = max(fill_since, newest - 1)

    writer.write(
        {
            "record_type": "slate_manifest",
            "received_at": utc_now(),
            "schema_version": 1,
            **{key: value for key, value in manifest.items() if key != "event_games"},
        }
    )
    writer.status(
        "starting",
        slate_id=manifest["slate_id"],
        league=manifest["league"],
        game_count=len(manifest["event_games"]),
    )
    setup_backoff = 1
    while not STOP:
        try:
            seed_tickers = set(manifest.get("combo_tickers", []))
            if seed_tickers:
                for row in await asyncio.to_thread(
                    market_metadata, client, seed_tickers
                ):
                    registry.add_combo(row, "manifest_seed")
            if not args.skip_initial_scan:
                markets = await asyncio.to_thread(
                    open_combo_markets, client, registry
                )
                for row in markets:
                    registry.add_combo(row, "initial_open_market_scan")
            rfqs = await asyncio.to_thread(open_rfqs, client, registry)
            for row in rfqs:
                registry.add_combo(
                    {**row, "ticker": row.get("market_ticker")},
                    "initial_open_rfq_scan",
                )
                writer.write(
                    {
                        "record_type": "communication",
                        "received_at": utc_now(),
                        "communication_type": "rfq_snapshot",
                        "market_ticker": row.get("market_ticker"),
                        "rfq_id": row.get("id"),
                        "exchange_timestamp": row.get("updated_ts")
                        or row.get("created_ts"),
                        "contracts": row.get("contracts_fp"),
                        "yes_contracts": row.get("yes_contracts_fp"),
                        "no_contracts": row.get("no_contracts_fp"),
                        "target_cost_dollars": row.get("target_cost_dollars"),
                        "status": row.get("status"),
                        "payload": row,
                    }
                )
            quotes = await asyncio.to_thread(open_quotes, client, registry)
            for row in quotes:
                writer.write(
                    {
                        "record_type": "communication",
                        "received_at": utc_now(),
                        "communication_type": "quote_snapshot",
                        "market_ticker": row.get("market_ticker"),
                        "rfq_id": row.get("rfq_id"),
                        "quote_id": row.get("id"),
                        "exchange_timestamp": row.get("updated_ts")
                        or row.get("created_ts"),
                        "contracts": row.get("contracts_fp"),
                        "yes_contracts": row.get("yes_contracts_fp"),
                        "no_contracts": row.get("no_contracts_fp"),
                        "yes_bid_dollars": row.get("yes_bid_dollars"),
                        "no_bid_dollars": row.get("no_bid_dollars"),
                        "target_cost_dollars": row.get(
                            "rfq_target_cost_dollars"
                        ),
                        "accepted_side": row.get("accepted_side"),
                        "status": row.get("status"),
                        "payload": row,
                    }
                )
            registry.add_component_metadata(
                await asyncio.to_thread(
                    market_metadata, client, registry.components
                )
            )
            await poll_fills()
            writer.status(
                "ready",
                slate_id=manifest["slate_id"],
                combo_count=len(registry.combos),
                component_count=len(registry.components),
                open_rfq_count=len(rfqs),
                quote_api_available=(
                    API_ROOT + "/communications/quotes"
                    not in client.denied_paths
                ),
                fill_api_available=fill_api_available,
                fills_received=fills_received,
            )
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            writer.status(
                "setup_error",
                error=f"{type(exc).__name__}: {exc}",
                retry_in_seconds=setup_backoff,
            )
            await asyncio.sleep(setup_backoff)
            setup_backoff = min(setup_backoff * 2, 30)

    if STOP:
        writer.status("stopped", reason="signal_during_setup")
        return

    started = time.monotonic()
    attempt, backoff = 0, 1
    while not STOP and (not args.duration or time.monotonic() - started < args.duration):
        attempt += 1
        connection_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}-{attempt}"
        try:
            writer.status("connecting", connection_id=connection_id, market_count=len(registry.tickers))
            async with websockets.connect(
                WS_URL,
                additional_headers=auth_headers(key_id, private_key),
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
                max_queue=10000,
            ) as ws:
                writer.status("connected", connection_id=connection_id)
                command_id = 1
                for channel in GLOBAL_CHANNELS:
                    command_id = await subscribe(ws, command_id, channel)
                if registry.tickers:
                    command_id = await subscribe_market_data(
                        ws, command_id, registry, sorted(registry.tickers)
                    )
                state = MarketState(sorted(registry.tickers), args.top_size_change)
                last_sequence, last_heartbeat = {}, 0.0
                messages, last_message_at = 0, None
                backoff = 1
                while not STOP and (not args.duration or time.monotonic() - started < args.duration):
                    now = time.monotonic()
                    if now - last_heartbeat >= args.heartbeat_seconds:
                        await poll_fills()
                        writer.status(
                            "heartbeat",
                            connection_id=connection_id,
                            messages_received=messages,
                            combo_count=len(registry.combos),
                            component_count=len(registry.components),
                            books_initialized=len(state.initialized),
                            fills_received=fills_received,
                            fill_api_available=fill_api_available,
                            last_message_at=last_message_at,
                        )
                        last_heartbeat = now
                    try:
                        raw = json.loads(await asyncio.wait_for(ws.recv(), timeout=1))
                    except asyncio.TimeoutError:
                        continue
                    received_at = utc_now()
                    last_message_at = received_at
                    messages += 1
                    msg = raw.get("msg") if isinstance(raw.get("msg"), dict) else {}
                    sid, seq = raw.get("sid") or msg.get("sid"), raw.get("seq")
                    if isinstance(sid, int) and isinstance(seq, int):
                        previous = last_sequence.get(sid)
                        if previous is not None and seq != previous + 1:
                            writer.status("sequence_gap", sid=sid, expected_seq=previous + 1, received_seq=seq)
                            raise RuntimeError("WebSocket sequence gap")
                        last_sequence[sid] = seq

                    message_type = raw.get("type")
                    if message_type in COMMUNICATION_TYPES:
                        if message_type == "rfq_created" and is_slate_combo(
                            msg, registry.event_games
                        ):
                            new_tickers = registry.add_combo(
                                {**msg, "ticker": msg.get("market_ticker")}, "communications"
                            )
                            if new_tickers:
                                state.add_tickers(new_tickers)
                                command_id = await subscribe_market_data(
                                    ws, command_id, registry, new_tickers
                                )
                                metadata = await asyncio.to_thread(
                                    market_metadata, client, set(new_tickers) & registry.components
                                )
                                registry.add_component_metadata(metadata)
                        if registry.role(msg.get("market_ticker")) == "combo":
                            record = communication_record(raw, received_at)
                            record["connection_id"] = connection_id
                            writer.write(record)
                        continue

                    ticker = msg.get("market_ticker") or msg.get("ticker")
                    if (
                        msg.get("event_type") == "created"
                        and ticker
                        and ticker not in registry.combos
                        and str(ticker).startswith("KXMVE")
                        and message_type in {
                            "market_lifecycle_v2", "multivariate_market_lifecycle"
                        }
                    ):
                        market = await fetch_new_market(client, ticker)
                        new_tickers = registry.add_combo(market, "multivariate_lifecycle")
                        if new_tickers:
                            state.add_tickers(new_tickers)
                            command_id = await subscribe_market_data(
                                ws, command_id, registry, new_tickers
                            )
                            metadata = await asyncio.to_thread(
                                market_metadata, client, set(new_tickers) & registry.components
                            )
                            registry.add_component_metadata(metadata)
                    for record in state.process(raw, received_at):
                        record["connection_id"] = connection_id
                        record["market_role"] = registry.role(record.get("market_ticker"))
                        writer.write(record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            writer.status(
                "disconnected",
                connection_id=connection_id,
                error=f"{type(exc).__name__}: {exc}",
                reconnect_in_seconds=backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
    writer.status("stopped")


def parse_args() -> argparse.Namespace:
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    default_output = os.getenv("COLLECTOR_OUTPUT_ROOT") or (
        str(Path(volume) / "combo_slates")
        if volume
        else "data/live/combo_slates"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(default_output))
    parser.add_argument("--key-id")
    parser.add_argument("--private-key-path")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--skip-initial-scan", action="store_true")
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=float(os.getenv("COLLECTOR_HEARTBEAT_SECONDS", "15")),
    )
    parser.add_argument(
        "--top-size-change",
        type=float,
        default=float(os.getenv("KALSHI_TOP_SIZE_CHANGE", "0")),
    )
    return parser.parse_args()


def request_stop(_signum, _frame) -> None:
    global STOP
    STOP = True


def main() -> None:
    load_local_env()
    args = parse_args()
    manifest = read_manifest(args.manifest)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    path = args.output_dir / manifest["slate_id"] / "kalshi" / "events.jsonl.gz"
    writer = Writer(path)
    print(f"capturing {manifest['slate_id']} -> {path}", flush=True)
    try:
        asyncio.run(collect(args, manifest, writer))
    finally:
        writer.close()


if __name__ == "__main__":
    main()
