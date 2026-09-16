"""Fit fixed ridge markout models and evaluate once on held-out DAL-NYG."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


ROOT = Path(__file__).resolve().parents[1]

from helpers import (  # noqa: E402
    annotate_bovada_payloads,
    bovada_price_events,
    read_mapping,
    scan_kalshi_horizons,
)
from rank import add_neighbor_tickers, score_event  # noqa: E402


HOLDOUT_GAME = "DAL @ NYG"
HORIZONS = (10, 30)
RIDGE_ALPHA = 1.0
FEATURES = (
    "gross_disagreement",
    "bovada_move",
    "kalshi_spread",
    "log_available_size",
    "log_quote_age",
    "bovada_fair_probability",
    "kalshi_executable_price",
    "log_repricing_selection_count",
    "repricing_game_wide",
    "repricing_small_batch",
    "ladder_violation",
    "ladder_unavailable",
    "entry_no",
    "rushing_yards",
)
FEATURE_SOURCES = {
    "gross_disagreement",
    "bovada_move",
    "kalshi_spread",
    "kalshi_available_size",
    "kalshi_quote_age_seconds",
    "bovada_fair_probability",
    "kalshi_executable_price",
    "repricing_selection_count",
    "repricing_scope",
    "ladder_check",
    "entry_side",
    "prop_type",
}
FORBIDDEN_FEATURES = {
    "historical_markout_10s",
    "historical_markout_30s",
    "quote_available_seconds_30s",
    "min_size_while_available",
    "tier_history_10s_n",
    "tier_history_10s_mean",
    "tier_history_10s_median",
    "tier_history_10s_hit_rate",
    "tier_history_10s_adverse_rate",
    "tier_history_30s_n",
    "tier_history_30s_mean",
    "tier_history_30s_median",
    "tier_history_30s_hit_rate",
    "tier_history_30s_adverse_rate",
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=ROOT / "data/sunday_2026-09-13"
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=ROOT / "model_output/player_prop_markout_v1",
    )
    return parser.parse_args()


def build_rows(data_root, games=None, require_future_isolation=True):
    processed = data_root / "processed"
    mapping = read_mapping(data_root / "mappings/bovada_kalshi_props.parquet")
    ranker_args = SimpleNamespace(
        min_residual=0.05,
        max_spread=0.05,
        min_size=50,
        taker_fee_rate=0.07,
        slippage=0.01,
    )
    rows = []
    for bovada_path in sorted((processed / "bovada").glob("*.parquet")):
        game = pq.read_table(bovada_path, columns=["game"]).column("game")[0].as_py()
        if games and game not in games:
            continue
        slug = bovada_path.stem.removeprefix("bovada_")
        events, _ = bovada_price_events(
            bovada_path,
            mapping.get(game, {}),
            min_move=0.02,
            require_future_isolation=require_future_isolation,
        )
        annotate_bovada_payloads(bovada_path, events)
        add_neighbor_tickers(events, mapping.get(game, {}))
        scan_kalshi_horizons(
            processed / "kalshi" / f"kalshi_{slug}.parquet", events, HORIZONS
        )
        rows.extend(
            score_event(event, ranker_args)
            for event in events
            if "initial_price" in event
        )
    return rows


def feature_row(row):
    return [
        row["gross_disagreement"],
        row["bovada_move"],
        row["kalshi_spread"],
        math.log1p(row["kalshi_available_size"]),
        math.log1p(max(0.0, row["kalshi_quote_age_seconds"])),
        row["bovada_fair_probability"],
        row["kalshi_executable_price"],
        math.log1p(row["repricing_selection_count"]),
        float(row["repricing_scope"] == "game_wide"),
        float(row["repricing_scope"] == "small_batch"),
        float(row["ladder_check"] == "violation"),
        float(row["ladder_check"] == "unavailable"),
        float(row["entry_side"] == "no"),
        float(row["prop_type"] == "rushing_yards"),
    ]


def leakage_audit(rows, train_rows, test_rows):
    leaked = sorted(FEATURE_SOURCES & FORBIDDEN_FEATURES)
    train_games = sorted({row["game"] for row in train_rows})
    test_games = sorted({row["game"] for row in test_rows})
    audit = {
        "passed": not leaked and HOLDOUT_GAME not in train_games
        and len(train_games) == 12 and test_games == [HOLDOUT_GAME],
        "feature_allowlist": list(FEATURES),
        "feature_source_columns": sorted(FEATURE_SOURCES),
        "forbidden_future_columns": sorted(FORBIDDEN_FEATURES),
        "leaked_feature_columns": leaked,
        "labels_only": [f"historical_markout_{horizon}s" for horizon in HORIZONS],
        "split_before_scaling": True,
        "training_games": train_games,
        "holdout_games": test_games,
        "training_rows": len(train_rows),
        "holdout_rows": len(test_rows),
        "source_rows": len(rows),
    }
    if not audit["passed"]:
        raise RuntimeError(f"leakage audit failed: {audit}")
    return audit


def fit_ridge(x, y):
    means = x.mean(axis=0)
    scales = x.std(axis=0)
    scales[scales == 0] = 1.0
    z = (x - means) / scales
    design = np.column_stack([np.ones(len(z)), z])
    penalty = np.eye(design.shape[1]) * RIDGE_ALPHA
    penalty[0, 0] = 0.0
    coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    return {
        "intercept": float(coefficients[0]),
        "coefficients": coefficients[1:],
        "means": means,
        "scales": scales,
    }


def predict(model, x):
    return model["intercept"] + ((x - model["means"]) / model["scales"]) @ model["coefficients"]


def correlation(left, right):
    return float(np.corrcoef(left, right)[0, 1]) if np.std(left) and np.std(right) else None


def movement_summary(values):
    values = np.asarray(values)
    return {
        "n": len(values),
        "mean": float(values.mean()) if len(values) else None,
        "median": float(np.median(values)) if len(values) else None,
        "hit_rate": float((values > 0).mean()) if len(values) else None,
        "adverse_rate": float((values < 0).mean()) if len(values) else None,
    }


def regression_summary(actual, predicted):
    error = predicted - actual
    return {
        "n": len(actual),
        "mae": float(np.abs(error).mean()),
        "rmse": float(np.sqrt((error ** 2).mean())),
        "bias": float(error.mean()),
        "correlation": correlation(actual, predicted),
        "zero_baseline_mae": float(np.abs(actual).mean()),
        "zero_baseline_rmse": float(np.sqrt((actual ** 2).mean())),
    }


def json_model(models, audit):
    return {
        "model": "standardized ridge regression",
        "ridge_alpha_fixed_before_holdout": RIDGE_ALPHA,
        "holdout_game": HOLDOUT_GAME,
        "leakage_audit": audit,
        "horizons": {
            str(horizon): {
                "intercept": model["intercept"],
                "features": [
                    {
                        "name": name,
                        "standardized_coefficient": float(coefficient),
                        "training_mean": float(mean),
                        "training_scale": float(scale),
                    }
                    for name, coefficient, mean, scale in zip(
                        FEATURES,
                        model["coefficients"],
                        model["means"],
                        model["scales"],
                    )
                ],
            }
            for horizon, model in models.items()
        },
    }


def main():
    args = parse_args()
    rows = build_rows(args.data_root)
    train_rows = [row for row in rows if row["game"] != HOLDOUT_GAME]
    test_rows = [row for row in rows if row["game"] == HOLDOUT_GAME]
    audit = leakage_audit(rows, train_rows, test_rows)

    x_train = np.asarray([feature_row(row) for row in train_rows], dtype=float)
    x_test = np.asarray([feature_row(row) for row in test_rows], dtype=float)
    models = {}
    for horizon in HORIZONS:
        labels = np.asarray(
            [row[f"historical_markout_{horizon}s"] for row in train_rows],
            dtype=float,
        )
        models[horizon] = fit_ridge(x_train, labels)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    frozen = json_model(models, audit)
    model_bytes = json.dumps(frozen, indent=2, sort_keys=True).encode()
    (args.output_dir / "model.json").write_bytes(model_bytes)
    model_hash = hashlib.sha256(model_bytes).hexdigest()

    predictions = {horizon: predict(models[horizon], x_test) for horizon in HORIZONS}
    ranker_indexes = [
        index for index, row in enumerate(test_rows)
        if row["recommendation"] in {"strong", "enter"}
    ]
    comparison = {}
    regression = {}
    for horizon in HORIZONS:
        actual = np.asarray(
            [row[f"historical_markout_{horizon}s"] for row in test_rows],
            dtype=float,
        )
        predicted = predictions[horizon]
        top_indexes = np.argsort(predicted)[::-1][:len(ranker_indexes)]
        positive_indexes = np.flatnonzero(predicted > 0)
        regression[str(horizon)] = regression_summary(actual, predicted)
        comparison[str(horizon)] = {
            "all_holdout": movement_summary(actual),
            "rule_ranker_strong_or_enter": movement_summary(actual[ranker_indexes]),
            "model_top_k_equal_to_rule_count": movement_summary(actual[top_indexes]),
            "model_predicted_positive": movement_summary(actual[positive_indexes]),
            "model_positive_count": len(positive_indexes),
        }

    prediction_rows = []
    for index, row in enumerate(test_rows):
        prediction_rows.append({
            "game": row["game"],
            "player": row["player"],
            "prop_type": row["prop_type"],
            "threshold": row["threshold"],
            "entry_side": row["entry_side"],
            "bovada_repriced_at": row["bovada_repriced_at"],
            "ranker_recommendation": row["recommendation"],
            "ranker_signal_score": row["signal_score"],
            "predicted_markout_10s": float(predictions[10][index]),
            "actual_markout_10s": row["historical_markout_10s"],
            "predicted_markout_30s": float(predictions[30][index]),
            "actual_markout_30s": row["historical_markout_30s"],
        })
    pq.write_table(
        pa.Table.from_pylist(prediction_rows),
        args.output_dir / "dal_nyg_predictions.parquet",
        compression="zstd",
    )
    evaluation = {
        "frozen_model_sha256": model_hash,
        "holdout_game": HOLDOUT_GAME,
        "leakage_audit": audit,
        "regression": regression,
        "selection_comparison": comparison,
    }
    (args.output_dir / "evaluation.json").write_text(
        json.dumps(evaluation, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(evaluation, sort_keys=True))


if __name__ == "__main__":
    main()
