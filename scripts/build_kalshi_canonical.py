"""Build the inspectable, unfeatured Kalshi player-prop history table."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


SCHEMA = pa.schema(
    [
        ("market_id", pa.string()),
        ("ticker", pa.string()),
        ("event_ticker", pa.string()),
        ("player", pa.string()),
        ("player_id", pa.string()),
        ("game_id", pa.string()),
        ("game", pa.string()),
        ("week", pa.int64()),
        ("team", pa.string()),
        ("opponent", pa.string()),
        ("prop_type", pa.string()),
        ("threshold", pa.float64()),
        ("timestamp", pa.timestamp("ns", tz="UTC")),
        ("kickoff", pa.timestamp("ns", tz="UTC")),
        ("hours_to_kickoff", pa.float64()),
        ("is_pregame", pa.bool_()),
        ("market_open_time", pa.timestamp("ns", tz="UTC")),
        ("yes_bid", pa.float64()),
        ("yes_ask", pa.float64()),
        ("midpoint", pa.float64()),
        ("trade_price", pa.float64()),
        ("spread", pa.float64()),
        ("volume", pa.float64()),
        ("open_interest", pa.float64()),
        ("actual_result", pa.float64()),
        ("settlement_result", pa.string()),
    ]
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/processed/kalshi_player_prop_history.parquet"),
    )
    return parser.parse_args()


def metadata(raw: Path) -> pd.DataFrame:
    matches_path = raw / "kalshi_market_player_matches.parquet"
    if not matches_path.exists():
        raise SystemExit(
            f"Missing {matches_path}. Run scripts/report_coverage.py before this builder."
        )
    markets = pd.read_parquet(matches_path)

    stats = pd.read_parquet(
        raw / "nflverse_player_stats_2025.parquet",
        columns=["player_id", "game_id", "week", "team", "opponent_team"],
    ).drop_duplicates(["player_id", "game_id"])
    markets = markets.merge(stats, on=["player_id", "game_id"], how="left")
    markets["kickoff"] = pd.to_datetime(markets["kickoff"], utc=True)
    markets["open_time"] = pd.to_datetime(markets["open_time"], utc=True)
    return markets.set_index("market_id", drop=False)


def canonical_part(history: pd.DataFrame, market: pd.Series) -> pd.DataFrame:
    history = history.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    timestamp = pd.to_datetime(history["timestamp"], utc=True)
    bid = pd.to_numeric(history.get("yes_bid_close"), errors="coerce")
    ask = pd.to_numeric(history.get("yes_ask_close"), errors="coerce")
    valid_quote = bid.between(0, 1) & ask.between(0, 1) & bid.le(ask)
    bid = bid.where(valid_quote)
    ask = ask.where(valid_quote)
    kickoff = market["kickoff"]

    frame = pd.DataFrame(
        {
            "market_id": market["market_id"],
            "ticker": market["market_id"],
            "event_ticker": market["event_ticker"],
            "player": market["player"],
            "player_id": market["player_id"],
            "game_id": market["game_id"],
            "game": market["game"],
            "week": market["week"],
            "team": market["team"],
            "opponent": market["opponent_team"],
            "prop_type": market["prop_type"],
            "threshold": market["threshold"],
            "timestamp": timestamp,
            "kickoff": kickoff,
            "hours_to_kickoff": (kickoff - timestamp).dt.total_seconds() / 3600,
            "is_pregame": timestamp.lt(kickoff),
            "market_open_time": market["open_time"],
            "yes_bid": bid,
            "yes_ask": ask,
            "midpoint": (bid + ask) / 2,
            "trade_price": pd.to_numeric(history.get("trade_close"), errors="coerce"),
            "spread": ask - bid,
            "volume": pd.to_numeric(history.get("volume"), errors="coerce"),
            "open_interest": pd.to_numeric(
                history.get("open_interest"), errors="coerce"
            ),
            "actual_result": market["actual_stat"],
            "settlement_result": market["result"],
        }
    )
    return frame.loc[:, SCHEMA.names]


def build(raw: Path, output: Path) -> None:
    markets = metadata(raw)
    parts = sorted((raw / "kalshi_price_parts").glob("*.parquet"))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp.parquet")
    written_rows = 0
    written_markets = 0

    with pq.ParquetWriter(temporary, SCHEMA, compression="zstd") as writer:
        for number, part in enumerate(parts, start=1):
            if part.stem not in markets.index or pq.read_metadata(part).num_rows == 0:
                continue
            history = pd.read_parquet(part)
            frame = canonical_part(history, markets.loc[part.stem])
            writer.write_table(
                pa.Table.from_pandas(frame, schema=SCHEMA, preserve_index=False)
            )
            written_rows += len(frame)
            written_markets += 1
            if number % 1000 == 0:
                print(f"Processed {number:,}/{len(parts):,} checkpoint files", flush=True)

    temporary.replace(output)
    print(
        f"Wrote {written_rows:,} rows for {written_markets:,} markets -> {output}",
        flush=True,
    )


if __name__ == "__main__":
    args = parse_args()
    build(args.raw, args.output)
