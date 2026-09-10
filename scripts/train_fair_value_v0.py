"""Train the first football-only in-game player-prop fair-value model."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier


FEATURES = [
    "prop_receiving",
    "threshold",
    "yards_so_far",
    "yards_to_threshold",
    "game_seconds_remaining",
    "quarter",
    "player_score_differential",
    "targets_so_far",
    "receptions_so_far",
    "carries_so_far",
    "target_share_so_far",
    "carry_share_so_far",
    "team_pass_attempts_so_far",
    "team_rush_attempts_so_far",
    "team_offensive_plays_so_far",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("data/processed/kalshi_pbp_pilot_audited.parquet"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("models/nfl_fair_value_v0.joblib")
    )
    return parser.parse_args()


def model_frame(data: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    frame = data[
        data.during_actual_game
        & data.timing_safe
        & data.quote_usable
        & data.actual_result.notna()
        & data.player_id.notna()
        & data.prop_type.isin(["receiving_yards", "rushing_yards"])
    ].copy()
    frame["prop_receiving"] = frame.prop_type.eq("receiving_yards").astype(float)
    frame["threshold_cleared"] = frame.yards_so_far.gt(frame.threshold).astype(float)
    home_team = frame.game_id.str.split("_").str[-1]
    frame["player_score_differential"] = frame.score_differential.where(
        frame.team.eq(home_team), -frame.score_differential
    )
    frame[FEATURES] = frame[FEATURES].apply(pd.to_numeric, errors="coerce").fillna(0)
    frame["target"] = frame.actual_result.gt(frame.threshold).astype(int)
    return frame, frame.target


def models() -> dict[str, object]:
    return {
        "logistic": make_pipeline(
            StandardScaler(), LogisticRegression(max_iter=1000, C=1.0)
        ),
        "xgboost": XGBClassifier(
            n_estimators=180,
            max_depth=3,
            learning_rate=0.05,
            min_child_weight=10,
            subsample=0.85,
            colsample_bytree=0.9,
            reg_lambda=5.0,
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=42,
            n_jobs=4,
        ),
    }


def calibration_error(y: np.ndarray, probability: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    bucket = np.clip(np.digitize(probability, edges[1:-1]), 0, bins - 1)
    error = 0.0
    for index in range(bins):
        selected = bucket == index
        if selected.any():
            error += selected.mean() * abs(probability[selected].mean() - y[selected].mean())
    return float(error)


def metrics(y: np.ndarray, probability: np.ndarray) -> dict[str, float]:
    probability = np.clip(probability, 1e-6, 1 - 1e-6)
    logit = np.log(probability / (1 - probability)).reshape(-1, 1)
    calibration = LogisticRegression(C=1e6).fit(logit, y)
    return {
        "brier": brier_score_loss(y, probability),
        "log_loss": log_loss(y, probability),
        "ece_10": calibration_error(y, probability),
        "calibration_intercept": float(calibration.intercept_[0]),
        "calibration_slope": float(calibration.coef_[0, 0]),
    }


def calibration_table(y: np.ndarray, probability: np.ndarray) -> pd.DataFrame:
    table = pd.DataFrame({"actual": y, "probability": probability})
    table["bin"] = pd.qcut(table.probability, 10, duplicates="drop")
    return table.groupby("bin", observed=True).agg(
        rows=("actual", "size"), predicted=("probability", "mean"), actual=("actual", "mean")
    ).reset_index(drop=True)


def main() -> None:
    args = parse_args()
    raw = pd.read_parquet(args.data)
    frame, target = model_frame(raw)
    frame = (
        frame[~frame.threshold_cleared.astype(bool)]
        .sort_values("timestamp")
        .drop_duplicates(["market_id", "play_id"], keep="first")
        .reset_index(drop=True)
    )
    target = frame.target
    x = frame[FEATURES]
    y = target.to_numpy()
    games = sorted(frame.game_id.unique())
    results = {}
    probabilities = {}
    per_game_rows = []

    for name in models():
        oof = np.zeros(len(frame))
        for game_id in games:
            train = frame.game_id.ne(game_id).to_numpy()
            valid = ~train
            model = models()[name]
            model.fit(x.loc[train], y[train])
            oof[valid] = model.predict_proba(x.loc[valid])[:, 1]
            score = metrics(y[valid], oof[valid])
            per_game_rows.append({"model": name, "game": game_id, **score})
        probabilities[name] = oof
        results[name] = metrics(y, oof)

    comparison = pd.DataFrame(results).T.sort_values(["brier", "log_loss"])
    winner = comparison.index[0]
    final_model = models()[winner]
    final_model.fit(x, y)

    if winner == "logistic":
        coefficients = final_model.named_steps["logisticregression"].coef_[0]
        importance = pd.Series(abs(coefficients), index=FEATURES).sort_values(ascending=False)
    else:
        importance = pd.Series(
            final_model.feature_importances_, index=FEATURES
        ).sort_values(ascending=False)

    bundle = {
        "model_name": winner,
        "model": final_model,
        "features": FEATURES,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_rows": len(frame),
        "training_markets": frame.market_id.nunique(),
        "training_games": games,
        "validation": results,
        "top_features": importance.to_dict(),
        "target": "actual_result > threshold",
        "training_scope": "unresolved unique market/play states",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, args.output)

    print(
        f"TRAINING: {len(frame):,} unresolved unique market/play states | "
        f"{frame.market_id.nunique()} markets | {len(games)} games"
    )
    print("\nLEAVE-ONE-GAME-OUT RESULTS")
    print(comparison.round(4).to_string())
    print("\nPER-GAME BRIER")
    print(pd.DataFrame(per_game_rows).pivot(index="game", columns="model", values="brier").round(4).to_string())
    print(f"\nWINNER: {winner}")
    print("\nTOP FEATURES")
    print(importance.head(10).round(4).to_string())
    print("\nWINNER OOF CALIBRATION")
    print(calibration_table(y, probabilities[winner]).round(3).to_string(index=False))
    print(f"\nSaved {args.output}")


if __name__ == "__main__":
    main()
