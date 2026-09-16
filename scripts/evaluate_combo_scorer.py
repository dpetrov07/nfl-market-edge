"""Evaluate the frozen combo scorer on one or more settled slate feature files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
from nfl_market_edge.combo import score_snapshot


DEFAULT_INPUT = ROOT / "research/output/sunday_combo_2026-09-13/fair_value/quality_fill_values.parquet"
DEFAULT_OUTPUT = ROOT / "research/output/sunday_combo_2026-09-13/fair_value/scorer_evaluation.csv"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        action="append",
        default=None,
        help="Settled combo feature Parquet; repeat for future slates",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--scored-output", type=Path)
    return parser.parse_args()


def score_rows(path: Path):
    data = pd.read_parquet(path).copy()
    required = {
        "combo_market_ticker",
        "trade_id",
        "yes_price",
        "settlement_value",
        "net_pnl",
        "size",
        "leg_count",
        "distinct_leg_games",
        "component_bid_product",
        "component_mid_product",
        "component_ask_product",
        "max_quote_age_seconds",
        "max_leg_spread",
    }
    missing = sorted(required - set(data.columns))
    if missing:
        raise ValueError(f"{path} is missing columns: {', '.join(missing)}")
    label = str(data.slate_id.iloc[0]) if "slate_id" in data else path.parents[1].name
    scores = []
    for row in data.itertuples(index=False):
        optional = lambda value: None if pd.isna(value) else value
        score = score_snapshot(
            yes_price=row.yes_price,
            scope=getattr(row, "scope", "cross_game"),
            leg_count=row.leg_count,
            distinct_leg_games=row.distinct_leg_games,
            component_bid_product=optional(row.component_bid_product),
            component_mid_product=optional(row.component_mid_product),
            component_ask_product=optional(row.component_ask_product),
            max_quote_age_seconds=optional(row.max_quote_age_seconds),
            max_leg_spread=optional(row.max_leg_spread),
            combo_bid=optional(getattr(row, "combo_bid", None)),
            combo_ask=optional(getattr(row, "combo_ask", None)),
            combo_quote_age_seconds=optional(
                getattr(row, "combo_quote_age_seconds", None)
            ),
            available_size=optional(getattr(row, "available_size", None)),
        )
        scores.append(
            {
                "slate": label,
                "combo_market_ticker": row.combo_market_ticker,
                "trade_id": row.trade_id,
                "settlement": row.settlement_value,
                "net_pnl": row.net_pnl,
                "size": row.size,
                **{
                    key: score[key]
                    for key in (
                        "fair_value",
                        "fair_value_low",
                        "fair_value_high",
                        "fair_value_method",
                        "structural_seller_edge_after_fee",
                        "expected_net_edge",
                        "historical_net_edge_prior",
                        "recommendation",
                        "worth_considering",
                        "reason",
                        "available_size",
                    )
                },
                "confidence": score["uncertainty"]["confidence"],
                "component_quote_quality_pass": score["uncertainty"][
                    "quote_quality_pass"
                ],
                "combo_book_quality_pass": score["uncertainty"][
                    "combo_book_quality_pass"
                ],
            }
        )
    return pd.DataFrame(scores)


def summarize(data: pd.DataFrame, label: str, selected: pd.Series):
    sample = data[selected].copy()
    if sample.empty:
        return {
            "slate": data.slate.iloc[0],
            "sample": label,
            "fills": 0,
            "combos": 0,
            "contracts": 0.0,
            "equal_combo_realized_net": None,
            "volume_weighted_realized_net": None,
            "mean_fair_value": None,
            "mean_structural_edge": None,
            "mean_expected_net_edge": None,
            "loss_rate": None,
        }
    weighted_columns = [
        "fair_value",
        "structural_seller_edge_after_fee",
        "expected_net_edge",
    ]
    for column in weighted_columns:
        sample[column + "_notional"] = sample[column] * sample["size"]
    grouped = sample.groupby("combo_market_ticker")
    positions = grouped.agg(contracts=("size", "sum"), net_pnl=("net_pnl", "sum"))
    positions["realized_net_per_contract"] = positions.net_pnl / positions.contracts
    for column in weighted_columns:
        positions[column] = grouped[column + "_notional"].sum(
            min_count=1
        ) / positions.contracts
    contracts = positions.contracts.sum()
    return {
        "slate": data.slate.iloc[0],
        "sample": label,
        "fills": int(len(sample)),
        "combos": int(len(positions)),
        "contracts": float(contracts),
        "equal_combo_realized_net": float(positions.realized_net_per_contract.mean()),
        "volume_weighted_realized_net": float(
            positions.net_pnl.sum() / contracts
        ),
        "mean_fair_value": float(positions.fair_value.mean()),
        "mean_structural_edge": float(
            positions.structural_seller_edge_after_fee.mean()
        ),
        "mean_expected_net_edge": (
            float(positions.expected_net_edge.mean())
            if sample.expected_net_edge.notna().all()
            else None
        ),
        "loss_rate": float((positions.realized_net_per_contract < 0).mean()),
    }


def main():
    args = parse_args()
    inputs = args.input or [DEFAULT_INPUT]
    scored = pd.concat([score_rows(path) for path in inputs], ignore_index=True)
    summaries = []
    for _, slate in scored.groupby("slate", sort=True):
        summaries.extend(
            [
                summarize(slate, "all_component_quality", pd.Series(True, index=slate.index)),
                summarize(
                    slate,
                    "frozen_below_10c",
                    slate.historical_net_edge_prior.notna(),
                ),
                summarize(
                    slate,
                    "operational_consider",
                    slate.worth_considering,
                ),
                summarize(
                    slate,
                    "positive_structural_edge",
                    slate.structural_seller_edge_after_fee > 0,
                ),
            ]
        )
    result = pd.DataFrame(summaries)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(args.output, index=False)
    if args.scored_output:
        args.scored_output.parent.mkdir(parents=True, exist_ok=True)
        scored.to_parquet(args.scored_output, index=False)
    print(json.dumps({"inputs": len(inputs), "output": str(args.output), "rows": len(result)}))


if __name__ == "__main__":
    main()
