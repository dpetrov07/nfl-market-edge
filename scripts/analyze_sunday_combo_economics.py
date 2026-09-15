"""Lightweight economics check for taking the NO side of observed Sunday combo fills."""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

import pyarrow as pa
import pyarrow.parquet as pq


DEFAULT_ROOT = Path("data/sunday_2026-09-13")
FEE_RATE = 0.07
GAME_ALIASES = {"CLE @ JAC": "CLE @ JAX", "WAS @ PHI": "WSH @ PHI"}


def average(rows, field, weight=None):
    if not rows:
        return None
    if weight is None:
        return mean(row[field] for row in rows)
    total = sum(row[weight] for row in rows)
    return sum(row[field] * row[weight] for row in rows) / total if total else None


def group_by_combo(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["combo_market_ticker"]].append(row)
    return grouped


def summarize(rows):
    by_combo = group_by_combo(rows)
    combo_values = []
    for group in by_combo.values():
        size = sum(row["size"] for row in group)
        combo_values.append(
            {
                "gross": sum(row["gross_pnl"] for row in group) / size,
                "net": sum(row["net_pnl"] for row in group) / size,
            }
        )
    size = sum(row["size"] for row in rows)
    return {
        "fills": len(rows),
        "combos": len(by_combo),
        "contracts": round(size, 4),
        "equal_combo_gross_per_contract": mean(row["gross"] for row in combo_values)
        if combo_values else None,
        "equal_combo_net_per_contract": mean(row["net"] for row in combo_values)
        if combo_values else None,
        "volume_weighted_gross_per_contract": sum(row["gross_pnl"] for row in rows) / size
        if size else None,
        "volume_weighted_net_per_contract": sum(row["net_pnl"] for row in rows) / size
        if size else None,
        "gross_pnl": sum(row["gross_pnl"] for row in rows),
        "net_pnl": sum(row["net_pnl"] for row in rows),
    }


