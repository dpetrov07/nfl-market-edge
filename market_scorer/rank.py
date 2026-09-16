"""Rank executable Kalshi player-prop prices after clean Bovada repricings."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from helpers import (
    annotate_bovada_payloads,
    bovada_price_events,
    read_mapping,
    scan_kalshi_horizons,
)


DEFAULT_ROOT = Path("data/sunday_2026-09-13")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--game", action="append", help="Exact game label; repeatable")
    parser.add_argument("--player", help="Case-insensitive player name lookup")
    parser.add_argument(
        "--prop-type", choices=("receiving_yards", "rushing_yards")
    )
    parser.add_argument("--threshold", type=float)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument(
        "--latest-only", action="store_true",
        help="Keep only the latest qualifying repricing for each market",
    )
    parser.add_argument("--min-bovada-move", type=float, default=0.02)
    parser.add_argument("--min-residual", type=float, default=0.05)
    parser.add_argument("--max-spread", type=float, default=0.05)
    parser.add_argument("--min-size", type=float, default=50)
    parser.add_argument("--taker-fee-rate", type=float, default=0.07)
    parser.add_argument("--slippage", type=float, default=0.01)
    return parser.parse_args()


def fee_per_contract(price, rate):
    return rate * price * (1 - price)


def add_neighbor_tickers(events, game_mapping):
    ladders = {}
    for key, rows in game_mapping.items():
        ticker = rows[0]["kalshi_market_ticker"]
        ladders.setdefault(key[:2], []).append((key[2], ticker))
    for ladder in ladders.values():
        ladder.sort()

    for event in events:
        ladder = ladders[event["key"][:2]]
        index = next(i for i, item in enumerate(ladder) if item[0] == event["key"][2])
        event["neighbor_tickers"] = {}
        if index:
            threshold, ticker = ladder[index - 1]
            event["neighbor_tickers"]["lower"] = {
                "threshold": threshold, "ticker": ticker,
            }
        if index + 1 < len(ladder):
            threshold, ticker = ladder[index + 1]
            event["neighbor_tickers"]["higher"] = {
                "threshold": threshold, "ticker": ticker,
            }


def ladder_check(event, entry_side):
    quotes = event.get("neighbor_quotes", {})
    comparable = []
    violations = []
    for label in ("lower", "higher"):
        quote = quotes.get(label)
        if not quote:
            continue
        price = (
            quote["yes_ask"] if entry_side == "yes"
            else 1 - quote["yes_bid"] if quote["yes_bid"] is not None else None
        )
        if price is None:
            continue
        comparable.append(f'{label} {quote["threshold"]:g}={price:.2f}')
        if label == "lower":
            violated = price + 0.01 < event["entry_price"] if entry_side == "yes" else price > event["entry_price"] + 0.01
        else:
            violated = price > event["entry_price"] + 0.01 if entry_side == "yes" else price + 0.01 < event["entry_price"]
        if violated:
            violations.append(label)
    status = "violation" if violations else "ok" if comparable else "unavailable"
    return status, ", ".join(comparable) or None


def score_event(event, args):
    mapping = event["pair"]["over"]
    entry_side = "yes" if event["direction"] > 0 else "no"
    entry_price = event["initial_price"] if entry_side == "yes" else 1 - event["initial_price"]
    event["entry_price"] = entry_price
    fair = event["new_prob"] if entry_side == "yes" else 1 - event["new_prob"]
    prior_fair = event["old_prob"] if entry_side == "yes" else 1 - event["old_prob"]
    fair_low, fair_high = sorted((fair, prior_fair))
    fee = fee_per_contract(entry_price, args.taker_fee_rate)
    gross_edge = fair - entry_price
    conservative_edge = fair_low - entry_price
    net_edge = gross_edge - fee - args.slippage
    conservative_net_edge = conservative_edge - fee - args.slippage
    ladder_status, ladder_context = ladder_check(event, entry_side)
    strong = (
        event["batch_class"] == "game_wide"
        and gross_edge >= args.min_residual
        and event["spread"] is not None
        and event["spread"] <= args.max_spread
        and event["initial_size"] >= args.min_size
    )
    quality = {"game_wide": 1.0, "small_batch": 0.7, "isolated": 0.4}[event["batch_class"]]
    quality *= 1.0 if event["spread"] is not None and event["spread"] <= args.max_spread else 0.5
    quality *= min(1.0, event["initial_size"] / args.min_size)
    quality *= 0.5 if ladder_status == "violation" else 1.0
    opportunity_score = round(
        100 * max(0.0, min(1.0, conservative_net_edge / 0.10)) * quality
    )
    if strong and conservative_net_edge > 0 and ladder_status != "violation":
        action = "enter"
    elif net_edge > 0 and ladder_status != "violation":
        action = "watch"
    else:
        action = "pass"
    recommendation = (
        "strong" if action == "enter" and opportunity_score >= 50 else action
    )

    return {
        "game": mapping["game"],
        "player": mapping["kalshi_player"],
        "prop_type": event["key"][1],
        "threshold": event["key"][2],
        "entry_side": entry_side,
        "kalshi_market_ticker": event["ticker"],
        "kalshi_executable_price": entry_price,
        "kalshi_available_size": event["initial_size"],
        "kalshi_spread": event["spread"],
        "kalshi_quote_age_seconds": event["quote_age_seconds"],
        "bovada_fair_probability": fair,
        "bovada_over_fair_probability": event["new_prob"],
        "gross_disagreement": gross_edge,
        "entry_fee": fee,
        "assumed_slippage": args.slippage,
        "net_edge": net_edge,
        "conservative_net_edge": conservative_net_edge,
        "fair_value_low": fair_low,
        "fair_value_high": fair_high,
        "fair_value_range_basis": "old-to-new Bovada no-vig repricing interval",
        "repricing_episode_id": event["episode_id"],
        "bovada_repriced_at": event["at"],
        "bovada_over_old_probability": event["old_prob"],
        "bovada_over_new_probability": event["new_prob"],
        "bovada_move": abs(event["delta"]),
        "repricing_direction": "up" if event["direction"] > 0 else "down",
        "repricing_scope": event["batch_class"],
        "repricing_selection_count": event["payload_selection_count"],
        "quote_available_seconds_30s": event["available_seconds"],
        "min_size_while_available": event["min_size_while_available"],
        "historical_markout_10s": event["outcomes"].get(10),
        "historical_markout_30s": event["outcomes"].get(30),
        "ladder_check": ladder_status,
        "neighbor_prices": ladder_context,
        "research_filter_pass": strong,
        "recommendation": recommendation,
        "signal_score": opportunity_score,
    }


def add_tier_context(rows):
    for recommendation in ("strong", "enter", "watch", "pass"):
        group = [row for row in rows if row["recommendation"] == recommendation]
        if not group:
            continue
        context = {}
        for horizon in (10, 30):
            values = [
                row[f"historical_markout_{horizon}s"]
                for row in group
                if row[f"historical_markout_{horizon}s"] is not None
            ]
            values.sort()
            context[f"tier_history_{horizon}s_n"] = len(values)
            if not values:
                context.update({
                    f"tier_history_{horizon}s_mean": None,
                    f"tier_history_{horizon}s_median": None,
                    f"tier_history_{horizon}s_hit_rate": None,
                    f"tier_history_{horizon}s_adverse_rate": None,
                })
                continue
            middle = len(values) // 2
            median = (
                values[middle] if len(values) % 2
                else (values[middle - 1] + values[middle]) / 2
            )
            context.update({
                f"tier_history_{horizon}s_mean": sum(values) / len(values),
                f"tier_history_{horizon}s_median": median,
                f"tier_history_{horizon}s_hit_rate": sum(value > 0 for value in values) / len(values),
                f"tier_history_{horizon}s_adverse_rate": sum(value < 0 for value in values) / len(values),
            })
        for row in group:
            row.update(context)


def print_rows(rows, limit, detailed=False):
    print(
        "tier   signal_score side price  fair range   edge   net  sprd   size  "
        "| history: n  10s hit/med  30s hit/med | player / prop"
    )
    for row in rows[:limit]:
        prop = "rec" if row["prop_type"] == "receiving_yards" else "rush"
        spread = "  n/a" if row["kalshi_spread"] is None else f'{row["kalshi_spread"]:>5.2f}'
        history = {}
        for horizon in (10, 30):
            hit = row[f"tier_history_{horizon}s_hit_rate"]
            median = row[f"tier_history_{horizon}s_median"]
            history[horizon] = (
                "n/a" if hit is None else f"{hit:.0%}/{median * 100:+.1f}c"
            )
        print(
            f'{row["recommendation"]:<6} {row["signal_score"]:>12} '
            f'{row["entry_side"].upper():<3} '
            f'{row["kalshi_executable_price"]:>5.2f} '
            f'{row["fair_value_low"]:.2f}-{row["fair_value_high"]:.2f} '
            f'{row["gross_disagreement"]:>5.2f} {row["net_edge"]:>5.2f} {spread} '
            f'{row["kalshi_available_size"]:>6.0f}  '
            f'| n={row["tier_history_10s_n"]:<3} '
            f'{history[10]:>11} {history[30]:>11} | '
            f'{row["player"]} {prop} >{row["threshold"]:g} '
            f'({row["repricing_scope"]}, ladder {row["ladder_check"]})'
        )
        if detailed:
            parts = []
            for horizon in (10, 30):
                mean = row[f"tier_history_{horizon}s_mean"]
                median = row[f"tier_history_{horizon}s_median"]
                hit = row[f"tier_history_{horizon}s_hit_rate"]
                adverse = row[f"tier_history_{horizon}s_adverse_rate"]
                parts.append(
                    f"{horizon}s avg {mean * 100:+.1f}c, med {median * 100:+.1f}c, "
                    f"hit {hit:.0%}, adverse {adverse:.0%}"
                    if mean is not None else f"{horizon}s unavailable"
                )
            print(f'       {row["recommendation"]} history: ' + "; ".join(parts))


def main():
    args = parse_args()
    processed = args.data_root / "processed"
    mapping_path = args.data_root / "mappings" / "bovada_kalshi_props.parquet"
    output = args.output or args.data_root / "timing" / "player_prop_opportunities.parquet"
    mapping = read_mapping(mapping_path)
    events = []
    for bovada_path in sorted((processed / "bovada").glob("*.parquet")):
        game = pq.read_table(bovada_path, columns=["game"]).column("game")[0].as_py()
        if args.game and game not in args.game:
            continue
        slug = bovada_path.stem.removeprefix("bovada_")
        game_events, _ = bovada_price_events(
            bovada_path, mapping.get(game, {}), min_move=args.min_bovada_move
        )
        annotate_bovada_payloads(bovada_path, game_events)
        add_neighbor_tickers(game_events, mapping.get(game, {}))
        scan_kalshi_horizons(
            processed / "kalshi" / f"kalshi_{slug}.parquet",
            game_events,
            (10, 30),
        )
        events.extend(game_events)

    rows = [score_event(event, args) for event in events if "initial_price" in event]
    add_tier_context(rows)
    lookup = args.player is not None or args.prop_type is not None or args.threshold is not None
    if args.player:
        player = args.player.casefold()
        rows = [row for row in rows if player in row["player"].casefold()]
    if args.prop_type:
        rows = [row for row in rows if row["prop_type"] == args.prop_type]
    if args.threshold is not None:
        rows = [row for row in rows if abs(row["threshold"] - args.threshold) < 1e-9]
    if args.latest_only:
        latest = {}
        for row in rows:
            ticker = row["kalshi_market_ticker"]
            if (
                ticker not in latest
                or row["bovada_repriced_at"] > latest[ticker]["bovada_repriced_at"]
            ):
                latest[ticker] = row
        rows = list(latest.values())
    tier_order = {"strong": 3, "enter": 2, "watch": 1, "pass": 0}
    if lookup:
        rows.sort(key=lambda row: row["bovada_repriced_at"], reverse=True)
    else:
        rows.sort(
            key=lambda row: (tier_order[row["recommendation"]], row["net_edge"]),
            reverse=True,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    pq.write_table(pa.Table.from_pylist(rows), temporary, compression="zstd")
    os.replace(temporary, output)
    print_rows(rows, args.top, detailed=lookup)
    print(json.dumps({
        "output": str(output),
        "rows": len(rows),
        "recommendations": dict(Counter(row["recommendation"] for row in rows)),
        "research_filter_pass": sum(row["research_filter_pass"] for row in rows),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
