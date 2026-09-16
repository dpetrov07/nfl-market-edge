"""Create compact per-game Parquet files from the preserved Sunday capture."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


BATCH_SIZE = 25_000
ALIASES = {"JAC": "JAX", "WAS": "WSH"}


@dataclass(frozen=True)
class Game:
    away: str
    home: str
    away_name: str
    home_name: str
    kickoff: datetime
    nfl_path: Path

    @property
    def label(self) -> str:
        return f"{self.away} @ {self.home}"

    @property
    def key(self) -> str:
        return f"{self.away}_{self.home}"


class ParquetBatchWriter:
    def __init__(self, path: Path, schema: pa.Schema):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.tmp_path = path.with_suffix(".parquet.tmp")
        self.schema = schema
        self.rows: list[dict] = []
        self.writer: pq.ParquetWriter | None = None
        self.count = 0

    def add(self, row: dict) -> None:
        self.rows.append(row)
        if len(self.rows) >= BATCH_SIZE:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        table = pa.Table.from_pylist(self.rows, schema=self.schema)
        if self.writer is None:
            self.writer = pq.ParquetWriter(
                self.tmp_path, self.schema, compression="zstd", use_dictionary=True
            )
        self.writer.write_table(table)
        self.count += len(self.rows)
        self.rows.clear()

    def close(self) -> tuple[int, int]:
        self.flush()
        if self.writer is None:
            self.writer = pq.ParquetWriter(
                self.tmp_path, self.schema, compression="zstd", use_dictionary=True
            )
        self.writer.close()
        os.replace(self.tmp_path, self.path)
        return self.count, self.path.stat().st_size


def timestamp(value) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def number(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def integer(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def read_first(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.loads(next(handle))


def game_name_key(away: str, home: str) -> tuple[str, str]:
    clean = lambda value: re.sub(r"[^a-z0-9]", "", value.lower())
    return clean(away), clean(home)


def load_games(input_dir: Path) -> list[Game]:
    games = []
    for path in sorted((input_dir / "nfl_live").glob("*.jsonl.gz")):
        record = read_first(path)
        raw = record["game"]
        games.append(
            Game(
                away=raw["away"]["abbreviation"],
                home=raw["home"]["abbreviation"],
                away_name=raw["away"]["name"],
                home_name=raw["home"]["name"],
                kickoff=timestamp(raw["kickoff"]),
                nfl_path=path,
            )
        )
    if len(games) != 13:
        raise RuntimeError(f"expected 13 NFL games, found {len(games)}")
    return games


KALSHI_SCHEMA = pa.schema([
    ("game", pa.string()),
    ("received_at", pa.timestamp("us", tz="UTC")),
    ("player", pa.string()),
    ("player_id", pa.string()),
    ("prop_type", pa.string()),
    ("threshold", pa.float64()),
    ("event_type", pa.string()),
    ("change_type", pa.string()),
    ("state", pa.string()),
    ("market_ticker", pa.string()),
    ("trade_id", pa.string()),
    ("connection_id", pa.string()),
    ("sid", pa.int64()),
    ("seq", pa.int64()),
    ("exchange_timestamp", pa.string()),
    ("exchange_ts_ms", pa.int64()),
    ("yes_bid", pa.float64()),
    ("yes_bid_size", pa.float64()),
    ("yes_ask", pa.float64()),
    ("yes_ask_size", pa.float64()),
    ("spread", pa.float64()),
    ("trade_yes_price", pa.float64()),
    ("trade_count", pa.float64()),
    ("taker_side", pa.string()),
    ("books_initialized", pa.int64()),
    ("market_count", pa.int64()),
    ("messages_received", pa.int64()),
    ("last_message_at", pa.timestamp("us", tz="UTC")),
])

BOVADA_SCHEMA = pa.schema([
    ("game", pa.string()),
    ("received_at", pa.timestamp("us", tz="UTC")),
    ("player", pa.string()),
    ("player_id", pa.string()),
    ("prop_type", pa.string()),
    ("threshold", pa.float64()),
    ("event_type", pa.string()),
    ("change_type", pa.string()),
    ("side", pa.string()),
    ("american_odds", pa.int64()),
    ("decimal_odds", pa.float64()),
    ("state", pa.string()),
    ("market_state", pa.string()),
    ("selection_state", pa.string()),
    ("is_alternate", pa.bool_()),
    ("event_id", pa.string()),
    ("market_id", pa.string()),
    ("selection_id", pa.string()),
    ("session_id", pa.string()),
    ("source", pa.string()),
])

NFL_SCHEMA = pa.schema([
    ("game", pa.string()),
    ("received_at", pa.timestamp("us", tz="UTC")),
    ("player", pa.string()),
    ("player_id", pa.string()),
    ("prop_type", pa.string()),
    ("threshold", pa.float64()),
    ("event_type", pa.string()),
    ("change_type", pa.string()),
    ("stat_value", pa.float64()),
    ("yards", pa.float64()),
    ("quarter", pa.int64()),
    ("clock", pa.string()),
    ("provider_event_id", pa.string()),
    ("provider_play_id", pa.string()),
    ("sequence_number", pa.int64()),
    ("revision", pa.int64()),
    ("source_fingerprint", pa.string()),
])


def in_window(record: dict, start: datetime, end: datetime) -> datetime | None:
    received_at = timestamp(record.get("received_at"))
    return received_at if received_at and start <= received_at <= end else None


def kalshi_game(path: Path, games: list[Game]) -> Game:
    match = re.search(r"_([A-Z]+)_([A-Z]+)\.jsonl\.gz$", path.name)
    if not match:
        raise RuntimeError(f"cannot parse Kalshi game from {path.name}")
    pair = tuple(ALIASES.get(value, value) for value in match.groups())
    return next(game for game in games if (game.away, game.home) == pair)


def process_kalshi(path: Path, game: Game, output_dir: Path, start: datetime, end: datetime):
    writer = ParquetBatchWriter(output_dir / "kalshi" / f"kalshi_{game.key}.parquet", KALSHI_SCHEMA)
    markets: dict[str, dict] = {}
    incomplete = False
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            while True:
                try:
                    line = handle.readline()
                except EOFError:
                    incomplete = True
                    break
                if not line:
                    break
                record = json.loads(line)
                event_type = record.get("record_type")
                if event_type == "market_discovery":
                    markets.update({row["ticker"]: row for row in record.get("markets", [])})
                    continue
                received_at = in_window(record, start, end)
                if not received_at:
                    continue
                ticker = record.get("market_ticker")
                market = markets.get(ticker, {})
                change_type = (
                    record.get("reason")
                    or record.get("status")
                    or record.get("message_type")
                    or ("trade" if event_type == "trade" else None)
                )
                writer.add({
                    "game": game.label,
                    "received_at": received_at,
                    "player": market.get("player"),
                    "player_id": str(market["player_id"]) if market.get("player_id") else None,
                    "prop_type": market.get("prop_type"),
                    "threshold": number(market.get("threshold")),
                    "event_type": event_type,
                    "change_type": change_type,
                    "state": record.get("status"),
                    "market_ticker": ticker,
                    "trade_id": record.get("trade_id"),
                    "connection_id": record.get("connection_id"),
                    "sid": integer(record.get("sid")),
                    "seq": integer(record.get("seq")),
                    "exchange_timestamp": str(record["exchange_timestamp"]) if record.get("exchange_timestamp") is not None else None,
                    "exchange_ts_ms": integer(record.get("exchange_ts_ms")),
                    "yes_bid": number(record.get("yes_bid_dollars")),
                    "yes_bid_size": number(record.get("yes_bid_size")),
                    "yes_ask": number(record.get("yes_ask_dollars")),
                    "yes_ask_size": number(record.get("yes_ask_size")),
                    "spread": number(record.get("spread_dollars")),
                    "trade_yes_price": number(record.get("yes_price_dollars")),
                    "trade_count": number(record.get("count")),
                    "taker_side": record.get("taker_outcome_side"),
                    "books_initialized": integer(record.get("books_initialized")),
                    "market_count": integer(record.get("market_count")),
                    "messages_received": integer(record.get("messages_received")),
                    "last_message_at": timestamp(record.get("last_message_at")),
                })
    finally:
        count, size = writer.close()
    return count, size, incomplete


def bovada_game(path: Path, games: list[Game]) -> Game:
    first = read_first(path)
    wanted = game_name_key(first["away_team"], first["home_team"])
    return next(game for game in games if game_name_key(game.away_name, game.home_name) == wanted)


def process_bovada(path: Path, game: Game, output_dir: Path, start: datetime, end: datetime):
    writer = ParquetBatchWriter(output_dir / "bovada" / f"bovada_{game.key}.parquet", BOVADA_SCHEMA)
    incomplete = False
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            while True:
                try:
                    line = handle.readline()
                except EOFError:
                    incomplete = True
                    break
                if not line:
                    break
                record = json.loads(line)
                received_at = in_window(record, start, end)
                if not received_at:
                    continue
                writer.add({
                    "game": game.label,
                    "received_at": received_at,
                    "player": record.get("player"),
                    "player_id": record.get("player_id"),
                    "prop_type": record.get("prop_type"),
                    "threshold": number(record.get("line")),
                    "event_type": record.get("record_type"),
                    "change_type": record.get("change_type"),
                    "side": record.get("side"),
                    "american_odds": integer(record.get("american_odds")),
                    "decimal_odds": number(record.get("decimal_odds")),
                    "state": record.get("state"),
                    "market_state": record.get("market_state"),
                    "selection_state": record.get("selection_state"),
                    "is_alternate": record.get("is_alternate"),
                    "event_id": record.get("event_id"),
                    "market_id": record.get("market_id"),
                    "selection_id": record.get("selection_id"),
                    "session_id": record.get("session_id"),
                    "source": record.get("source"),
                })
    finally:
        count, size = writer.close()
    return count, size, incomplete


def process_nfl(game: Game, output_dir: Path, start: datetime, end: datetime):
    writer = ParquetBatchWriter(output_dir / "nfl" / f"nfl_{game.key}.parquet", NFL_SCHEMA)
    with gzip.open(game.nfl_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            received_at = in_window(record, start, end)
            if not received_at:
                continue
            event_type = record.get("record_type")
            writer.add({
                "game": game.label,
                "received_at": received_at,
                "player": record.get("player"),
                "player_id": record.get("player_id"),
                "prop_type": record.get("prop_type"),
                "threshold": number(record.get("threshold")),
                "event_type": event_type,
                "change_type": record.get("play_type") or record.get("status"),
                "stat_value": number(record.get("stat_value")),
                "yards": number(record.get("yards")),
                "quarter": integer(record.get("quarter")),
                "clock": record.get("clock"),
                "provider_event_id": record.get("provider_event_id"),
                "provider_play_id": record.get("provider_play_id"),
                "sequence_number": integer(record.get("sequence_number")),
                "revision": integer(record.get("revision")),
                "source_fingerprint": record.get("source_fingerprint"),
            })
    count, size = writer.close()
    return count, size, False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=Path("data/sunday_2026-09-13"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--minutes-before", type=int, default=60)
    parser.add_argument("--hours-after", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir or args.input_dir / "processed"
    games = load_games(args.input_dir)
    results = {source: {"rows": 0, "bytes": 0, "files": 0, "incomplete_raw": 0}
               for source in ("kalshi", "bovada", "nfl")}

    tasks = []
    for path in sorted((args.input_dir / "kalshi_live").glob("*.jsonl.gz")):
        tasks.append(("kalshi", path, kalshi_game(path, games), process_kalshi))
    for path in sorted((args.input_dir / "bovada_live").glob("*.jsonl.gz")):
        tasks.append(("bovada", path, bovada_game(path, games), process_bovada))
    for game in games:
        tasks.append(("nfl", game.nfl_path, game, process_nfl))

    for source, path, game, processor in tasks:
        start = game.kickoff - timedelta(minutes=args.minutes_before)
        end = game.kickoff + timedelta(hours=args.hours_after)
        if source == "nfl":
            count, size, incomplete = processor(game, output_dir, start, end)
        else:
            count, size, incomplete = processor(path, game, output_dir, start, end)
        results[source]["rows"] += count
        results[source]["bytes"] += size
        results[source]["files"] += 1
        results[source]["incomplete_raw"] += int(incomplete)
        print(f"{source} {game.key}: {count} rows, {size} bytes")

    print(json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    main()
