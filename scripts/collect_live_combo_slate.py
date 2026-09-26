"""Capture one prospective NFL/CFB combo slate and its standalone leg books."""

from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict
from contextlib import suppress
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
    RecentSet,
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
GLOBAL_CHANNELS = ("market_lifecycle_v2", "multivariate_market_lifecycle")
COMMUNICATION_TYPES = {
    "rfq_created",
    "rfq_deleted",
    "quote_created",
    "quote_accepted",
    "quote_executed",
}
STOP = False


def directory_size(path: Path) -> int:
    total = 0
    try:
        for root, _, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def compact_market(market: dict) -> dict:
    fields = (
        "ticker", "market_ticker", "market_id", "event_ticker", "title", "subtitle",
        "yes_sub_title", "no_sub_title", "status", "created_time", "open_time",
        "close_time", "expected_expiration_time", "settlement_ts", "result",
        "settlement_value_dollars", "mve_collection_ticker", "mve_selected_legs",
        "floor_strike", "cap_strike", "custom_strike", "strike_type",
        "prop_type", "player", "player_id", "threshold", "occurrence_datetime",
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
    if manifest.get("combo_scope", "cross_game") not in {
        "cross_game", "same_game", "any"
    }:
        raise ValueError("combo_scope must be cross_game, same_game, or any")
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


def is_slate_combo(
    row: dict, event_games: dict[str, str], scope: str = "cross_game"
) -> bool:
    legs = selected_legs(row)
    games = [event_games.get(event_key(leg.get("event_ticker"))) for leg in legs]
    if len(legs) not in (2, 3) or not all(games):
        return False
    distinct_games = len(set(games))
    return (
        scope == "any"
        or (scope == "same_game" and distinct_games == 1)
        or (scope == "cross_game" and distinct_games > 1)
    )


class Writer:
    def __init__(
        self,
        path: Path,
        *,
        gzip_member_bytes: int = 128 * 1024 * 1024,
        gzip_member_seconds: float = 3600,
        volume_capacity_bytes: int = 5_000_000_000,
        archive_retention_bytes: int = 2_600_000_000,
        target_storage_bytes: int = 2_750_000_000,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.health_path = path.parent / "health.json"
        self.gzip_member_bytes = gzip_member_bytes
        self.gzip_member_seconds = gzip_member_seconds
        self.volume_capacity_bytes = volume_capacity_bytes
        self.archive_retention_bytes = archive_retention_bytes
        self.target_storage_bytes = target_storage_bytes
        self.storage_root = Path(
            os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or path.parent
        )
        self.archives_created = 0
        self.archives_pruned = 0
        self.archive_bytes_pruned = 0
        if path.exists() and path.stat().st_size:
            path.replace(self._archive_path())
            self.archives_created += 1
        self._prune_archives()
        self.started = time.monotonic()
        self.initial_collector_bytes = self._collector_bytes()
        self.initial_volume_bytes = directory_size(self.storage_root)
        self.member_started = self.started
        self.gzip_members_opened = 1
        self.records_written = 0
        self.handle = gzip.open(path, "wt", encoding="utf-8", compresslevel=6)

    def _archive_path(self) -> Path:
        stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}"
        base = self.path.name.removesuffix(".jsonl.gz")
        return self.path.with_name(f"{base}.{stamp}.jsonl.gz")

    def _archives(self) -> list[Path]:
        base = self.path.name.removesuffix(".jsonl.gz")
        return sorted(self.path.parent.glob(f"{base}.*.jsonl.gz"))

    def _collector_bytes(self) -> int:
        paths = self._archives()
        if self.path.exists():
            paths.append(self.path)
        return sum(path.stat().st_size for path in paths if path.exists())

    def _prune_archives(self) -> None:
        archives = self._archives()
        total = sum(path.stat().st_size for path in archives)
        for path in archives:
            if total <= self.archive_retention_bytes:
                break
            size = path.stat().st_size
            path.unlink()
            total -= size
            self.archives_pruned += 1
            self.archive_bytes_pruned += size

    def write(self, record: dict, *, flush: bool = False) -> None:
        self.handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        self.records_written += 1
        if flush:
            self.handle.flush()
            self._rotate_member_if_needed()

    def _rotate_member_if_needed(self) -> None:
        size = self.path.stat().st_size if self.path.exists() else 0
        member_age = time.monotonic() - self.member_started
        if (
            size < self.gzip_member_bytes
            and member_age < self.gzip_member_seconds
        ):
            return
        self.handle.close()
        if self.path.exists() and self.path.stat().st_size:
            self.path.replace(self._archive_path())
            self.archives_created += 1
            self._prune_archives()
        self.member_started = time.monotonic()
        self.gzip_members_opened += 1
        self.handle = gzip.open(
            self.path, "wt", encoding="utf-8", compresslevel=6
        )

    def storage_metrics(self) -> dict:
        self.handle.flush()
        output_bytes = self.path.stat().st_size if self.path.exists() else 0
        collector_bytes = self._collector_bytes()
        volume_bytes = directory_size(self.storage_root)
        elapsed_hours = max((time.monotonic() - self.started) / 3600, 1 / 3600)
        bytes_written = max(
            collector_bytes - self.initial_collector_bytes + self.archive_bytes_pruned,
            0,
        )
        bytes_per_hour = bytes_written / elapsed_hours
        remaining = max(self.volume_capacity_bytes - volume_bytes, 0)
        hours_to_capacity = remaining / bytes_per_hour if bytes_per_hour else None
        utilization = (
            volume_bytes / self.volume_capacity_bytes
            if self.volume_capacity_bytes
            else None
        )
        non_collector_bytes = max(volume_bytes - collector_bytes, 0)
        estimated_max_volume_bytes = (
            non_collector_bytes
            + self.archive_retention_bytes
            + self.gzip_member_bytes
        )
        retention_protected = (
            estimated_max_volume_bytes <= self.volume_capacity_bytes
        )
        projected_24h_collector_bytes = round(
            collector_bytes + bytes_per_hour * 24
        )
        projection_warning = (
            projected_24h_collector_bytes > self.target_storage_bytes
        )
        return {
            "output_bytes": output_bytes,
            "collector_bytes": collector_bytes,
            "bytes_written": bytes_written,
            "records_written": self.records_written,
            "bytes_per_hour": round(bytes_per_hour, 1),
            "estimated_24h_growth_bytes": round(bytes_per_hour * 24),
            "projected_24h_collector_bytes": projected_24h_collector_bytes,
            "volume_bytes_used": volume_bytes,
            "volume_capacity_bytes": self.volume_capacity_bytes,
            "volume_utilization": round(utilization, 4)
            if utilization is not None
            else None,
            "estimated_hours_to_capacity": round(hours_to_capacity, 1)
            if hours_to_capacity is not None
            else None,
            "storage_warning": bool(
                utilization is not None
                and (
                    utilization >= 0.8
                    or not retention_protected
                    or projection_warning
                )
            ),
            "target_storage_bytes": self.target_storage_bytes,
            "projection_warning": projection_warning,
            "archive_retention_bytes": self.archive_retention_bytes,
            "estimated_max_volume_bytes": estimated_max_volume_bytes,
            "retention_protected": retention_protected,
            "gzip_members_opened": self.gzip_members_opened,
            "archives_created": self.archives_created,
            "archives_pruned": self.archives_pruned,
            "archive_bytes_pruned": self.archive_bytes_pruned,
        }

    def status(self, status: str, **details) -> None:
        record = health_record(
            "kalshi", status, **self.storage_metrics(), **details
        )
        self.write(record, flush=True)
        emit_health(record, self.health_path)

    def close(self) -> None:
        self.handle.close()


class SlateRegistry:
    def __init__(
        self,
        manifest: dict,
        writer: Writer,
        *,
        max_combos: int = 10_000,
        max_components: int = 10_000,
        combo_idle_seconds: float = 3600,
    ):
        if max_combos < 1 or max_components < 1:
            raise ValueError("registry limits must be positive")
        if combo_idle_seconds <= 0:
            raise ValueError("combo_idle_seconds must be positive")
        self.manifest = manifest
        self.writer = writer
        self.event_games = manifest["event_games"]
        self.combo_scope = manifest.get("combo_scope", "cross_game")
        self.max_combos = max_combos
        self.max_components = max_components
        self.combo_idle_seconds = combo_idle_seconds
        self.combos: OrderedDict[str, dict] = OrderedDict()
        self.combo_last_seen: dict[str, float] = {}
        self.components: OrderedDict[str, None] = OrderedDict()
        self.component_metadata = RecentSet(max_components)
        self.evicted_tickers: list[str] = []

    @property
    def tickers(self) -> set[str]:
        return set(self.combos) | set(self.components)

    def role(self, ticker: str | None) -> str | None:
        if ticker in self.combos:
            return "combo"
        if ticker in self.components:
            return "component"
        return None

    def touch_combo(self, ticker: str | None, now: float | None = None) -> None:
        if ticker in self.combos:
            self.combos.move_to_end(ticker)
            self.combo_last_seen[ticker] = time.monotonic() if now is None else now

    def remove_combos(self, tickers) -> None:
        for ticker in tickers:
            if ticker in self.combos:
                self.combos.pop(ticker, None)
                self.combo_last_seen.pop(ticker, None)
                self.evicted_tickers.append(ticker)

    def evict_stale(self, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        stale = [
            ticker
            for ticker, seen_at in self.combo_last_seen.items()
            if now - seen_at >= self.combo_idle_seconds
        ]
        self.remove_combos(stale)
        return stale

    def take_evicted_tickers(self) -> list[str]:
        evicted, self.evicted_tickers = self.evicted_tickers, []
        return evicted

    def add_combo(self, row: dict, source: str) -> list[str]:
        if not is_slate_combo(row, self.event_games, self.combo_scope):
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
        is_new_combo = ticker not in self.combos
        existing = self.combos.get(ticker, {})
        combo = {
            **existing,
            **compact_market(row),
            "ticker": ticker,
            "event_ticker": row.get("event_ticker") or existing.get("event_ticker"),
            "mve_collection_ticker": row.get("mve_collection_ticker")
            or row.get("collection_ticker")
            or existing.get("mve_collection_ticker"),
            "mve_selected_legs": legs,
        }
        self.combos[ticker] = combo
        self.touch_combo(ticker)
        new_tickers = [ticker] if is_new_combo else []
        for leg in legs:
            component = leg["market_ticker"]
            if component not in self.components:
                self.components[component] = None
                new_tickers.append(component)
                if len(self.components) > self.max_components:
                    evicted, _ = self.components.popitem(last=False)
                    self.evicted_tickers.append(evicted)
        if combo != existing:
            self.writer.write(
                {
                    "record_type": "combo_discovery",
                    "received_at": utc_now(),
                    "source": source,
                    "slate_id": self.manifest["slate_id"],
                    "combo": combo,
                }
            )
        while len(self.combos) > self.max_combos:
            oldest = next(iter(self.combos))
            self.remove_combos([oldest])
            if oldest == ticker:
                new_tickers = [value for value in new_tickers if value != ticker]
        return sorted(new_tickers)

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
        row_filter=lambda row: is_slate_combo(
            row, registry.event_games, registry.combo_scope
        ),
        max_pages=int(registry.manifest.get("initial_scan_pages", 5)),
    )
    return markets


def open_rfqs(client: KalshiClient, registry: SlateRegistry) -> list[dict]:
    rows, _, _ = client.paginate(
        API_ROOT + "/communications/rfqs",
        {"status": "open", "limit": 100},
        "rfqs",
        auth=True,
        row_filter=lambda row: is_slate_combo(
            row, registry.event_games, registry.combo_scope
        ),
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
    payload = msg
    if str(raw.get("type", "")).startswith("rfq_"):
        payload = {
            key: msg[key]
            for key in (
                "mve_collection_ticker",
                "mve_selected_legs",
            )
            if msg.get(key) is not None
        }
    exchange_timestamp = (
        msg.get("created_ts")
        or msg.get("updated_ts")
        or msg.get("executed_ts")
        or msg.get("deleted_ts")
    )
    persistence_lag_ms = None
    try:
        exchange_time = datetime.fromisoformat(
            str(exchange_timestamp).replace("Z", "+00:00")
        )
        receive_time = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        persistence_lag_ms = round(
            (receive_time - exchange_time).total_seconds() * 1000, 3
        )
    except (TypeError, ValueError):
        pass
    record = {
        "record_type": "communication",
        "received_at": received_at,
        "communication_type": raw.get("type"),
        "market_ticker": msg.get("market_ticker"),
        "rfq_id": msg.get("rfq_id") or (msg.get("id") if raw.get("type", "").startswith("rfq_") else None),
        "quote_id": msg.get("quote_id") or (msg.get("id") if raw.get("type", "").startswith("quote_") else None),
        "exchange_timestamp": exchange_timestamp,
        "persistence_lag_ms": persistence_lag_ms,
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
        "payload": payload,
    }
    return {
        key: value
        for key, value in record.items()
        if value is not None and value != {}
    }


def communication_identity(raw: dict) -> tuple | None:
    msg = raw.get("msg") if isinstance(raw.get("msg"), dict) else {}
    message_type = raw.get("type")
    object_id = (
        msg.get("rfq_id")
        or msg.get("quote_id")
        or msg.get("id")
    )
    timestamp = (
        msg.get("created_ts")
        or msg.get("updated_ts")
        or msg.get("executed_ts")
        or msg.get("deleted_ts")
    )
    if not message_type or not object_id:
        return None
    return message_type, str(object_id), str(timestamp)


async def collect(args: argparse.Namespace, manifest: dict, writer: Writer) -> None:
    key_id = args.key_id or os.getenv("KALSHI_API_KEY_ID")
    if not key_id:
        raise SystemExit("set KALSHI_API_KEY_ID")
    private_key = load_private_key(args)
    client = KalshiClient(authenticated=True)
    client.key_id = key_id
    client.private_key = private_key
    registry = SlateRegistry(
        manifest,
        writer,
        max_combos=args.max_active_combos,
        max_components=args.state_max_tickers,
        combo_idle_seconds=args.combo_idle_seconds,
    )
    fill_since = int(time.time()) - 300
    seen_fills = RecentSet(args.dedupe_entries)
    fills_received = 0
    fill_api_available = True
    settled_combo_count = 0
    seen_communications = RecentSet(args.dedupe_entries)
    duplicate_communications_skipped = 0
    rfqs_persisted = 0
    last_rfq_at = None
    last_rfq_exchange_at = None
    last_rfq_persistence_lag_ms = None

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
    state = MarketState(
        [],
        args.top_size_change,
        max_tickers=args.state_max_tickers,
        dedupe_entries=args.dedupe_entries,
    )
    state.add_tickers(sorted(registry.components), pinned=True)
    state.add_tickers(sorted(registry.combos))
    market_queue: asyncio.Queue[list[str]] = asyncio.Queue(
        maxsize=args.max_pending_market_batches
    )
    pending_market_batches_dropped = 0
    pending_market_tickers_dropped = 0
    communication_messages_received = 0

    def apply_registry_evictions() -> None:
        state.remove_tickers(registry.take_evicted_tickers())

    def queue_new_markets(tickers: list[str]) -> None:
        nonlocal pending_market_batches_dropped, pending_market_tickers_dropped
        if not tickers:
            return
        try:
            market_queue.put_nowait(tickers)
        except asyncio.QueueFull:
            pending_market_batches_dropped += 1
            pending_market_tickers_dropped += len(tickers)

    async def collect_communications() -> None:
        nonlocal duplicate_communications_skipped
        nonlocal communication_messages_received
        nonlocal rfqs_persisted
        nonlocal last_rfq_at, last_rfq_exchange_at
        nonlocal last_rfq_persistence_lag_ms
        attempt, backoff = 0, 1
        while not STOP and (
            not args.duration or time.monotonic() - started < args.duration
        ):
            attempt += 1
            connection_id = (
                f"communications-{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}"
                f"-{attempt}"
            )
            try:
                writer.status(
                    "communications_connecting", connection_id=connection_id
                )
                async with websockets.connect(
                    WS_URL,
                    additional_headers=auth_headers(key_id, private_key),
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                    max_queue=10000,
                ) as ws:
                    await subscribe(ws, 1, "communications")
                    writer.status(
                        "communications_connected", connection_id=connection_id
                    )
                    last_sequence = {}
                    backoff = 1
                    while not STOP and (
                        not args.duration
                        or time.monotonic() - started < args.duration
                    ):
                        raw = json.loads(await ws.recv())
                        received_at = utc_now()
                        communication_messages_received += 1
                        msg = (
                            raw.get("msg")
                            if isinstance(raw.get("msg"), dict)
                            else {}
                        )
                        sid, seq = raw.get("sid") or msg.get("sid"), raw.get("seq")
                        if isinstance(sid, int) and isinstance(seq, int):
                            previous = last_sequence.get(sid)
                            if previous is not None and seq != previous + 1:
                                raise RuntimeError(
                                    f"communications sequence gap: expected "
                                    f"{previous + 1}, received {seq}"
                                )
                            last_sequence[sid] = seq
                        message_type = raw.get("type")
                        if message_type not in COMMUNICATION_TYPES:
                            for record in state.process(raw, received_at):
                                record["connection_id"] = connection_id
                                record["market_role"] = None
                                writer.write(record)
                            continue
                        is_new_slate_rfq = (
                            message_type == "rfq_created"
                            and is_slate_combo(
                                msg, registry.event_games, registry.combo_scope
                            )
                        )
                        registry.touch_combo(msg.get("market_ticker"))
                        if not is_new_slate_rfq and registry.role(
                            msg.get("market_ticker")
                        ) != "combo":
                            continue
                        identity = communication_identity(raw)
                        if identity and not seen_communications.add(identity):
                            duplicate_communications_skipped += 1
                            continue
                        record = communication_record(raw, received_at)
                        record["connection_id"] = connection_id
                        writer.write(record, flush=is_new_slate_rfq)
                        if not is_new_slate_rfq:
                            continue
                        rfqs_persisted += 1
                        last_rfq_at = received_at
                        last_rfq_exchange_at = record["exchange_timestamp"]
                        last_rfq_persistence_lag_ms = record[
                            "persistence_lag_ms"
                        ]
                        new_tickers = registry.add_combo(
                            {**msg, "ticker": msg.get("market_ticker")},
                            "communications",
                        )
                        apply_registry_evictions()
                        if new_tickers:
                            new_components = set(registry.components).intersection(new_tickers)
                            state.add_tickers(sorted(new_components), pinned=True)
                            state.add_tickers(
                                sorted(set(new_tickers) - new_components)
                            )
                            queue_new_markets(new_tickers)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                writer.status(
                    "communications_disconnected",
                    connection_id=connection_id,
                    error=f"{type(exc).__name__}: {exc}",
                    reconnect_in_seconds=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    communications_task = asyncio.create_task(collect_communications())
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
                state.add_tickers(sorted(registry.tickers))
                state.add_tickers(sorted(registry.components), pinned=True)

                async def process_new_markets() -> None:
                    nonlocal command_id
                    while True:
                        batch = set(await market_queue.get())
                        await asyncio.sleep(args.market_batch_seconds)
                        while not market_queue.empty():
                            batch.update(market_queue.get_nowait())
                        command_id = await subscribe_market_data(
                            ws, command_id, registry, sorted(batch)
                        )
                        components = batch & set(registry.components)
                        if components:
                            metadata = await asyncio.to_thread(
                                market_metadata, client, components
                            )
                            registry.add_component_metadata(metadata)

                market_worker = asyncio.create_task(process_new_markets())
                last_sequence, last_heartbeat = {}, 0.0
                messages, last_message_at = 0, None
                backoff = 1
                try:
                    while not STOP and (not args.duration or time.monotonic() - started < args.duration):
                        now = time.monotonic()
                        if now - last_heartbeat >= args.heartbeat_seconds:
                            await poll_fills()
                            registry.evict_stale(now)
                            apply_registry_evictions()
                            writer.status(
                                "heartbeat",
                                connection_id=connection_id,
                                messages_received=messages,
                                combo_count=len(registry.combos),
                                component_count=len(registry.components),
                                books_initialized=len(state.initialized),
                                settled_combo_count=settled_combo_count,
                                fills_received=fills_received,
                                fill_api_available=fill_api_available,
                                communication_messages_received=communication_messages_received,
                                communications_connected=not communications_task.done(),
                                rfqs_persisted=rfqs_persisted,
                                duplicate_communications_skipped=duplicate_communications_skipped,
                                pending_market_batches=market_queue.qsize(),
                                pending_market_batches_dropped=pending_market_batches_dropped,
                                pending_market_tickers_dropped=pending_market_tickers_dropped,
                                market_state_tickers=len(state.tickers),
                                market_state_tickers_evicted=state.tickers_evicted,
                                trade_dedupe_entries=len(state.seen_trade_ids),
                                communication_dedupe_entries=len(seen_communications),
                                fill_dedupe_entries=len(seen_fills),
                                last_rfq_at=last_rfq_at,
                                last_rfq_exchange_at=last_rfq_exchange_at,
                                last_rfq_persistence_lag_ms=last_rfq_persistence_lag_ms,
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
                            apply_registry_evictions()
                            if new_tickers:
                                new_components = set(registry.components).intersection(new_tickers)
                                state.add_tickers(sorted(new_components), pinned=True)
                                state.add_tickers(
                                    sorted(set(new_tickers) - new_components)
                                )
                                queue_new_markets(new_tickers)
                        registry.touch_combo(ticker, now)
                        for record in state.process(raw, received_at):
                            record["connection_id"] = connection_id
                            record["market_role"] = registry.role(record.get("market_ticker"))
                            if (
                                record["market_role"] == "combo"
                                and record["record_type"] == "market_status"
                                and (
                                    record.get("result") in {"yes", "no"}
                                    or record.get("settlement_value_dollars") is not None
                                    or record.get("status") == "settled"
                                )
                            ):
                                settled_combo_count += 1
                                registry.remove_combos([record["market_ticker"]])
                            writer.write(record)
                        apply_registry_evictions()
                finally:
                    market_worker.cancel()
                    with suppress(asyncio.CancelledError):
                        await market_worker
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
    communications_task.cancel()
    with suppress(asyncio.CancelledError):
        await communications_task
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
    parser.add_argument(
        "--market-batch-seconds",
        type=float,
        default=float(os.getenv("KALSHI_MARKET_BATCH_SECONDS", "0.25")),
    )
    parser.add_argument(
        "--gzip-member-bytes",
        type=int,
        default=int(os.getenv("KALSHI_GZIP_MEMBER_BYTES", str(128 * 1024 * 1024))),
    )
    parser.add_argument(
        "--gzip-member-seconds",
        type=float,
        default=float(os.getenv("KALSHI_GZIP_MEMBER_SECONDS", "3600")),
    )
    parser.add_argument(
        "--volume-capacity-bytes",
        type=int,
        default=int(os.getenv("KALSHI_VOLUME_CAPACITY_BYTES", "5000000000")),
    )
    parser.add_argument(
        "--archive-retention-bytes",
        type=int,
        default=int(os.getenv("KALSHI_ARCHIVE_RETENTION_BYTES", "2600000000")),
    )
    parser.add_argument(
        "--target-storage-bytes",
        type=int,
        default=int(os.getenv("KALSHI_TARGET_STORAGE_BYTES", "2750000000")),
    )
    parser.add_argument(
        "--state-max-tickers",
        type=int,
        default=int(os.getenv("KALSHI_STATE_MAX_TICKERS", "10000")),
    )
    parser.add_argument(
        "--dedupe-entries",
        type=int,
        default=int(os.getenv("KALSHI_DEDUPE_ENTRIES", "100000")),
    )
    parser.add_argument(
        "--max-active-combos",
        type=int,
        default=int(os.getenv("KALSHI_MAX_ACTIVE_COMBOS", "10000")),
    )
    parser.add_argument(
        "--combo-idle-seconds",
        type=float,
        default=float(os.getenv("KALSHI_COMBO_IDLE_SECONDS", "3600")),
    )
    parser.add_argument(
        "--max-pending-market-batches",
        type=int,
        default=int(os.getenv("KALSHI_MAX_PENDING_MARKET_BATCHES", "1000")),
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
    writer = Writer(
        path,
        gzip_member_bytes=args.gzip_member_bytes,
        gzip_member_seconds=args.gzip_member_seconds,
        volume_capacity_bytes=args.volume_capacity_bytes,
        archive_retention_bytes=args.archive_retention_bytes,
        target_storage_bytes=args.target_storage_bytes,
    )
    print(f"capturing {manifest['slate_id']} -> {path}", flush=True)
    try:
        asyncio.run(collect(args, manifest, writer))
    finally:
        writer.close()


if __name__ == "__main__":
    main()
