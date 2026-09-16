"""Analyze executable Bovada/Kalshi residual convergence without fitting a model."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

import pyarrow as pa
import pyarrow.parquet as pq

from helpers import (
    annotate_bovada_payloads,
    bovada_price_events,
    read_mapping,
    scan_kalshi_horizons,
)


HORIZONS = (1, 3, 5, 10, 30)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processed-dir", type=Path, default=Path("data/sunday_2026-09-13/processed"))
    parser.add_argument("--mapping", type=Path, default=Path("data/sunday_2026-09-13/mappings/bovada_kalshi_props.parquet"))
    parser.add_argument("--output", type=Path, default=Path("data/sunday_2026-09-13/timing/bovada_kalshi_residuals.parquet"))
    parser.add_argument("--min-bovada-move", type=float, default=0.02)
    parser.add_argument("--min-residual", type=float, default=0.05)
    parser.add_argument("--max-spread", type=float, default=0.05)
    parser.add_argument("--min-size", type=float, default=50)
    parser.add_argument("--taker-fee-rate", type=float, default=0.07)
    parser.add_argument("--slippage", type=float, default=0.01)
    return parser.parse_args()


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def fee_per_contract(price, rate):
    return rate * price * (1 - price)


def output_row(event, args):
    mapping = event["pair"]["over"]
    trade_price = event["initial_price"] if event["direction"] > 0 else 1 - event["initial_price"]
    fee = fee_per_contract(trade_price, args.taker_fee_rate)
    row = {
        "game": mapping["game"],
        "player": mapping["kalshi_player"],
        "player_key": event["key"][0],
        "prop_type": event["key"][1],
        "market_threshold": event["key"][2],
        "episode_id": event["episode_id"],
        "batch_class": event["batch_class"],
        "payload_selection_count": event["payload_selection_count"],
        "bovada_received_at": event["at"],
        "bovada_old_no_vig_prob": event["old_prob"],
        "bovada_new_no_vig_prob": event["new_prob"],
        "bovada_move": abs(event["delta"]),
        "direction": "up" if event["direction"] > 0 else "down",
        "kalshi_market_ticker": event["ticker"],
        "kalshi_executable_side": "yes_" + event["exec_side"],
        "kalshi_initial_price": event["initial_price"],
        "kalshi_initial_size": event["initial_size"],
        "kalshi_spread": event["spread"],
        "kalshi_quote_age_seconds": event["quote_age_seconds"],
        "residual": event["residual"],
        "quote_available_seconds_30s": event["available_seconds"],
        "availability_censored_30s": event["availability_censored"],
        "min_size_while_available": event["min_size_while_available"],
        "entry_fee_per_contract": fee,
        "net_residual_after_fee_slippage": event["residual"] - fee - args.slippage,
        "strong_signal": (
            event["batch_class"] == "game_wide"
            and event["residual"] >= args.min_residual
            and event["spread"] is not None
            and event["spread"] <= args.max_spread
            and event["initial_size"] >= args.min_size
        ),
        "bovada_market_id": mapping["bovada_market_id"],
        "bovada_over_selection_id": event["pair"]["over"]["bovada_selection_id"],
        "bovada_under_selection_id": event["pair"]["under"]["bovada_selection_id"],
    }
    for horizon in HORIZONS:
        row[f"kalshi_move_{horizon}s"] = event["outcomes"].get(horizon)
    return row


def horizon_summary(rows, horizon):
    values = [row[f"kalshi_move_{horizon}s"] for row in rows if row[f"kalshi_move_{horizon}s"] is not None]
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "hit_rate": sum(value > 0 for value in values) / len(values),
        "adverse_rate": sum(value < 0 for value in values) / len(values),
        "mean_move": mean(values),
        "median_move": median(values),
        "mean_move_capped_10c": mean(max(-0.10, min(0.10, value)) for value in values),
    }


def robustness(rows, horizon):
    values = sorted(
        (row[f"kalshi_move_{horizon}s"] for row in rows if row[f"kalshi_move_{horizon}s"] is not None),
        reverse=True,
    )
    return {
        "raw_mean": mean(values) if values else None,
        "drop_largest": mean(values[1:]) if len(values) > 1 else None,
        "drop_two_largest": mean(values[2:]) if len(values) > 2 else None,
        "drop_three_largest": mean(values[3:]) if len(values) > 3 else None,
    }


def main():
    args = parse_args()
    mapping = read_mapping(args.mapping)
    events = []
    mapped_thresholds = 0
    for bovada_path in sorted((args.processed_dir / "bovada").glob("*.parquet")):
        game = pq.read_table(bovada_path, columns=["game"]).column("game")[0].as_py()
        slug = bovada_path.stem.removeprefix("bovada_")
        game_events, pair_count = bovada_price_events(
            bovada_path, mapping.get(game, {}), min_move=args.min_bovada_move
        )
        annotate_bovada_payloads(bovada_path, game_events)
        scan_kalshi_horizons(
            args.processed_dir / "kalshi" / f"kalshi_{slug}.parquet",
            game_events,
            HORIZONS,
        )
        events.extend(game_events)
        mapped_thresholds += pair_count

    rows = [output_row(event, args) for event in events if "initial_price" in event]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    pq.write_table(pa.Table.from_pylist(rows), temporary, compression="zstd")
    os.replace(temporary, args.output)

    strong_rows = [row for row in rows if row["strong_signal"]]
    episodes = defaultdict(list)
    for row in strong_rows:
        episodes[row["episode_id"]].append(row)
    independent = [max(group, key=lambda row: (row["residual"], row["kalshi_initial_size"])) for group in episodes.values()]

    summary = {
        "output": str(args.output),
        "rows": len(rows),
        "mapped_two_sided_thresholds": mapped_thresholds,
        "batch_classes": dict(Counter(row["batch_class"] for row in rows)),
        "strong_rows": len(strong_rows),
        "strong_independent_episodes": len(independent),
        "strong_games": len({row["game"] for row in independent}),
        "strong_players": len({row["player_key"] for row in independent}),
        "strong_horizons": {str(horizon): horizon_summary(independent, horizon) for horizon in HORIZONS},
        "strong_robustness": {
            "10s": robustness(independent, 10),
            "30s": robustness(independent, 30),
        },
        "strong_executability": {
            "median_spread": percentile([row["kalshi_spread"] for row in independent], 0.5),
            "median_initial_size": percentile([row["kalshi_initial_size"] for row in independent], 0.5),
            "median_available_seconds": percentile([row["quote_available_seconds_30s"] for row in independent], 0.5),
            "median_net_residual_after_fee_slippage": percentile(
                [row["net_residual_after_fee_slippage"] for row in independent], 0.5
            ),
        },
    }
    print(json.dumps(summary, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
