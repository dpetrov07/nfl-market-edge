"""Capture one prospective NFL/CFB combo slate and its standalone leg books."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets

from collect_live_kalshi_ws import (
    MarketState,
    WS_URL,
    auth_headers,
    load_local_env,
    load_private_key,
    utc_now,
)
from discover_sunday_kalshi_combos import API_ROOT, KalshiClient


MARKET_CHANNELS = ("orderbook_delta", "trade", "ticker")
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


def read_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text())
    missing = [key for key in ("slate_id", "league", "date", "events") if not manifest.get(key)]
    if missing:
        raise ValueError(f"manifest is missing: {', '.join(missing)}")
    if manifest["league"].lower() not in {"nfl", "cfb"}:
        raise ValueError("league must be nfl or cfb")
    events = {}
    for value in manifest["events"]:
        if isinstance(value, str):
            events[value] = value
        else:
            events[value["ticker"]] = value.get("game") or value["ticker"]
    manifest["event_games"] = events
    return manifest


def selected_legs(row: dict) -> list[dict]:
    return row.get("mve_selected_legs") or row.get("selected_markets") or []


def is_slate_combo(row: dict, event_tickers: set[str]) -> bool:
    legs = selected_legs(row)
    return len(legs) in (2, 3) and all(leg.get("event_ticker") in event_tickers for leg in legs)


class Writer:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.handle = gzip.open(path, "at", encoding="utf-8", compresslevel=6)

    def write(self, record: dict) -> None:
        self.handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")

    def status(self, status: str, **details) -> None:
        self.write({"record_type": "collector_status", "received_at": utc_now(), "status": status, **details})
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()


class SlateRegistry:
    def __init__(self, manifest: dict, writer: Writer):
        self.manifest = manifest
        self.writer = writer
        self.event_tickers = set(manifest["event_games"])
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
        if not is_slate_combo(row, self.event_tickers):
            return []
        ticker = row.get("ticker") or row.get("market_ticker")
        if not ticker:
            return []
        legs = selected_legs(row)
        before = self.tickers
        existing = self.combos.get(ticker, {})
        self.combos[ticker] = {
            **existing,
            "ticker": ticker,
            "event_ticker": row.get("event_ticker") or existing.get("event_ticker"),
            "mve_collection_ticker": row.get("mve_collection_ticker")
            or row.get("collection_ticker")
            or existing.get("mve_collection_ticker"),
            "mve_selected_legs": legs,
            **compact_market(row),
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
    markets, _, _ = client.paginate(
        API_ROOT + "/markets",
        {"mve_filter": "only", "status": "open", "limit": 1000},
        "markets",
        row_filter=lambda row: is_slate_combo(row, registry.event_tickers),
    )
    return markets


def open_rfqs(client: KalshiClient, registry: SlateRegistry) -> list[dict]:
    rows, _, _ = client.paginate(
        API_ROOT + "/communications/rfqs",
        {"status": "open", "limit": 100},
        "rfqs",
        auth=True,
        row_filter=lambda row: is_slate_combo(row, registry.event_tickers),
    )
    return rows


def open_quotes(client: KalshiClient, registry: SlateRegistry) -> list[dict]:
    rows, _, _ = client.paginate(
        API_ROOT + "/communications/quotes",
        {"status": "open", "limit": 500},
        "quotes",
        auth=True,
        row_filter=lambda row: row.get("market_ticker") in registry.combos,
    )
    return rows


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


async def subscribe_markets(ws, command_id: int, tickers: list[str]) -> int:
    for index in range(0, len(tickers), 200):
        chunk = tickers[index : index + 200]
        for channel in MARKET_CHANNELS:
            command_id = await subscribe(ws, command_id, channel, chunk)
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

    writer.write(
        {
            "record_type": "slate_manifest",
            "received_at": utc_now(),
            "schema_version": 1,
            **{key: value for key, value in manifest.items() if key != "event_games"},
        }
    )
    if not args.skip_initial_scan:
        for row in await asyncio.to_thread(open_combo_markets, client, registry):
            registry.add_combo(row, "initial_open_market_scan")
    for row in await asyncio.to_thread(open_rfqs, client, registry):
        registry.add_combo({**row, "ticker": row.get("market_ticker")}, "initial_open_rfq_scan")
        writer.write(
            {
                "record_type": "communication",
                "received_at": utc_now(),
                "communication_type": "rfq_snapshot",
                "market_ticker": row.get("market_ticker"),
                "rfq_id": row.get("id"),
                "exchange_timestamp": row.get("updated_ts") or row.get("created_ts"),
                "contracts": row.get("contracts_fp"),
                "yes_contracts": row.get("yes_contracts_fp"),
                "no_contracts": row.get("no_contracts_fp"),
                "target_cost_dollars": row.get("target_cost_dollars"),
                "status": row.get("status"),
                "payload": row,
            }
        )
    for row in await asyncio.to_thread(open_quotes, client, registry):
        writer.write(
            {
                "record_type": "communication",
                "received_at": utc_now(),
                "communication_type": "quote_snapshot",
                "market_ticker": row.get("market_ticker"),
                "rfq_id": row.get("rfq_id"),
                "quote_id": row.get("id"),
                "exchange_timestamp": row.get("updated_ts") or row.get("created_ts"),
                "contracts": row.get("contracts_fp"),
                "yes_contracts": row.get("yes_contracts_fp"),
                "no_contracts": row.get("no_contracts_fp"),
                "yes_bid_dollars": row.get("yes_bid_dollars"),
                "no_bid_dollars": row.get("no_bid_dollars"),
                "target_cost_dollars": row.get("rfq_target_cost_dollars"),
                "accepted_side": row.get("accepted_side"),
                "status": row.get("status"),
                "payload": row,
            }
        )
    registry.add_component_metadata(await asyncio.to_thread(market_metadata, client, registry.components))

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
                    command_id = await subscribe_markets(
                        ws, command_id, sorted(registry.tickers)
                    )
                state = MarketState(sorted(registry.tickers), args.top_size_change)
                last_sequence, last_heartbeat = {}, 0.0
                messages = 0
                backoff = 1
                while not STOP and (not args.duration or time.monotonic() - started < args.duration):
                    now = time.monotonic()
                    if now - last_heartbeat >= args.heartbeat_seconds:
                        writer.status(
                            "heartbeat",
                            connection_id=connection_id,
                            messages_received=messages,
                            combo_count=len(registry.combos),
                            component_count=len(registry.components),
                            books_initialized=len(state.initialized),
                        )
                        last_heartbeat = now
                    try:
                        raw = json.loads(await asyncio.wait_for(ws.recv(), timeout=1))
                    except asyncio.TimeoutError:
                        continue
                    received_at = utc_now()
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
                        if message_type == "rfq_created" and is_slate_combo(msg, registry.event_tickers):
                            new_tickers = registry.add_combo(
                                {**msg, "ticker": msg.get("market_ticker")}, "communications"
                            )
                            if new_tickers:
                                state.add_tickers(new_tickers)
                                command_id = await subscribe_markets(
                                    ws, command_id, new_tickers
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
                            command_id = await subscribe_markets(
                                ws, command_id, new_tickers
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("data/live/combo_slates"))
    parser.add_argument("--key-id")
    parser.add_argument("--private-key-path")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--skip-initial-scan", action="store_true")
    parser.add_argument("--heartbeat-seconds", type=float, default=5)
    parser.add_argument("--top-size-change", type=float, default=0)
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
    path = args.output_dir / manifest["slate_id"] / "kalshi.jsonl.gz"
    writer = Writer(path)
    print(f"capturing {manifest['slate_id']} -> {path}", flush=True)
    try:
        asyncio.run(collect(args, manifest, writer))
    finally:
        writer.close()


if __name__ == "__main__":
    main()
