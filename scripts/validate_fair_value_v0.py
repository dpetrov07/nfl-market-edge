"""Focused unresolved-state and held-out-market validation for fair value V0."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, log_loss

from train_fair_value_v0 import FEATURES, calibration_error, metrics, model_frame, models


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("data/processed/kalshi_pbp_pilot_audited.parquet"),
    )
    return parser.parse_args()


def balanced_metrics(frame: pd.DataFrame, probability: str) -> dict[str, float]:
    values = []
    for _, market in frame.groupby("market_id"):
        y = market.target.to_numpy()
        p = np.clip(market[probability].to_numpy(), 1e-6, 1 - 1e-6)
        values.append((brier_score_loss(y, p), log_loss(y, p, labels=[0, 1])))
    return {
        "market_balanced_brier": float(np.mean([value[0] for value in values])),
        "market_balanced_log_loss": float(np.mean([value[1] for value in values])),
    }


def score_row(frame: pd.DataFrame, name: str, probability: str) -> dict[str, object]:
    y = frame.target.to_numpy()
    p = frame[probability].to_numpy()
    return {"predictor": name, **metrics(y, p), **balanced_metrics(frame, probability)}


def edge_results(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for prop in ["all", "receiving_yards", "rushing_yards"]:
        source = frame if prop == "all" else frame[frame.prop_type.eq(prop)]
        for side in ["YES", "NO"]:
            edge_column = f"{side.lower()}_edge"
            ask_column = f"{side.lower()}_ask"
            win_column = "target" if side == "YES" else "no_target"
            for minimum in [0.05, 0.10, 0.15]:
                signals = (
                    source[source[edge_column].ge(minimum)]
                    .sort_values("timestamp")
                    .drop_duplicates("market_id", keep="first")
                )
                cost = signals[ask_column].sum()
                contract_profit = signals[win_column] - signals[ask_column]
                profit = contract_profit.sum()
                game_profit = contract_profit.groupby(signals.game_id).sum()
                rows.append(
                    {
                        "prop": prop,
                        "side": side,
                        "edge": f">={minimum:.0%}",
                        "signals": len(signals),
                        "games": signals.game_id.nunique(),
                        "profitable_games": int(game_profit.gt(0).sum()),
                        "win_rate": signals[win_column].mean(),
                        "avg_model_edge": signals[edge_column].mean(),
                        "avg_ask": signals[ask_column].mean(),
                        "profit_per_contract": profit / len(signals) if len(signals) else np.nan,
                        "roi_on_cost": profit / cost if cost else np.nan,
                    }
                )
    return pd.DataFrame(rows)


def main() -> None:
    args = parse_args()
    frame, _ = model_frame(pd.read_parquet(args.data))
    unresolved_rows = frame[frame.yards_so_far.le(frame.threshold)].copy()
    unresolved = (
        unresolved_rows.sort_values("timestamp")
        .drop_duplicates(["market_id", "play_id"], keep="first")
        .reset_index(drop=True)
    )
    games = sorted(unresolved.game_id.unique())

    for name in ["logistic", "xgboost"]:
        unresolved[name] = 0.0
        for game_id in games:
            train = unresolved.game_id.ne(game_id)
            valid = ~train
            model = models()[name]
            model.fit(unresolved.loc[train, FEATURES], unresolved.loc[train, "target"])
            unresolved.loc[valid, name] = model.predict_proba(
                unresolved.loc[valid, FEATURES]
            )[:, 1]

    unresolved["kalshi"] = unresolved.midpoint
    unresolved["yes_ask_exec"] = unresolved.yes_ask
    unresolved["no_ask"] = 1 - unresolved.yes_bid
    unresolved["yes_ask"] = unresolved["yes_ask_exec"]
    unresolved["no_target"] = 1 - unresolved.target
    unresolved["yes_edge"] = unresolved.logistic - unresolved.yes_ask
    unresolved["no_edge"] = (1 - unresolved.logistic) - unresolved.no_ask

    comparison = pd.DataFrame([
        score_row(unresolved, "logistic", "logistic"),
        score_row(unresolved, "xgboost", "xgboost"),
        score_row(unresolved, "kalshi_midpoint", "kalshi"),
    ])
    by_prop = []
    for prop, group in unresolved.groupby("prop_type"):
        for name, column in [
            ("logistic", "logistic"),
            ("kalshi_midpoint", "kalshi"),
        ]:
            row = score_row(group, name, column)
            row["prop"] = prop
            by_prop.append(row)

    edges = edge_results(unresolved)
    disagreements = (
        unresolved.assign(disagreement=(unresolved.logistic - unresolved.kalshi).abs())
        .sort_values("disagreement", ascending=False)
        .drop_duplicates("market_id")
        .head(12)
    )

    print(
        f"CLEAN SAMPLE: {len(frame):,} quote rows -> {len(unresolved_rows):,} unresolved rows "
        f"-> {len(unresolved):,} unique market/play states | {unresolved.market_id.nunique()} markets"
    )
    print("Features contain no Kalshi prices, actual_result, settlement_result, player identity, or game identity.")
    print("Validation holds out one complete game at a time.")
    print("\nUNRESOLVED HELD-OUT PERFORMANCE")
    print(comparison.round(4).to_string(index=False))
    print("\nRECEIVING VS RUSHING")
    print(pd.DataFrame(by_prop)[[
        "prop", "predictor", "brier", "log_loss", "ece_10",
        "market_balanced_brier", "market_balanced_log_loss",
    ]].round(4).to_string(index=False))
    print("\nFIRST SIGNAL PER MARKET (before fees)")
    print(edges.round(3).to_string(index=False))
    print("\nLARGEST MODEL VS KALSHI MIDPOINT DISAGREEMENTS")
    disagreement_view = disagreements[[
        "game_id", "timestamp", "player", "prop_type", "threshold", "yards_so_far",
        "game_seconds_remaining", "logistic", "kalshi", "yes_bid", "yes_ask",
        "spread", "target",
    ]].copy()
    numeric = disagreement_view.select_dtypes(include="number").columns
    disagreement_view[numeric] = disagreement_view[numeric].round(3)
    print(disagreement_view.to_string(index=False))


if __name__ == "__main__":
    main()
