"""Freeze v2 on all Sunday games; future games are the untouched holdout."""

from __future__ import annotations

import hashlib
import json

import numpy as np

from model import (
    FEATURES,
    FORBIDDEN_FEATURES,
    HORIZONS,
    RIDGE_ALPHA,
    ROOT,
    build_rows,
    feature_row,
    fit_ridge,
)


V1 = ROOT / "model_output/player_prop_markout_v1/model.json"
OUTPUT = ROOT / "model_output/player_prop_markout_v2/model.json"


def main():
    v1_hash = hashlib.sha256(V1.read_bytes()).hexdigest()
    rows = build_rows(ROOT / "data/sunday_2026-09-13")
    x = np.asarray([feature_row(row) for row in rows], dtype=float)
    models = {
        horizon: fit_ridge(
            x,
            np.asarray(
                [row[f"historical_markout_{horizon}s"] for row in rows],
                dtype=float,
            ),
        )
        for horizon in HORIZONS
    }
    frozen = {
        "model": "standardized ridge regression",
        "version": 2,
        "ridge_alpha": RIDGE_ALPHA,
        "training_rows": len(rows),
        "training_games": sorted({row["game"] for row in rows}),
        "training_data_end": "2026-09-13",
        "future_games_are_untouched_holdout": True,
        "preserved_v1_sha256": v1_hash,
        "features": list(FEATURES),
        "excluded_future_columns": sorted(FORBIDDEN_FEATURES),
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
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(frozen, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "output": str(OUTPUT),
        "training_rows": len(rows),
        "training_games": len(frozen["training_games"]),
        "preserved_v1_sha256": v1_hash,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
