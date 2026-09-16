"""Timestamp-safe standalone-leg pricing for Kalshi combo fills."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import math
import pandas as pd
import pyarrow.parquet as pq


GAME_ALIASES = {"CLE @ JAC": "CLE @ JAX", "WAS @ PHI": "WSH @ PHI"}


def game_paths(processed_dir: Path):
    paths = {}
    for path in sorted(processed_dir.glob("*.parquet")):
        batch = next(
            pq.ParquetFile(path).iter_batches(batch_size=1, columns=["game"]), None
        )
        if batch is not None:
            paths[batch.column("game")[0].as_py()] = path
    return paths


def reconstruct_leg_quotes(fill_rows, leg_rows, processed_dir: Path):
    """Return the latest standalone bid/ask strictly available at each fill."""
    legs_by_combo = defaultdict(list)
    for leg in leg_rows:
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
    columns = [
        "received_at",
        "event_type",
        "market_ticker",
        "yes_bid",
        "yes_bid_size",
        "yes_ask",
        "yes_ask_size",
    ]
    for game, game_queries in queries.items():
        game_queries.sort(key=lambda row: row["at"])
        wanted = {row["ticker"] for row in game_queries}
        state, query_index = {}, 0

        def resolve(query):
            book = state.get(query["ticker"])
            if not book:
                return
            yes_bid, yes_bid_size, yes_ask, yes_ask_size, observed_at = book
            if query["side"] == "yes":
                bid, ask = yes_bid, yes_ask
                bid_size, ask_size = yes_bid_size, yes_ask_size
            else:
                bid = 1 - yes_ask if yes_ask is not None else None
                ask = 1 - yes_bid if yes_bid is not None else None
                bid_size, ask_size = yes_ask_size, yes_bid_size
            if bid is None and ask is None:
                return
            result[(query["fill_id"], query["ticker"])] = {
                "bid": bid,
                "mid": (bid + ask) / 2 if bid is not None and ask is not None else None,
                "ask": ask,
                "bid_size": bid_size,
                "ask_size": ask_size,
                "spread": ask - bid if bid is not None and ask is not None else None,
                "age_seconds": (query["at"] - observed_at).total_seconds(),
            }

        path = paths[GAME_ALIASES.get(game, game)]
        for batch in pq.ParquetFile(path).iter_batches(
            batch_size=131072, columns=columns
        ):
            data = batch.to_pydict()
            for values in zip(*(data[column] for column in columns)):
                row = dict(zip(columns, values))
                while (
                    query_index < len(game_queries)
                    and game_queries[query_index]["at"] < row["received_at"]
                ):
                    resolve(game_queries[query_index])
                    query_index += 1
                if (
                    row["event_type"] == "top_of_book"
                    and row["market_ticker"] in wanted
                ):
                    state[row["market_ticker"]] = (
                        row["yes_bid"],
                        row["yes_bid_size"],
                        row["yes_ask"],
                        row["yes_ask_size"],
                        row["received_at"],
                    )
        while query_index < len(game_queries):
            resolve(game_queries[query_index])
            query_index += 1
    return result, legs_by_combo


def component_values(fills: pd.DataFrame, legs: pd.DataFrame, processed_dir: Path):
    """Build component bid/mid/ask products for combo fills with full coverage."""
    quotes, legs_by_combo = reconstruct_leg_quotes(
        fills.to_dict("records"), legs.to_dict("records"), processed_dir
    )
    rows = []
    for fill in fills.itertuples(index=False):
        combo_legs = legs_by_combo[fill.combo_market_ticker]
        leg_quotes = [
            quotes.get((fill.trade_id, leg["underlying_market_ticker"]))
            for leg in combo_legs
        ]
        if (
            not combo_legs
            or not all(leg_quotes)
            or any(
                quote["bid"] is None
                or quote["mid"] is None
                or quote["ask"] is None
                or quote["ask_size"] is None
                for quote in leg_quotes
            )
        ):
            continue
        rows.append(
            {
                "trade_id": fill.trade_id,
                "component_bid_product": math.prod(q["bid"] for q in leg_quotes),
                "component_mid_product": math.prod(q["mid"] for q in leg_quotes),
                "component_ask_product": math.prod(q["ask"] for q in leg_quotes),
                "mean_leg_spread": sum(q["spread"] for q in leg_quotes)
                / len(leg_quotes),
                "max_leg_spread": max(q["spread"] for q in leg_quotes),
                "max_quote_age_seconds": max(q["age_seconds"] for q in leg_quotes),
                "min_component_ask_size": min(q["ask_size"] for q in leg_quotes),
                "component_full_size": min(q["ask_size"] for q in leg_quotes)
                >= fill.size,
                "distinct_leg_games": len({leg["game"] for leg in combo_legs}),
            }
        )
    return pd.DataFrame(rows)
