"""Build a reusable Bovada-to-Kalshi player-prop market mapping."""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


PROP_TYPES = {"receiving_yards", "rushing_yards"}
PLAYER_ALIASES = {
    "joshuapalmer": "joshpalmer",
    "kennygainwell": "kennethgainwell",
    "hollywoodbrown": "marquisebrown",
}

MAPPING_SCHEMA = pa.schema([
    ("game", pa.string()),
    ("player_key", pa.string()),
    ("kalshi_player", pa.string()),
    ("kalshi_player_id", pa.string()),
    ("bovada_player", pa.string()),
    ("bovada_player_id", pa.string()),
    ("prop_type", pa.string()),
    ("market_threshold", pa.float64()),
    ("bovada_side", pa.string()),
    ("kalshi_outcome", pa.string()),
    ("stat_operator", pa.string()),
    ("stat_threshold", pa.int64()),
    ("kalshi_market_ticker", pa.string()),
    ("bovada_event_id", pa.string()),
    ("bovada_market_id", pa.string()),
    ("bovada_selection_id", pa.string()),
    ("bovada_is_alternate", pa.bool_()),
])


def normalize_player(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    parts = re.findall(r"[a-z0-9]+", text.lower())
    while parts and parts[-1] in {"jr", "sr", "ii", "iii", "iv", "v"}:
        parts.pop()
    key = "".join(parts)
    return PLAYER_ALIASES.get(key, key)


def kalshi_strike(ticker: str) -> int | None:
    match = re.search(r"-(\d+)$", ticker)
    return int(match.group(1)) if match else None


def extract_kalshi(root: Path) -> tuple[dict, dict]:
    by_ticker = {}
    schemas = {}
    columns = ["game", "player", "player_id", "prop_type", "threshold", "market_ticker"]
    for path in sorted((root / "kalshi").glob("*.parquet")):
        parquet = pq.ParquetFile(path)
        schemas[path.name] = parquet.schema_arrow.field("received_at").type
        for batch in parquet.iter_batches(batch_size=65_536, columns=columns):
            data = batch.to_pydict()
            for values in zip(*(data[column] for column in columns)):
                row = dict(zip(columns, values))
                if row["prop_type"] in PROP_TYPES and row["market_ticker"]:
                    by_ticker.setdefault(row["market_ticker"], row)
    by_key = defaultdict(list)
    for row in by_ticker.values():
        key = (
            row["game"], normalize_player(row["player"]),
            row["prop_type"], row["threshold"],
        )
        by_key[key].append(row)
    return by_key, schemas


def extract_bovada(root: Path) -> tuple[dict, dict, int]:
    by_selection_line = {}
    schemas = {}
    market_id_changes = set()
    columns = [
        "game", "player", "player_id", "prop_type", "threshold", "side",
        "event_id", "market_id", "selection_id", "is_alternate",
    ]
    for path in sorted((root / "bovada").glob("*.parquet")):
        parquet = pq.ParquetFile(path)
        schemas[path.name] = parquet.schema_arrow.field("received_at").type
        for batch in parquet.iter_batches(batch_size=65_536, columns=columns):
            data = batch.to_pydict()
            for values in zip(*(data[column] for column in columns)):
                row = dict(zip(columns, values))
                if row["prop_type"] not in PROP_TYPES or not row["selection_id"]:
                    continue
                identity = row["selection_id"], row["threshold"]
                old = by_selection_line.get(identity)
                if old and old["market_id"] != row["market_id"]:
                    market_id_changes.add(identity)
                by_selection_line[identity] = row
    by_key = defaultdict(list)
    for row in by_selection_line.values():
        key = (
            row["game"], normalize_player(row["player"]),
            row["prop_type"], row["threshold"],
        )
        by_key[key].append(row)
    return by_key, schemas, len(market_id_changes)


def valid_market(key: tuple, kalshi_rows: list[dict]) -> bool:
    threshold = key[3]
    if threshold is None or abs((threshold % 1) - 0.5) > 1e-9:
        return False
    expected_strike = int(threshold + 0.5)
    return len(kalshi_rows) == 1 and kalshi_strike(kalshi_rows[0]["market_ticker"]) == expected_strike


def build_mapping(kalshi: dict, bovada: dict) -> tuple[list[dict], set[tuple]]:
    matched_keys = {
        key for key in set(kalshi) & set(bovada) if valid_market(key, kalshi[key])
    }
    rows = []
    for key in sorted(matched_keys):
        game, player_key, prop_type, threshold = key
        kalshi_row = kalshi[key][0]
        for bovada_row in sorted(
            bovada[key], key=lambda row: (row["selection_id"], row["side"] or "")
        ):
            side = bovada_row["side"]
            if side not in {"over", "under"}:
                continue
            rows.append({
                "game": game,
                "player_key": player_key,
                "kalshi_player": kalshi_row["player"],
                "kalshi_player_id": kalshi_row["player_id"],
                "bovada_player": bovada_row["player"],
                "bovada_player_id": bovada_row["player_id"],
                "prop_type": prop_type,
                "market_threshold": threshold,
                "bovada_side": side,
                "kalshi_outcome": "yes" if side == "over" else "no",
                "stat_operator": ">=" if side == "over" else "<=",
                "stat_threshold": int(threshold + 0.5 if side == "over" else threshold - 0.5),
                "kalshi_market_ticker": kalshi_row["market_ticker"],
                "bovada_event_id": bovada_row["event_id"],
                "bovada_market_id": bovada_row["market_id"],
                "bovada_selection_id": bovada_row["selection_id"],
                "bovada_is_alternate": bovada_row["is_alternate"],
            })
    return rows, matched_keys


def interval(existing, value):
    if value is None:
        return existing
    if existing is None:
        return [value, value]
    existing[0] = min(existing[0], value)
    existing[1] = max(existing[1], value)
    return existing


def timing_overlap(root: Path, mapping_rows: list[dict]) -> tuple[dict, bool]:
    tickers = defaultdict(set)
    selections = defaultdict(set)
    for row in mapping_rows:
        tickers[row["game"]].add(row["kalshi_market_ticker"])
        selections[row["game"]].add(
            (row["bovada_selection_id"], row["market_threshold"])
        )

    spans = {source: {} for source in ("kalshi", "bovada", "nfl")}
    for path in sorted((root / "kalshi").glob("*.parquet")):
        for batch in pq.ParquetFile(path).iter_batches(
            batch_size=65_536, columns=["game", "received_at", "market_ticker"]
        ):
            data = batch.to_pydict()
            for game, received_at, ticker in zip(
                data["game"], data["received_at"], data["market_ticker"]
            ):
                if ticker in tickers[game]:
                    spans["kalshi"][game] = interval(spans["kalshi"].get(game), received_at)

    for path in sorted((root / "bovada").glob("*.parquet")):
        for batch in pq.ParquetFile(path).iter_batches(
            batch_size=65_536,
            columns=["game", "received_at", "selection_id", "threshold"],
        ):
            data = batch.to_pydict()
            for game, received_at, selection_id, threshold in zip(
                data["game"], data["received_at"], data["selection_id"], data["threshold"]
            ):
                if (selection_id, threshold) in selections[game]:
                    spans["bovada"][game] = interval(spans["bovada"].get(game), received_at)

    for path in sorted((root / "nfl").glob("*.parquet")):
        for batch in pq.ParquetFile(path).iter_batches(
            columns=["game", "received_at", "event_type"]
        ):
            data = batch.to_pydict()
            for game, received_at, event_type in zip(
                data["game"], data["received_at"], data["event_type"]
            ):
                if event_type in {"play_new", "play_correction"}:
                    spans["nfl"][game] = interval(spans["nfl"].get(game), received_at)

    overlap_minutes = {}
    for game in sorted(tickers):
        source_spans = [spans[source].get(game) for source in spans]
        if any(value is None for value in source_spans):
            overlap_minutes[game] = 0.0
            continue
        start = max(value[0] for value in source_spans)
        end = min(value[1] for value in source_spans)
        overlap_minutes[game] = max(0.0, (end - start).total_seconds() / 60)
    return overlap_minutes, all(value > 0 for value in overlap_minutes.values())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--processed-dir", type=Path,
        default=Path("data/sunday_2026-09-13/processed"),
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = args.output or args.processed_dir.parent / "mappings" / "bovada_kalshi_props.parquet"
    kalshi, kalshi_schemas = extract_kalshi(args.processed_dir)
    bovada, bovada_schemas, market_id_changes = extract_bovada(args.processed_dir)
    rows, matched_keys = build_mapping(kalshi, bovada)

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".parquet.tmp")
    pq.write_table(pa.Table.from_pylist(rows, schema=MAPPING_SCHEMA), tmp, compression="zstd")
    os.replace(tmp, output)

    overlap_minutes, all_games_overlap = timing_overlap(args.processed_dir, rows)
    games = sorted({key[0] for key in matched_keys})
    by_game = {
        game: {
            "players": len({key[1] for key in matched_keys if key[0] == game}),
            "thresholds": sum(key[0] == game for key in matched_keys),
            "mapping_rows": sum(row["game"] == game for row in rows),
        }
        for game in games
    }
    kalshi_only = set(kalshi) - matched_keys
    bovada_only = set(bovada) - matched_keys
    kalshi_player_props = {key[:3] for key in kalshi}
    bovada_player_props = {key[:3] for key in bovada}
    summary = {
        "mapping_rows": len(rows),
        "mapping_bytes": output.stat().st_size,
        "matched_players": len({key[:2] for key in matched_keys}),
        "matched_player_props": len({key[:3] for key in matched_keys}),
        "matched_thresholds": len(matched_keys),
        "matched_by_prop": dict(Counter(key[2] for key in matched_keys)),
        "by_game": by_game,
        "kalshi_unmatched_thresholds": len(kalshi_only),
        "kalshi_unmatched_no_player_prop": sum(
            key[:3] not in bovada_player_props for key in kalshi_only
        ),
        "bovada_unmatched_thresholds": len(bovada_only),
        "bovada_unmatched_no_player_prop": sum(
            key[:3] not in kalshi_player_props for key in bovada_only
        ),
        "bovada_market_id_changes": market_id_changes,
        "utc_received_at": all(
            str(value) == "timestamp[us, tz=UTC]"
            for value in [*kalshi_schemas.values(), *bovada_schemas.values()]
        ),
        "all_games_overlap_nfl_live_window": all_games_overlap,
        "live_overlap_minutes_range": [
            round(min(overlap_minutes.values()), 1), round(max(overlap_minutes.values()), 1)
        ],
        "output": str(output),
    }
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