def short_yes_risk_audit(rows, legs, price_ceiling=0.10):
    candidate = [
        row for row in rows
        if row["scope"] == "cross_game" and row["yes_price"] < price_ceiling
    ]
    by_combo = group_by_combo(candidate)
    positions = []
    for ticker, group in by_combo.items():
        size = sum(row["size"] for row in group)
        positions.append(
            {
                "combo_market_ticker": ticker,
                "yes_price": average(group, "yes_price", "size"),
                "settlement_value": group[0]["settlement_value"],
                "gross": sum(row["gross_pnl"] for row in group) / size,
                "net": sum(row["net_pnl"] for row in group) / size,
                "games": group[0]["games"].split(", "),
            }
        )

    legs_by_combo = defaultdict(list)
    for leg in legs:
        if leg["combo_market_ticker"] in by_combo:
            legs_by_combo[leg["combo_market_ticker"]].append(leg)

    game_overlap = Counter()
    directional_leg_overlap = Counter()
    directional_leg_details = {}
    leg_slots = 0
    for position in positions:
        game_overlap.update(set(position["games"]))
        for leg in legs_by_combo[position["combo_market_ticker"]]:
            key = (leg["underlying_market_ticker"], leg["side"])
            directional_leg_overlap[key] += 1
            directional_leg_details[key] = {
                "underlying_market_ticker": key[0],
                "side": key[1],
                "game": leg["game"],
            }
            leg_slots += 1

    game_pnl = Counter()
    for position in positions:
        games = set(position["games"])
        for game in games:
            game_pnl[game] += position["net"] / len(games)
    total_net = sum(position["net"] for position in positions)
    absolute_game_pnl = sum(abs(value) for value in game_pnl.values())
    game_rows = [
        {
            "game": game,
            "combos": game_overlap[game],
            "allocated_net_pnl": pnl,
            "signed_share_of_total": pnl / total_net if total_net else None,
            "absolute_share": abs(pnl) / absolute_game_pnl if absolute_game_pnl else None,
        }
        for game, pnl in sorted(game_pnl.items(), key=lambda item: item[1], reverse=True)
    ]
    leave_one_game_out = []
    for game in sorted(game_overlap):
        kept = [position for position in positions if game not in position["games"]]
        leave_one_game_out.append(
            {
                "excluded_game": game,
                "combos": len(kept),
                "total_net_pnl": sum(position["net"] for position in kept),
                "mean_net_per_contract": mean(position["net"] for position in kept),
            }
        )

    largest_losses = []
    for position in sorted(positions, key=lambda row: row["net"])[:5]:
        largest_losses.append(
            {
                **position,
                "directional_legs": [
                    f'{leg["underlying_market_ticker"]}:{leg["side"]}'
                    for leg in sorted(
                        legs_by_combo[position["combo_market_ticker"]],
                        key=lambda row: row["leg_index"],
                    )
                ],
            }
        )

    max_game = game_overlap.most_common(1)[0] if game_overlap else (None, 0)
    max_leg = directional_leg_overlap.most_common(1)[0] if directional_leg_overlap else (None, 0)
    volume_summary = summarize(candidate)
    return {
        "definition": f"cross-game YES execution price < {price_ceiling:.2f}",
        "fills": len(candidate),
        "combos": len(positions),
        "contracts": volume_summary["contracts"],
        "equal_combo_average_yes_price": mean(row["yes_price"] for row in positions),
        "full_yes_settlements": sum(row["settlement_value"] == 1 for row in positions),
        "full_yes_hit_rate": mean(row["settlement_value"] == 1 for row in positions),
        "any_positive_settlement_rate": mean(
            row["settlement_value"] > 0 for row in positions
        ),
        "payoff_weighted_realized_settlement": mean(
            row["settlement_value"] for row in positions
        ),
        "equal_combo_gross_per_contract": mean(row["gross"] for row in positions),
        "equal_combo_net_per_contract": mean(row["net"] for row in positions),
        "hypothetical_equal_combo_net_pnl": total_net,
        "hypothetical_gains": sum(max(row["net"], 0) for row in positions),
        "hypothetical_losses": sum(min(row["net"], 0) for row in positions),
        "volume_weighted": volume_summary,
        "largest_losses": largest_losses,
        "overlap": {
            "leg_slots": leg_slots,
            "unique_directional_legs": len(directional_leg_overlap),
            "max_game": {
                "game": max_game[0],
                "combos": max_game[1],
                "share": max_game[1] / len(positions) if positions else None,
            },
            "max_directional_leg": {
                **(directional_leg_details.get(max_leg[0], {})),
                "combos": max_leg[1],
                "share": max_leg[1] / len(positions) if positions else None,
            },
        },
        "game_pnl": {
            "allocation": "each combo's net P&L split equally across its games",
            "by_game": game_rows,
            "leave_one_game_out": leave_one_game_out,
        },
    }


def game_paths(processed_dir: Path):
    paths = {}
    for path in sorted(processed_dir.glob("*.parquet")):
        batch = next(pq.ParquetFile(path).iter_batches(batch_size=1, columns=["game"]), None)
        if batch is not None:
            paths[batch.column("game")[0].as_py()] = path
    return paths


