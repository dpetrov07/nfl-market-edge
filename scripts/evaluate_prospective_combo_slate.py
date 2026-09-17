"""Turn a captured combo slate into frozen-scorer features and one evaluation."""

from __future__ import annotations

import argparse
import gzip
import json
import math
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
from nfl_market_edge.combo import fee_per_contract
from scripts.collect_live_combo_slate import event_key, read_manifest
from scripts.evaluate_combo_scorer import score_rows, summarize


def timestamp(value):
    return pd.to_datetime(value, utc=True) if value else None


def number(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def leg_snapshot(combo: dict, books: dict, at) -> dict:
    legs = combo.get("mve_selected_legs") or []
    values = []
    for leg in legs:
        book = books.get(leg["market_ticker"])
        if not book:
            return {}
        if leg.get("side") == "no":
            bid = 1 - book["yes_ask"] if book["yes_ask"] is not None else None
            ask = 1 - book["yes_bid"] if book["yes_bid"] is not None else None
            bid_size, ask_size = book["yes_ask_size"], book["yes_bid_size"]
        else:
            bid, ask = book["yes_bid"], book["yes_ask"]
            bid_size, ask_size = book["yes_bid_size"], book["yes_ask_size"]
        if bid is None or ask is None:
            return {}
        values.append(
            {
                "bid": bid,
                "mid": (bid + ask) / 2,
                "ask": ask,
                "spread": ask - bid,
                "bid_size": bid_size,
                "ask_size": ask_size,
                "age": (at - book["at"]).total_seconds(),
            }
        )
    if not values:
        return {}
    return {
        "component_bid_product": math.prod(value["bid"] for value in values),
        "component_mid_product": math.prod(value["mid"] for value in values),
        "component_ask_product": math.prod(value["ask"] for value in values),
        "mean_leg_spread": sum(value["spread"] for value in values) / len(values),
        "max_leg_spread": max(value["spread"] for value in values),
        "max_quote_age_seconds": max(value["age"] for value in values),
        "min_component_ask_size": min(
            value["ask_size"] for value in values if value["ask_size"] is not None
        ) if all(value["ask_size"] is not None for value in values) else None,
    }


def activity_snapshot(record: dict, combo: dict, books: dict, event_games: dict) -> dict:
    at = timestamp(record.get("received_at"))
    legs = combo.get("mve_selected_legs") or []
    games = [
        leg.get("game")
        or event_games.get(event_key(leg.get("event_ticker")))
        or leg.get("event_ticker")
        for leg in legs
    ]
    combo_book = books.get(combo["ticker"])
    result = {
        "combo_market_ticker": combo["ticker"],
        "observed_at": at,
        "leg_count": len(legs),
        "distinct_leg_games": len(set(games)),
        "scope": "same_game" if len(set(games)) == 1 else "cross_game",
        "games": ", ".join(sorted(set(games))),
        "combo_bid": combo_book["yes_bid"] if combo_book else None,
        "combo_bid_size": combo_book["yes_bid_size"] if combo_book else None,
        "combo_ask": combo_book["yes_ask"] if combo_book else None,
        "combo_ask_size": combo_book["yes_ask_size"] if combo_book else None,
        "combo_quote_age_seconds": (at - combo_book["at"]).total_seconds() if combo_book else None,
        "component_bid_product": None,
        "component_mid_product": None,
        "component_ask_product": None,
        "mean_leg_spread": None,
        "max_leg_spread": None,
        "max_quote_age_seconds": None,
        "min_component_ask_size": None,
        **leg_snapshot(combo, books, at),
    }
    return result


def load_capture(path: Path, manifest: dict):
    combos, books, settlements = {}, {}, {}
    fills, communications = [], []
    seen_trades = set()
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            kind = record.get("record_type")
            if kind == "combo_discovery":
                combo = record["combo"]
                combos[combo["ticker"]] = combo
            elif kind == "top_of_book":
                ticker = record.get("market_ticker")
                books[ticker] = {
                    "at": timestamp(record.get("received_at")),
                    "yes_bid": number(record.get("yes_bid_dollars")),
                    "yes_bid_size": number(record.get("yes_bid_size")),
                    "yes_ask": number(record.get("yes_ask_dollars")),
                    "yes_ask_size": number(record.get("yes_ask_size")),
                }
            elif kind in {"trade", "fill"} and record.get("market_ticker") in combos:
                trade_id = record.get("trade_id") or record.get("fill_id")
                if trade_id and trade_id in seen_trades:
                    continue
                if trade_id:
                    seen_trades.add(trade_id)
                combo = combos[record["market_ticker"]]
                fills.append(
                    {
                        **activity_snapshot(record, combo, books, manifest["event_games"]),
                        "trade_id": trade_id,
                        "executed_at": timestamp(record.get("received_at")),
                        "exchange_timestamp": record.get("exchange_timestamp"),
                        "size": number(record.get("count")),
                        "yes_price": number(record.get("yes_price_dollars")),
                        "taker_side": record.get("taker_outcome_side")
                        or record.get("side"),
                    }
                )
            elif kind == "communication" and record.get("market_ticker") in combos:
                combo = combos[record["market_ticker"]]
                communications.append(
                    {
                        **activity_snapshot(record, combo, books, manifest["event_games"]),
                        "communication_type": record.get("communication_type"),
                        "exchange_timestamp": record.get("exchange_timestamp"),
                        "rfq_id": record.get("rfq_id"),
                        "quote_id": record.get("quote_id"),
                        "size": number(record.get("contracts")),
                        "yes_size": number(record.get("yes_contracts")),
                        "no_size": number(record.get("no_contracts")),
                        "accepted_size": number(record.get("contracts_accepted")),
                        "yes_price": number(record.get("yes_bid_dollars")),
                        "no_price": number(record.get("no_bid_dollars")),
                        "target_cost": number(record.get("target_cost_dollars")),
                        "accepted_side": record.get("accepted_side"),
                        "status": record.get("status"),
                    }
                )
            elif kind == "market_status" and record.get("market_ticker") in combos:
                value = number(record.get("settlement_value_dollars"))
                if value is None and record.get("result") in {"yes", "no"}:
                    value = float(record["result"] == "yes")
                if value is not None:
                    settlements[record["market_ticker"]] = value
    return fills, communications, settlements


def finish_fills(fills: list[dict], settlements: dict) -> pd.DataFrame:
    rows = []
    for row in fills:
        settlement = settlements.get(row["combo_market_ticker"])
        price, size = row["yes_price"], row["size"]
        if settlement is None or price is None or size is None:
            continue
        gross_per_contract = price - settlement
        fee = fee_per_contract(price)
        component_size = row.get("min_component_ask_size")
        combo_size = row.get("combo_ask_size")
        rows.append(
            {
                **row,
                "settlement_value": settlement,
                "no_entry_price": 1 - price,
                "gross_pnl_per_contract": gross_per_contract,
                "estimated_fee_per_contract": fee,
                "net_pnl_per_contract": gross_per_contract - fee,
                "gross_pnl": size * gross_per_contract,
                "estimated_fee": size * fee,
                "net_pnl": size * (gross_per_contract - fee),
                "realized_net_per_contract": gross_per_contract - fee,
                "component_full_size": component_size is not None
                and component_size >= size,
                "combo_book_full_size": combo_size is not None and combo_size >= size,
            }
        )
    return pd.DataFrame(rows)


def evaluate(data_path: Path, output: Path, scored_output: Path) -> pd.DataFrame:
    scored = score_rows(data_path)
    summaries = []
    for _, slate in scored.groupby("slate", sort=True):
        quality = slate.component_quote_quality_pass
        summaries.extend(
            [
                summarize(slate, "all_settled_cross_game", pd.Series(True, index=slate.index)),
                summarize(slate, "frozen_below_10c", slate.historical_net_edge_prior.notna()),
                summarize(slate, "component_quote_quality", quality),
                summarize(slate, "operational_consider", slate.worth_considering),
            ]
        )
    result = pd.DataFrame(summaries)
    result.to_csv(output, index=False)
    scored.to_parquet(scored_output, index=False)
    return result


def select_scope(settled: pd.DataFrame, configured_scope: str) -> pd.DataFrame:
    if configured_scope == "any":
        return settled.copy()
    return settled[settled.scope == configured_scope].copy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = read_manifest(args.manifest)
    raw = args.input or (
        ROOT
        / "data/live/combo_slates"
        / manifest["slate_id"]
        / "kalshi"
        / "events.jsonl.gz"
    )
    output = args.output_dir or ROOT / "research/output" / manifest["slate_id"] / "prospective"
    output.mkdir(parents=True, exist_ok=True)
    fills, communications, settlements = load_capture(raw, manifest)
    settled = finish_fills(fills, settlements)
    if settled.empty:
        raise SystemExit("no settled combo fills in the capture; keep collecting through settlement")
    configured_scope = manifest.get("combo_scope", "cross_game")
    selected = select_scope(settled, configured_scope)
    if selected.empty:
        raise SystemExit(f"no settled {configured_scope} combo fills in the capture")
    selected["slate_id"] = manifest["slate_id"]
    fill_path = output / "fill_values.parquet"
    selected.to_parquet(fill_path, index=False)
    pd.DataFrame(communications).to_parquet(output / "communication_values.parquet", index=False)
    if configured_scope == "cross_game":
        result = evaluate(
            fill_path,
            output / "scorer_evaluation.csv",
            output / "scored_fills.parquet",
        )
    else:
        result = pd.DataFrame()
    summary = {
        "slate_id": manifest["slate_id"],
        "scope": configured_scope,
        "raw_capture": str(raw),
        "captured_combo_fills": len(fills),
        "settled_scope_fills": len(selected),
        "settled_combos": int(selected.combo_market_ticker.nunique()),
        "communications": len(communications),
        "settled_combo_markets": len(settlements),
        "evaluation": result.to_dict("records"),
        "scorer_note": (
            None
            if configured_scope == "cross_game"
            else "frozen scorer is cross-game-only; same-game fills are retained without a score"
        ),
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print(json.dumps({key: value for key, value in summary.items() if key != "evaluation"}))


if __name__ == "__main__":
    main()
