"""Compare cross-game combo prices with timestamp-matched standalone legs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.sep13.fair_value import component_values
from research.sep13.analyze_edges import (
    BLUE,
    GREEN,
    GRID,
    INK,
    MUTED,
    ORANGE,
    RED,
    svg_text,
    write_svg,
)


DEFAULT_ROOT = Path("data/sunday_2026-09-13")
DEFAULT_OUTPUT = Path("research/output/sunday_combo_2026-09-13/fair_value")
FRESH_SECONDS = 30
MAX_LEG_SPREAD = 0.05
VALUE_COLUMNS = [
    "yes_price",
    "component_bid_product",
    "component_mid_product",
    "component_ask_product",
    "mean_leg_spread",
    "max_leg_spread",
    "max_quote_age_seconds",
]


def aggregate_combos(fills: pd.DataFrame):
    data = fills.copy()
    for column in VALUE_COLUMNS:
        if column in data:
            data[column + "_notional"] = data[column] * data["size"]
    aggregations = {
        "fills": ("trade_id", "size"),
        "contracts": ("size", "sum"),
        "settlement": ("settlement_value", "first"),
        "net_pnl": ("net_pnl", "sum"),
        "leg_count": ("leg_count", "first"),
        "games": ("games", "first"),
    }
    if "distinct_leg_games" in data:
        aggregations["distinct_leg_games"] = ("distinct_leg_games", "first")
    for column in VALUE_COLUMNS:
        if column in data:
            aggregations[column] = (column + "_notional", "sum")
    grouped = data.groupby("combo_market_ticker").agg(**aggregations).reset_index()
    for column in VALUE_COLUMNS:
        if column in grouped:
            grouped[column] /= grouped.contracts
    grouped["net_per_contract"] = grouped.net_pnl / grouped.contracts
    if "component_mid_product" in grouped:
        grouped["mid_premium"] = grouped.yes_price - grouped.component_mid_product
        grouped["ask_premium"] = grouped.yes_price - grouped.component_ask_product
        grouped["log_mid_ratio"] = np.log(
            grouped.yes_price.clip(1e-4) / grouped.component_mid_product.clip(1e-4)
        )
        interval = (
            grouped.component_ask_product - grouped.component_bid_product
        ).clip(1e-4)
        grouped["spread_adjusted_premium"] = grouped.mid_premium / interval
    return grouped


def summarize_rule(fills: pd.DataFrame, label: str):
    positions = aggregate_combos(fills)
    contracts = positions.contracts.sum()
    result = {
        "rule": label,
        "fills": int(len(fills)),
        "combos": int(len(positions)),
        "contracts": float(contracts),
        "mean_price": float(positions.yes_price.mean()),
        "mean_settlement": float(positions.settlement.mean()),
        "equal_combo_net_per_contract": float(positions.net_per_contract.mean()),
        "volume_weighted_net_per_contract": float(
            positions.net_pnl.sum() / contracts
        ),
        "losing_combo_rate": float((positions.net_per_contract < 0).mean()),
        "worst_net_per_contract": float(positions.net_per_contract.min()),
    }
    if "component_mid_product" in positions:
        result["mean_component_mid"] = float(positions.component_mid_product.mean())
        result["mean_mid_premium"] = float(positions.mid_premium.mean())
    return result


def auc(y, score):
    y = np.asarray(y) == 1
    positives, negatives = y.sum(), (~y).sum()
    if not positives or not negatives:
        return None
    ranks = pd.Series(score).rank(method="average").to_numpy()
    return float(
        (ranks[y].sum() - positives * (positives + 1) / 2)
        / (positives * negatives)
    )


def forecast_table(quality: pd.DataFrame):
    scopes = {
        "all_quality": quality,
        "2_leg": quality[quality.leg_count == 2],
        "3_leg_distinct_games": quality[
            (quality.leg_count == 3) & (quality.distinct_leg_games == 3)
        ],
        "3_leg_mixed_games": quality[
            (quality.leg_count == 3) & (quality.distinct_leg_games == 2)
        ],
    }
    methods = {
        "combo_trade_price": "yes_price",
        "component_bid_product": "component_bid_product",
        "component_mid_product": "component_mid_product",
        "component_ask_product": "component_ask_product",
    }
    rows = []
    for scope, data in scopes.items():
        binary = data.settlement.isin([0, 1])
        for method, column in methods.items():
            error = data[column] - data.settlement
            rows.append(
                {
                    "sample": scope,
                    "forecast": method,
                    "combos": int(len(data)),
                    "mean_forecast": float(data[column].mean()),
                    "mean_settlement": float(data.settlement.mean()),
                    "bias": float(error.mean()),
                    "brier_score": float((error**2).mean()),
                    "mean_absolute_error": float(error.abs().mean()),
                    "binary_auc": auc(
                        data.loc[binary, "settlement"], data.loc[binary, column]
                    ),
                }
            )
    return pd.DataFrame(rows)


def calibration_table(quality: pd.DataFrame):
    rows = []
    for label, column in {
        "Combo trade price": "yes_price",
        "Component midpoint product": "component_mid_product",
    }.items():
        data = quality[[column, "settlement"]].copy()
        data["bin"] = np.minimum((data[column] * 10).astype(int), 9)
        grouped = data.groupby("bin").agg(
            combos=("settlement", "size"),
            mean_forecast=(column, "mean"),
            mean_settlement=("settlement", "mean"),
        )
        for index, row in grouped.reset_index().iterrows():
            rows.append({"forecast": label, **row.to_dict()})
    return pd.DataFrame(rows)


def premium_tables(quality: pd.DataFrame):
    data = quality.copy()
    data["premium_quintile"] = pd.qcut(
        data.mid_premium, 5, labels=False, duplicates="drop"
    )
    quintiles = (
        data.groupby("premium_quintile")
        .agg(
            combos=("combo_market_ticker", "size"),
            mean_premium=("mid_premium", "mean"),
            mean_combo_price=("yes_price", "mean"),
            mean_component_mid=("component_mid_product", "mean"),
            mean_settlement=("settlement", "mean"),
            equal_combo_net_per_contract=("net_per_contract", "mean"),
        )
        .reset_index()
    )
    quintiles["premium_quintile"] += 1

    data["price_bucket"] = pd.cut(
        data.yes_price,
        [0, 0.10, 0.25, 0.50, 1.001],
        labels=["0–10¢", "10–25¢", "25–50¢", "50–100¢"],
        right=False,
        include_lowest=True,
    )
    data["premium_sign"] = np.where(data.mid_premium > 0, "premium > 0", "premium ≤ 0")
    price_cells = (
        data.groupby(["price_bucket", "premium_sign"], observed=True)
        .agg(
            combos=("combo_market_ticker", "size"),
            mean_premium=("mid_premium", "mean"),
            mean_settlement=("settlement", "mean"),
            equal_combo_net_per_contract=("net_per_contract", "mean"),
        )
        .reset_index()
    )
    return quintiles, price_cells


def feature_diagnostics(quality: pd.DataFrame):
    data = quality.copy()
    data["price_decile"] = np.minimum((data.yes_price * 10).astype(int), 9)
    rows = []
    for feature in [
        "mid_premium",
        "ask_premium",
        "log_mid_ratio",
        "spread_adjusted_premium",
    ]:
        feature_residual = data[feature] - data.groupby("price_decile")[
            feature
        ].transform("mean")
        net_residual = data.net_per_contract - data.groupby("price_decile")[
            "net_per_contract"
        ].transform("mean")
        rows.append(
            {
                "feature": feature,
                "pearson_with_net": float(data[feature].corr(data.net_per_contract)),
                "spearman_with_net": float(
                    data[feature].corr(data.net_per_contract, method="spearman")
                ),
                "within_price_pearson": float(feature_residual.corr(net_residual)),
                "within_price_spearman": float(
                    feature_residual.corr(net_residual, method="spearman")
                ),
            }
        )
    return pd.DataFrame(rows)


def cross_validated_models(quality: pd.DataFrame):
    data = quality.copy()
    data["fold"] = data.combo_market_ticker.map(
        lambda value: int(hashlib.sha1(value.encode()).hexdigest()[:8], 16) % 5
    )
    y = data.settlement.to_numpy()
    feature_sets = {
        "calibrated_combo_price": ["yes_price"],
        "calibrated_component_mid": ["component_mid_product"],
        "combo_plus_component": ["yes_price", "component_mid_product"],
        "lightweight_features": [
            "yes_price",
            "component_mid_product",
            "leg_count",
            "distinct_leg_games",
            "mean_leg_spread",
            "max_quote_age_seconds",
        ],
    }
    rows = []
    for model, features in feature_sets.items():
        predictions = np.zeros(len(data))
        for fold in range(5):
            train = data.fold.ne(fold).to_numpy()
            test = ~train
            train_x = np.column_stack(
                [np.ones(train.sum()), data.loc[train, features].to_numpy()]
            )
            coefficients = np.linalg.lstsq(train_x, y[train], rcond=None)[0]
            test_x = np.column_stack(
                [np.ones(test.sum()), data.loc[test, features].to_numpy()]
            )
            predictions[test] = test_x @ coefficients
        predictions = predictions.clip(0, 1)
        rows.append(
            {
                "model": model,
                "features": ", ".join(features),
                "combos": int(len(data)),
                "brier_score": float(np.mean((predictions - y) ** 2)),
                "mean_absolute_error": float(np.mean(np.abs(predictions - y))),
            }
        )
    return pd.DataFrame(rows)


def calibration_chart(path: Path, calibration: pd.DataFrame, forecasts: pd.DataFrame):
    width, height = 920, 790
    left, right, top, bottom = 105, 850, 125, 675
    scale_x = lambda value: left + value * (right - left)
    scale_y = lambda value: bottom - value * (bottom - top)
    parts = [
        svg_text(65, 42, "Standalone leg prices did not improve win predictions", 24, weight="bold"),
        svg_text(65, 68, "Each point compares predicted win chance with actual win rate; dashed line = perfect", 14, fill=MUTED),
    ]
    for value in np.arange(0, 1.01, 0.1):
        x, y = scale_x(value), scale_y(value)
        parts += [
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" stroke="{GRID}"/>',
            f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="{GRID}"/>',
            svg_text(x, bottom + 25, f"{value:.1f}", 12, anchor="middle", fill=MUTED),
            svg_text(left - 13, y + 4, f"{value:.1f}", 12, anchor="end", fill=MUTED),
        ]
    parts.append(f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{top}" stroke="{INK}" stroke-width="2" stroke-dasharray="6 5"/>')
    for label, color, radius in [
        ("Combo trade price", BLUE, 6),
        ("Component midpoint product", ORANGE, 5),
    ]:
        data = calibration[calibration.forecast == label]
        points = " ".join(
            f"{scale_x(row.mean_forecast):.1f},{scale_y(row.mean_settlement):.1f}"
            for row in data.itertuples()
        )
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="3"/>')
        for row in data.itertuples():
            parts.append(f'<circle cx="{scale_x(row.mean_forecast):.1f}" cy="{scale_y(row.mean_settlement):.1f}" r="{radius}" fill="{color}"/>')
    metrics = forecasts[forecasts["sample"] == "all_quality"].set_index("forecast")
    combo_brier = metrics.loc["combo_trade_price", "brier_score"]
    mid_brier = metrics.loc["component_mid_product", "brier_score"]
    parts += [
        svg_text((left + right) / 2, 728, "Predicted chance the combo settles YES", 14, anchor="middle"),
        f'<text x="28" y="{(top + bottom) / 2:.1f}" font-family="Arial, sans-serif" font-size="14" text-anchor="middle" fill="{INK}" transform="rotate(-90 28 {(top + bottom) / 2:.1f})">Actual share that settled YES</text>',
        f'<line x1="{left+20}" y1="101" x2="{left+52}" y2="101" stroke="{BLUE}" stroke-width="3"/>',
        svg_text(left + 60, 106, f"combo price · error {combo_brier:.5f}", 13),
        f'<line x1="{left+255}" y1="101" x2="{left+287}" y2="101" stroke="{ORANGE}" stroke-width="3"/>',
        svg_text(left + 295, 106, f"standalone legs · error {mid_brier:.5f}", 13),
        svg_text((left + right) / 2, 765, "Brier error: 0 is perfect and lower is better; these two scores are nearly identical", 13, anchor="middle", fill=MUTED),
    ]
    write_svg(
        path,
        width,
        height,
        "Combo and component calibration",
        "Calibration of combo trade prices and products of timestamp-matched standalone leg midpoints against realized settlements.",
        parts,
    )


def premium_chart(path: Path, quintiles: pd.DataFrame, price_cells: pd.DataFrame):
    width, height = 1120, 760
    left, right = 105, 1050
    parts = [
        svg_text(65, 42, "Standalone leg prices did not reveal a reliable seller edge", 24, weight="bold"),
        svg_text(65, 68, "Price gap = combo price minus the win chance implied by its standalone legs", 14, fill=MUTED),
        svg_text(65, 110, "Combos grouped from lowest to highest price gap", 18, weight="bold"),
    ]
    xs = np.linspace(left + 75, right - 75, len(quintiles))
    top, bottom, ymin, ymax = 135, 365, -7, 6
    scale_y = lambda value: bottom - (value - ymin) / (ymax - ymin) * (bottom - top)
    for value in [-6, -3, 0, 3, 6]:
        y = scale_y(value)
        parts += [
            f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="{INK if value == 0 else GRID}"/>',
            svg_text(left - 12, y + 4, f"{value:+d}¢", 12, anchor="end", fill=MUTED),
        ]
    zero = scale_y(0)
    for x, row in zip(xs, quintiles.itertuples()):
        value = row.equal_combo_net_per_contract * 100
        y = scale_y(value)
        color = GREEN if value >= 0 else RED
        parts += [
            f'<rect x="{x-33:.1f}" y="{min(y,zero):.1f}" width="66" height="{abs(zero-y):.1f}" fill="{color}"/>',
            svg_text(x, y - 8 if value >= 0 else y + 18, f"{value:+.1f}¢", 13, anchor="middle", fill=color),
            svg_text(x, 392, ["Lowest", "Low", "Middle", "High", "Highest"][int(row.premium_quintile) - 1], 14, anchor="middle", weight="bold"),
            svg_text(x, 414, f"mean gap {row.mean_premium*100:+.2f}¢", 12, anchor="middle", fill=MUTED),
            svg_text(x, 434, f"n={int(row.combos):,}", 12, anchor="middle", fill=MUTED),
        ]
    parts += [
        svg_text(65, 482, "Within the same combo-price range", 18, weight="bold"),
        svg_text(65, 506, "Seller profit when the price gap is negative/zero versus positive", 13, fill=MUTED),
    ]
    buckets = ["0–10¢", "10–25¢", "25–50¢", "50–100¢"]
    xs = np.linspace(left + 100, right - 100, len(buckets))
    top2, bottom2, ymin2, ymax2 = 530, 690, -12, 8
    scale_y2 = lambda value: bottom2 - (value - ymin2) / (ymax2 - ymin2) * (bottom2 - top2)
    for value in [-10, -5, 0, 5]:
        y = scale_y2(value)
        parts += [
            f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="{INK if value == 0 else GRID}"/>',
            svg_text(left - 12, y + 4, f"{value:+d}¢", 12, anchor="end", fill=MUTED),
        ]
    zero2 = scale_y2(0)
    for x, bucket in zip(xs, buckets):
        data = price_cells[price_cells.price_bucket.astype(str) == bucket].set_index("premium_sign")
        for offset, sign, color in [(-27, "premium ≤ 0", GREEN), (27, "premium > 0", ORANGE)]:
            row = data.loc[sign]
            value = row.equal_combo_net_per_contract * 100
            y = scale_y2(value)
            parts += [
                f'<rect x="{x+offset-22:.1f}" y="{min(y,zero2):.1f}" width="44" height="{abs(zero2-y):.1f}" fill="{color}"/>',
                svg_text(x + offset, y - 7 if value >= 0 else y + 16, f"{value:+.1f}", 11, anchor="middle", fill=color),
            ]
        parts.append(svg_text(x, 720, bucket, 13, anchor="middle", weight="bold"))
    parts += [
        f'<rect x="{left}" y="738" width="14" height="14" fill="{GREEN}"/>',
        svg_text(left + 22, 750, "price gap ≤ 0", 12),
        f'<rect x="{left+125}" y="738" width="14" height="14" fill="{ORANGE}"/>',
        svg_text(left + 147, 750, "price gap > 0", 12),
    ]
    write_svg(
        path,
        width,
        height,
        "Component premium and seller edge",
        "Seller returns by component-midpoint premium quintile and by premium sign within combo price ranges.",
        parts,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    combo_dir = args.data_root / "combos"
    fills = pd.read_parquet(combo_dir / "kalshi_nfl_combo_economics.parquet")
    fills = fills[fills.scope == "cross_game"].copy()
    legs = pd.read_parquet(combo_dir / "kalshi_nfl_combo_legs.parquet")

    cross_tickers = set(fills.combo_market_ticker)
    cross_legs = legs[legs.combo_market_ticker.isin(cross_tickers)]
    mapping = cross_legs.groupby("combo_market_ticker").agg(
        legs=("leg_index", "size"),
        mapped_legs=("mapped_to_sunday_kalshi", "sum"),
        known_settlements=("selected_leg_settlement_value", "count"),
        leg_settlement_product=("selected_leg_settlement_value", "prod"),
    )
    combo_settlements = fills.groupby("combo_market_ticker").settlement_value.first()
    mapping = mapping.join(combo_settlements.rename("combo_settlement"))
    mapping_validation = {
        "cross_game_combos": int(len(mapping)),
        "all_legs_mapped_combos": int((mapping.mapped_legs == mapping.legs).sum()),
        "all_legs_mapped_rate": float((mapping.mapped_legs == mapping.legs).mean()),
        "known_leg_settlement_combos": int(
            (mapping.known_settlements == mapping.legs).sum()
        ),
        "settlement_product_match_rate": float(
            np.isclose(mapping.leg_settlement_product, mapping.combo_settlement).mean()
        ),
    }

    values = component_values(fills, legs, args.data_root / "processed" / "kalshi")
    matched = fills.merge(values, on="trade_id", how="inner", validate="one_to_one")
    fresh = matched[matched.max_quote_age_seconds <= FRESH_SECONDS]
    quality_fills = fresh[fresh.max_leg_spread <= MAX_LEG_SPREAD].copy()
    quality = aggregate_combos(quality_fills)

    coverage = pd.DataFrame(
        [
            {
                "sample": "all_cross_game",
                "fills": len(fills),
                "combos": fills.combo_market_ticker.nunique(),
                "contracts": fills["size"].sum(),
            },
            {
                "sample": "full_component_book",
                "fills": len(matched),
                "combos": matched.combo_market_ticker.nunique(),
                "contracts": matched["size"].sum(),
            },
            {
                "sample": "quotes_at_most_30s_old",
                "fills": len(fresh),
                "combos": fresh.combo_market_ticker.nunique(),
                "contracts": fresh["size"].sum(),
            },
            {
                "sample": "primary_quality",
                "fills": len(quality_fills),
                "combos": len(quality),
                "contracts": quality_fills["size"].sum(),
            },
        ]
    )
    forecasts = forecast_table(quality)
    calibration = calibration_table(quality)
    quintiles, price_cells = premium_tables(quality)
    diagnostics = feature_diagnostics(quality)
    models = cross_validated_models(quality)

    rules = pd.DataFrame(
        [
            summarize_rule(fills[fills.yes_price < 0.10], "frozen_cross_game_yes_below_10c"),
            summarize_rule(
                quality_fills[quality_fills.yes_price < 0.10],
                "below_10c_on_primary_quality_sample",
            ),
            summarize_rule(
                quality_fills[
                    quality_fills.yes_price > quality_fills.component_mid_product
                ],
                "positive_midpoint_premium",
            ),
            summarize_rule(
                quality_fills[
                    quality_fills.yes_price > quality_fills.component_ask_product
                ],
                "combo_above_component_ask_product",
            ),
            summarize_rule(
                quality_fills[
                    (quality_fills.yes_price < 0.10)
                    & (quality_fills.yes_price > quality_fills.component_mid_product)
                ],
                "below_10c_and_positive_midpoint_premium",
            ),
        ]
    )

    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    quality_fill_output = quality_fills.copy()
    quality_fill_output["realized_net_per_contract"] = (
        quality_fill_output.net_pnl / quality_fill_output["size"]
    )
    quality_fill_output.to_parquet(
        output / "quality_fill_values.parquet", index=False
    )
    quality.to_parquet(output / "quality_combo_values.parquet", index=False)
    coverage.to_csv(output / "coverage.csv", index=False)
    forecasts.to_csv(output / "forecast_metrics.csv", index=False)
    calibration.to_csv(output / "calibration.csv", index=False)
    quintiles.to_csv(output / "premium_quintiles.csv", index=False)
    price_cells.to_csv(output / "price_premium_cells.csv", index=False)
    diagnostics.to_csv(output / "feature_diagnostics.csv", index=False)
    models.to_csv(output / "model_cv.csv", index=False)
    rules.to_csv(output / "benchmark_comparison.csv", index=False)
    pd.DataFrame([mapping_validation]).to_csv(
        output / "mapping_validation.csv", index=False
    )
    calibration_chart(output / "component-calibration.svg", calibration, forecasts)
    premium_chart(output / "premium-signal.svg", quintiles, price_cells)

    primary_forecasts = forecasts[forecasts["sample"] == "all_quality"].set_index(
        "forecast"
    )
    model_metrics = models.set_index("model")
    rule_metrics = rules.set_index("rule")
    summary = {
        "method": {
            "scope": "cross-game combos",
            "component_mapping": "exact standalone Kalshi market ticker and selected side",
            "quote_rule": "latest top-of-book received no later than the combo fill",
            "primary_quality_filter": {
                "max_quote_age_seconds": FRESH_SECONDS,
                "max_leg_spread": MAX_LEG_SPREAD,
            },
            "fair_values": [
                "product of selected-side standalone bids",
                "product of selected-side standalone midpoints",
                "product of selected-side standalone asks",
            ],
        },
        "mapping_validation": mapping_validation,
        "coverage": coverage.set_index("sample").to_dict("index"),
        "headline": {
            "quality_combos": int(len(quality)),
            "combo_price_brier": float(
                primary_forecasts.loc["combo_trade_price", "brier_score"]
            ),
            "component_mid_brier": float(
                primary_forecasts.loc["component_mid_product", "brier_score"]
            ),
            "combo_mean_price": float(quality.yes_price.mean()),
            "component_mean_mid": float(quality.component_mid_product.mean()),
            "mean_settlement": float(quality.settlement.mean()),
            "price_only_cv_brier": float(
                model_metrics.loc["calibrated_combo_price", "brier_score"]
            ),
            "price_plus_component_cv_brier": float(
                model_metrics.loc["combo_plus_component", "brier_score"]
            ),
            "frozen_benchmark_equal_net": float(
                rule_metrics.loc[
                    "frozen_cross_game_yes_below_10c", "equal_combo_net_per_contract"
                ]
            ),
            "positive_premium_equal_net": float(
                rule_metrics.loc[
                    "positive_midpoint_premium", "equal_combo_net_per_contract"
                ]
            ),
        },
        "conclusion": [
            "Standalone midpoint products are sensibly calibrated in aggregate but do not beat the combo trade price as a probability forecast.",
            "Component premium is not monotonic with seller returns and adds no cross-validated improvement to a price-only linear calibration.",
            "The frozen cross-game YES below 10 cents result remains the stronger descriptive benchmark on this slate.",
            "The component model is useful as a sanity check and data-quality feature, not yet as a trading selector.",
        ],
        "future_data": [
            "Complete pregame and in-game standalone books for every combo leg, synchronized with exchange timestamps.",
            "Multiple future slates for strictly forward time validation and calibration by leg type.",
            "Combo bid/ask and RFQ state, not only prints, to measure executable seller capacity and selection.",
            "Enough repeated same-game leg pairs to estimate joint probabilities before multiplying across games.",
        ],
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "matched_fills": int(len(matched)),
                "quality_combos": int(len(quality)),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