def executable_leg_quotes(fill_rows, legs, processed_dir: Path):
    legs_by_combo = defaultdict(list)
    for leg in legs:
        legs_by_combo[leg["combo_market_ticker"]].append(leg)

    queries = defaultdict(list)
    for fill in fill_rows:
        if not fill["kalshi_joinable"]:
            continue
        for leg in legs_by_combo[fill["combo_market_ticker"]]:
            queries[leg["game"]].append(
                {
                    "at": fill["executed_at"],
                    "fill_id": fill["trade_id"],
                    "ticker": leg["underlying_market_ticker"],
                    "side": leg["side"],
                }
            )

    result = {}
    paths = game_paths(processed_dir)
    for game, game_queries in queries.items():
        game_queries.sort(key=lambda row: row["at"])
        wanted = {row["ticker"] for row in game_queries}
        state, query_index = {}, 0

        def resolve(query):
            book = state.get(query["ticker"])
            if not book:
                return
            bid, bid_size, ask, ask_size, observed_at = book
            if query["side"] == "yes":
                price, size = ask, ask_size
            else:
                price, size = (1 - bid if bid is not None else None), bid_size
            if price is not None and size is not None:
                result[(query["fill_id"], query["ticker"])] = {
                    "price": price,
                    "size": size,
                    "age_seconds": (query["at"] - observed_at).total_seconds(),
                }

        path = paths[GAME_ALIASES.get(game, game)]
        columns = [
            "received_at", "event_type", "market_ticker", "yes_bid",
            "yes_bid_size", "yes_ask", "yes_ask_size",
        ]
        for batch in pq.ParquetFile(path).iter_batches(batch_size=131072, columns=columns):
            data = batch.to_pydict()
            for values in zip(*(data[column] for column in columns)):
                row = dict(zip(columns, values))
                while query_index < len(game_queries) and game_queries[query_index]["at"] < row["received_at"]:
                    resolve(game_queries[query_index])
                    query_index += 1
                if row["event_type"] == "top_of_book" and row["market_ticker"] in wanted:
                    state[row["market_ticker"]] = (
                        row["yes_bid"], row["yes_bid_size"], row["yes_ask"],
                        row["yes_ask_size"], row["received_at"],
                    )
        while query_index < len(game_queries):
            resolve(game_queries[query_index])
            query_index += 1
    return result, legs_by_combo


def correlation(xs, ys):
    if len(xs) < 2:
        return None
    xbar, ybar = mean(xs), mean(ys)
    numerator = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
    denominator = math.sqrt(
        sum((x - xbar) ** 2 for x in xs) * sum((y - ybar) ** 2 for y in ys)
    )
    return numerator / denominator if denominator else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--fee-rate", type=float, default=FEE_RATE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--candidate-price-ceiling", type=float, default=0.10)
    args = parser.parse_args()
    combo_dir = args.data_root / "combos"
    combos = pq.read_table(combo_dir / "kalshi_nfl_combos.parquet").to_pylist()
    legs = pq.read_table(combo_dir / "kalshi_nfl_combo_legs.parquet").to_pylist()
    activity = pq.read_table(combo_dir / "kalshi_nfl_combo_activity.parquet").to_pylist()
    combo_lookup = {row["combo_market_ticker"]: row for row in combos}

    fills = []
    for row in activity:
        if row["activity_type"] != "trade":
            continue
        combo = combo_lookup[row["combo_market_ticker"]]
        price, size = row["yes_price"], row["size"]
        settlement = combo["settlement_value"]
        fee = args.fee_rate * size * price * (1 - price)
        gross = size * (price - settlement)
        fills.append(
            {
                "combo_market_ticker": row["combo_market_ticker"],
                "trade_id": row["activity_id"],
                "executed_at": row["activity_at"],
                "size": size,
                "yes_price": price,
                "no_entry_price": 1 - price,
                "settlement_value": settlement,
                "leg_count": combo["leg_count"],
                "scope": combo["scope"],
                "games": combo["games"],
                "taker_side": row["taker_side"],
                "kalshi_joinable": row["kalshi_joinable"],
                "gross_pnl_per_contract": price - settlement,
                "estimated_fee": fee,
                "estimated_fee_per_contract": fee / size,
                "net_pnl_per_contract": (gross - fee) / size,
                "gross_pnl": gross,
                "net_pnl": gross - fee,
                "leg_ask_product": None,
                "combo_premium_to_leg_product": None,
                "max_leg_quote_age_seconds": None,
                "independent_min_size": None,
                "independent_full_size": None,
            }
        )

    leg_quotes, legs_by_combo = executable_leg_quotes(
        fills, legs, args.data_root / "processed" / "kalshi"
    )
    for fill in fills:
        quotes = [
            leg_quotes.get((fill["trade_id"], leg["underlying_market_ticker"]))
            for leg in legs_by_combo[fill["combo_market_ticker"]]
        ]
        if quotes and all(quote is not None for quote in quotes):
            product = math.prod(quote["price"] for quote in quotes)
            fill["leg_ask_product"] = product
            fill["combo_premium_to_leg_product"] = fill["yes_price"] - product
            fill["max_leg_quote_age_seconds"] = max(quote["age_seconds"] for quote in quotes)
            fill["independent_min_size"] = min(quote["size"] for quote in quotes)
            fill["independent_full_size"] = fill["independent_min_size"] >= fill["size"]

    output = args.output or combo_dir / "kalshi_nfl_combo_economics.parquet"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    pq.write_table(pa.Table.from_pylist(fills), temporary, compression="zstd")
    os.replace(temporary, output)

    breakouts = {}
    for field in ("leg_count", "scope"):
        for value in sorted({row[field] for row in fills}, key=str):
            breakouts[f"{field}={value}"] = summarize([row for row in fills if row[field] == value])
    buckets = ((0, .10, "0-10c"), (.10, .25, "10-25c"), (.25, .50, "25-50c"), (.50, 1.01, "50-100c"))
    for low, high, label in buckets:
        breakouts[f"price={label}"] = summarize(
            [row for row in fills if low <= row["yes_price"] < high]
        )

    covered = [row for row in fills if row["leg_ask_product"] is not None]
    premium_by_combo = []
    for ticker, group in sorted(group_by_combo(covered).items()):
        premium_by_combo.append(
            {
                "premium": average(group, "combo_premium_to_leg_product", "size"),
                "gross": average(group, "gross_pnl_per_contract", "size"),
                "net": average(group, "net_pnl_per_contract", "size"),
                "opposite_won": group[0]["settlement_value"] < 1,
            }
        )
    premium_buckets = []
    ordered_premiums = sorted(premium_by_combo, key=lambda row: row["premium"])
    if ordered_premiums:
        for index in range(4):
            start = index * len(ordered_premiums) // 4
            end = (index + 1) * len(ordered_premiums) // 4
            group = ordered_premiums[start:end]
            premium_buckets.append(
                {
                    "bucket": index + 1,
                    "combos": len(group),
                    "mean_premium": mean(row["premium"] for row in group),
                    "opposite_win_rate": mean(row["opposite_won"] for row in group),
                    "mean_gross_per_contract": mean(row["gross"] for row in group),
                    "mean_net_per_contract": mean(row["net"] for row in group),
                }
            )

    games = sorted({game for combo in combos for game in combo["games"].split(", ")})
    leave_one_game_out = {
        game: summarize([row for row in fills if game not in row["games"].split(", ")])
        for game in games
    }
    summary = {
        "output": str(output),
        "overall": summarize(fills),
        "breakouts": breakouts,
        "covered_leg_product": {
            "fills": len(covered),
            "combos": len({row["combo_market_ticker"] for row in covered}),
            "full_size_fills": sum(bool(row["independent_full_size"]) for row in covered),
            "median_max_quote_age_seconds": median(row["max_leg_quote_age_seconds"] for row in covered)
            if covered else None,
            "equal_combo_mean_premium": mean(row["premium"] for row in premium_by_combo)
            if premium_by_combo else None,
            "premium_pnl_correlation": correlation(
                [row["premium"] for row in premium_by_combo],
                [row["gross"] for row in premium_by_combo],
            ),
            "premium_buckets": premium_buckets,
        },
        "leave_one_game_out_net_range": [
            min(row["equal_combo_net_per_contract"] for row in leave_one_game_out.values()),
            max(row["equal_combo_net_per_contract"] for row in leave_one_game_out.values()),
        ],
        "short_yes_candidate_risk": short_yes_risk_audit(
            fills, legs, args.candidate_price_ceiling
        ),
    }
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
