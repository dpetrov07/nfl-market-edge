"""Descriptive follow-up on the settled Sunday Kalshi combo sample."""

from __future__ import annotations

import argparse
import json
from html import escape
from pathlib import Path

import numpy as np
import pandas as pd

from analyze_sunday_combo_economics import DEFAULT_ROOT


DEFAULT_OUTPUT = Path("research/output/sunday_combo_2026-09-13")
BLUE = "#1677b3"
ORANGE = "#d95f02"
GREEN = "#159b76"
RED = "#c44e52"
INK = "#20252b"
MUTED = "#68717c"
GRID = "#d9dee4"
LIGHT = "#eef2f5"


def svg_text(x, y, value, size=14, anchor="start", weight="normal", fill=INK):
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="Arial, sans-serif" '
        f'font-size="{size}" text-anchor="{anchor}" font-weight="{weight}" '
        f'fill="{fill}">{escape(str(value))}</text>'
    )


def write_svg(path: Path, width: int, height: int, title: str, desc: str, parts):
    path.write_text(
        "\n".join(
            [
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
                f"<title>{escape(title)}</title>",
                f"<desc>{escape(desc)}</desc>",
                f'<rect width="{width}" height="{height}" fill="white"/>',
                *parts,
                "</svg>",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def aggregate_positions(fills: pd.DataFrame, keys=("combo_market_ticker",)):
    data = fills.assign(price_notional=fills.yes_price * fills["size"])
    aggregations = {
        "leg_count": ("leg_count", "first"),
        "games": ("games", "first"),
        "settlement": ("settlement_value", "first"),
        "fills": ("trade_id", "size"),
        "contracts": ("size", "sum"),
        "price_notional": ("price_notional", "sum"),
        "gross_pnl": ("gross_pnl", "sum"),
        "net_pnl": ("net_pnl", "sum"),
    }
    if "scope" not in keys:
        aggregations["scope"] = ("scope", "first")
    grouped = (
        data.groupby(list(keys), observed=True, sort=True)
        .agg(**aggregations)
        .reset_index()
    )
    grouped["vwap"] = grouped.price_notional / grouped.contracts
    grouped["gross_per_contract"] = grouped.gross_pnl / grouped.contracts
    grouped["net_per_contract"] = grouped.net_pnl / grouped.contracts
    return grouped


def summarize_positions(positions: pd.DataFrame):
    contracts = positions.contracts.sum()
    return {
        "combos": int(len(positions)),
        "fills": int(positions.fills.sum()),
        "contracts": float(contracts),
        "mean_vwap": float(positions.vwap.mean()),
        "mean_settlement": float(positions.settlement.mean()),
        "full_yes_settlement_rate": float((positions.settlement == 1).mean()),
        "any_positive_settlement_rate": float((positions.settlement > 0).mean()),
        "equal_combo_net_per_contract": float(positions.net_per_contract.mean()),
        "volume_weighted_net_per_contract": float(positions.net_pnl.sum() / contracts),
        "p05_net_per_contract": float(positions.net_per_contract.quantile(0.05)),
        "median_net_per_contract": float(positions.net_per_contract.median()),
        "p95_net_per_contract": float(positions.net_per_contract.quantile(0.95)),
        "worst_net_per_contract": float(positions.net_per_contract.min()),
    }


def build_price_buckets(fills: pd.DataFrame):
    data = fills.copy()
    data["price_bucket_low"] = np.minimum(
        np.floor((data.yes_price * 100 + 1e-8) / 5).astype(int) * 5, 95
    )
    positions = aggregate_positions(
        data, ("scope", "price_bucket_low", "combo_market_ticker")
    )
    rows = []
    for (scope, low), group in positions.groupby(
        ["scope", "price_bucket_low"], observed=True, sort=True
    ):
        row = summarize_positions(group)
        row.update(
            {
                "scope": scope,
                "price_bucket_low_cents": int(low),
                "price_bucket_high_cents": int(low + 5),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["scope", "price_bucket_low_cents"])


def build_structures(all_positions: pd.DataFrame):
    rows = []
    for (scope, leg_count), group in all_positions.groupby(
        ["scope", "leg_count"], observed=True, sort=True
    ):
        row = summarize_positions(group)
        row.update({"scope": scope, "leg_count": int(leg_count)})
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["scope", "leg_count"])


def candidate_analysis(fills: pd.DataFrame, legs: pd.DataFrame):
    selected_fills = fills[(fills.scope == "cross_game") & (fills.yes_price < 0.10)]
    positions = aggregate_positions(selected_fills)
    tickers = set(positions.combo_market_ticker)
    selected_legs = legs[legs.combo_market_ticker.isin(tickers)].merge(
        positions[["combo_market_ticker", "net_per_contract", "settlement"]],
        on="combo_market_ticker",
        how="left",
    )

    games = []
    for row in positions.itertuples():
        combo_games = row.games.split(", ")
        for game in combo_games:
            games.append(
                {
                    "game": game,
                    "combo_market_ticker": row.combo_market_ticker,
                    "allocated_equal_combo_net": row.net_per_contract / len(combo_games),
                    "losing_position": row.net_per_contract < 0,
                }
            )
    game_exposure = (
        pd.DataFrame(games)
        .groupby("game", as_index=False)
        .agg(
            combos=("combo_market_ticker", "nunique"),
            allocated_equal_combo_net=("allocated_equal_combo_net", "sum"),
            losing_positions=("losing_position", "sum"),
        )
    )
    game_exposure["combo_share"] = game_exposure.combos / len(positions)
    game_exposure = game_exposure.sort_values("combos", ascending=False)

    def leg_label(row):
        subject = row.player if pd.notna(row.player) else row.outcome_team
        subject = subject if pd.notna(subject) else row.underlying_market_ticker
        detail = str(row.prop_type) if pd.notna(row.prop_type) else "market"
        if pd.notna(row.threshold):
            detail += f" {row.threshold:g}"
        return f"{subject} | {detail} | {row.side.upper()}"

    selected_legs = selected_legs.copy()
    selected_legs["label"] = selected_legs.apply(leg_label, axis=1)
    underlying = (
        selected_legs.groupby(
            ["underlying_market_ticker", "side", "game", "label"], as_index=False
        )
        .agg(
            combos=("combo_market_ticker", "nunique"),
            positive_settlement_combos=("settlement", lambda values: int((values > 0).sum())),
        )
        .sort_values("combos", ascending=False)
    )
    underlying["combo_share"] = underlying.combos / len(positions)

    leave_one_game_out = []
    for game in sorted(game_exposure.game):
        kept = positions[
            ~positions.games.str.split(", ").map(lambda values: game in values)
        ]
        leave_one_game_out.append(
            {
                "excluded_game": game,
                "combos": int(len(kept)),
                "equal_combo_net_per_contract": float(kept.net_per_contract.mean()),
                "equal_combo_net_total": float(kept.net_per_contract.sum()),
            }
        )

    gains = positions[positions.net_per_contract > 0]
    losses = positions[positions.net_per_contract < 0]
    total_contracts = positions.contracts.sum()
    summary = summarize_positions(positions)
    by_leg_count = {
        str(int(leg_count)): summarize_positions(group)
        for leg_count, group in positions.groupby("leg_count", sort=True)
    }
    summary.update(
        {
            "definition": "cross-game fills with YES execution price < $0.10",
            "two_leg_combos": int((positions.leg_count == 2).sum()),
            "three_leg_combos": int((positions.leg_count == 3).sum()),
            "by_leg_count": by_leg_count,
            "profitable_equal_positions": int(len(gains)),
            "losing_equal_positions": int(len(losses)),
            "mean_gain_per_contract": float(gains.net_per_contract.mean()),
            "mean_loss_per_contract": float(losses.net_per_contract.mean()),
            "equal_position_gains": float(gains.net_per_contract.sum()),
            "equal_position_losses": float(losses.net_per_contract.sum()),
            "equal_position_net": float(positions.net_per_contract.sum()),
            "top_1_combo_volume_share": float(positions.contracts.max() / total_contracts),
            "top_10_combo_volume_share": float(
                positions.nlargest(10, "contracts").contracts.sum() / total_contracts
            ),
            "top_100_combo_volume_share": float(
                positions.nlargest(100, "contracts").contracts.sum() / total_contracts
            ),
            "loss_combo_volume_share": float(losses.contracts.sum() / total_contracts),
            "directional_leg_slots": int(len(selected_legs)),
            "unique_directional_legs": int(
                selected_legs.groupby(["underlying_market_ticker", "side"]).ngroups
            ),
            "max_directional_leg_share": float(underlying.iloc[0].combo_share),
            "max_directional_leg": underlying.iloc[0].label,
            "leave_one_game_out_edge_range": [
                min(row["equal_combo_net_per_contract"] for row in leave_one_game_out),
                max(row["equal_combo_net_per_contract"] for row in leave_one_game_out),
            ],
        }
    )
    return positions, game_exposure, underlying, pd.DataFrame(leave_one_game_out), summary


def price_neighborhood_chart(path: Path, buckets: pd.DataFrame):
    data = buckets[
        (buckets.scope == "cross_game") & (buckets.price_bucket_low_cents < 25)
    ].reset_index(drop=True)
    width, height = 1180, 760
    left, right = 90, 1120
    xs = np.linspace(left + 70, right - 70, len(data))
    parts = [
        svg_text(left, 42, "Cross-game YES pricing around the 10¢ candidate", 25, weight="bold"),
        svg_text(left, 68, "Five-cent execution buckets; each combo is equal-weighted inside a bucket", 14, fill=MUTED),
    ]

    top_y0, top_y1 = 105, 350
    for value in range(0, 26, 5):
        y = top_y1 - value / 25 * (top_y1 - top_y0)
        parts += [
            f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="{GRID}"/>',
            svg_text(left - 12, y + 5, f"{value}¢", 13, anchor="end", fill=MUTED),
        ]
    series = [
        ("Mean observed YES price", data.mean_vwap * 100, BLUE),
        ("Mean realized settlement", data.mean_settlement * 100, ORANGE),
    ]
    for label, values, color in series:
        ys = [top_y1 - value / 25 * (top_y1 - top_y0) for value in values]
        points = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="3"/>')
        for x, y, value in zip(xs, ys, values):
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}"/>')
            parts.append(svg_text(x, y - 10, f"{value:.1f}¢", 12, anchor="middle", fill=color))
    parts += [
        f'<line x1="{left}" y1="{top_y1}" x2="{right}" y2="{top_y1}" stroke="{INK}"/>',
        f'<line x1="{left+20}" y1="92" x2="{left+52}" y2="92" stroke="{BLUE}" stroke-width="3"/>',
        svg_text(left + 60, 97, "observed price", 13),
        f'<line x1="{left+190}" y1="92" x2="{left+222}" y2="92" stroke="{ORANGE}" stroke-width="3"/>',
        svg_text(left + 230, 97, "realized payout", 13),
        svg_text(left, 395, "Net seller P&L after the existing 7% p(1-p) fee estimate", 18, weight="bold"),
    ]

    bottom_y0, bottom_y1 = 425, 625
    ymin, ymax = -3.0, 5.5
    for value in [-2, 0, 2, 4]:
        y = bottom_y1 - (value - ymin) / (ymax - ymin) * (bottom_y1 - bottom_y0)
        parts += [
            f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="{INK if value == 0 else GRID}"/>',
            svg_text(left - 12, y + 5, f"{value:+d}¢", 13, anchor="end", fill=MUTED),
        ]
    bar_width = 30
    for x, row in zip(xs, data.itertuples()):
        values = [row.equal_combo_net_per_contract * 100, row.volume_weighted_net_per_contract * 100]
        for offset, value, color in [(-17, values[0], GREEN), (17, values[1], BLUE)]:
            zero = bottom_y1 - (0 - ymin) / (ymax - ymin) * (bottom_y1 - bottom_y0)
            y = bottom_y1 - (value - ymin) / (ymax - ymin) * (bottom_y1 - bottom_y0)
            parts.append(
                f'<rect x="{x+offset-bar_width/2:.1f}" y="{min(y, zero):.1f}" width="{bar_width}" '
                f'height="{abs(zero-y):.1f}" fill="{color}" opacity="0.9"/>'
            )
            parts.append(svg_text(x + offset, y - 7 if value >= 0 else y + 16, f"{value:+.1f}", 12, anchor="middle", fill=color))
        label = f"{row.price_bucket_low_cents}–{row.price_bucket_high_cents}¢"
        parts.append(svg_text(x, 652, label, 14, anchor="middle", weight="bold"))
        parts.append(svg_text(x, 674, f"{row.combos:,} combos", 12, anchor="middle", fill=MUTED))
        parts.append(svg_text(x, 693, f"{row.contracts/1e6:.2f}m contracts", 12, anchor="middle", fill=MUTED))
    parts += [
        f'<rect x="{left+20}" y="714" width="15" height="15" fill="{GREEN}"/>',
        svg_text(left + 43, 727, "equal combo", 13),
        f'<rect x="{left+150}" y="714" width="15" height="15" fill="{BLUE}"/>',
        svg_text(left + 173, 727, "volume weighted", 13),
        svg_text(right, 727, "A combo may appear in multiple buckets if it traded across them.", 12, anchor="end", fill=MUTED),
    ]
    write_svg(
        path,
        width,
        height,
        "Cross-game price neighborhood",
        "Observed prices, settlements, and seller net P&L for cross-game combo fills from zero to twenty-five cents.",
        parts,
    )


def structure_chart(path: Path, structures: pd.DataFrame):
    labels = {
        ("cross_game", 2): "Cross-game · 2 legs",
        ("cross_game", 3): "Cross-game · 3 legs",
        ("same_game", 2): "Same-game · 2 legs",
        ("same_game", 3): "Same-game · 3 legs",
    }
    data = structures.copy()
    data["label"] = [labels[(row.scope, row.leg_count)] for row in data.itertuples()]
    width, height = 1180, 700
    left, right = 285, 1110
    parts = [
        svg_text(70, 42, "Where the seller edge appears by combo structure", 25, weight="bold"),
        svg_text(70, 68, "One position per combo at its observed volume-weighted price", 14, fill=MUTED),
    ]

    top, row_gap = 125, 82
    xmin, xmax = -2.0, 7.0
    xscale = lambda value: left + (value - xmin) / (xmax - xmin) * (right - left)
    for value in [-2, 0, 2, 4, 6]:
        x = xscale(value)
        parts += [
            f'<line x1="{x:.1f}" y1="95" x2="{x:.1f}" y2="430" stroke="{INK if value == 0 else GRID}"/>',
            svg_text(x, 450, f"{value:+d}¢", 13, anchor="middle", fill=MUTED),
        ]
    for index, row in enumerate(data.itertuples()):
        y = top + index * row_gap
        parts.append(svg_text(left - 18, y + 6, row.label, 15, anchor="end", weight="bold"))
        for dy, value, color in [(-12, row.equal_combo_net_per_contract * 100, GREEN), (12, row.volume_weighted_net_per_contract * 100, BLUE)]:
            x0, x1 = xscale(0), xscale(value)
            parts.append(f'<rect x="{min(x0,x1):.1f}" y="{y+dy-8:.1f}" width="{abs(x1-x0):.1f}" height="16" fill="{color}"/>')
            parts.append(svg_text(x1 + (7 if value >= 0 else -7), y + dy + 5, f"{value:+.2f}¢", 12, anchor="start" if value >= 0 else "end", fill=color))
        parts.append(svg_text(left - 18, y + 29, f"n={row.combos:,} · {row.contracts/1e6:.2f}m contracts · YES settled {row.full_yes_settlement_rate:.1%}", 11, anchor="end", fill=MUTED))

    parts += [
        f'<rect x="{left}" y="480" width="15" height="15" fill="{GREEN}"/>',
        svg_text(left + 23, 493, "equal combo", 13),
        f'<rect x="{left+135}" y="480" width="15" height="15" fill="{BLUE}"/>',
        svg_text(left + 158, 493, "volume weighted", 13),
        svg_text(70, 535, "Left tail of equal-combo outcomes", 18, weight="bold"),
        svg_text(70, 560, "5th-percentile net P&L per contract", 13, fill=MUTED),
    ]
    tail_left, tail_right = 355, 1110
    for value in [-80, -60, -40, -20, 0]:
        x = tail_left + (value + 85) / 85 * (tail_right - tail_left)
        parts += [
            f'<line x1="{x:.1f}" y1="572" x2="{x:.1f}" y2="658" stroke="{GRID}"/>',
            svg_text(x, 681, f"{value}¢", 12, anchor="middle", fill=MUTED),
        ]
    for index, row in enumerate(data.itertuples()):
        y = 585 + index * 22
        value = row.p05_net_per_contract * 100
        x = tail_left + (value + 85) / 85 * (tail_right - tail_left)
        parts.append(svg_text(tail_left - 14, y + 4, row.label, 12, anchor="end"))
        parts.append(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{tail_right:.1f}" y2="{y:.1f}" stroke="{LIGHT}" stroke-width="8"/>')
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{RED}"/>')
        parts.append(svg_text(x - 9, y + 4, f"{value:.1f}¢", 12, anchor="end", fill=RED))
    write_svg(
        path,
        width,
        height,
        "Seller edge by combo structure",
        "Equal-combo and volume-weighted seller returns plus fifth-percentile downside for same-game and cross-game two- and three-leg combos.",
        parts,
    )


def candidate_risk_chart(path: Path, positions: pd.DataFrame, games: pd.DataFrame, summary):
    width, height = 1180, 760
    parts = [
        svg_text(65, 42, "Cross-game YES <10¢: positive average, asymmetric slate risk", 25, weight="bold"),
        svg_text(65, 68, "One equal-sized position per unique combo; correlated exposures are counted, not diversified away", 14, fill=MUTED),
        svg_text(65, 115, "Outcome asymmetry", 18, weight="bold"),
    ]
    gains = summary["profitable_equal_positions"]
    losses = summary["losing_equal_positions"]
    cards = [
        (65, GREEN, f"{gains:,} gains", f"mean {summary['mean_gain_per_contract']*100:+.2f}¢", f"equal-position sum ${summary['equal_position_gains']:+.2f}"),
        (285, RED, f"{losses:,} losses", f"mean {summary['mean_loss_per_contract']*100:+.2f}¢", f"equal-position sum -${abs(summary['equal_position_losses']):.2f}"),
    ]
    for x, color, heading, mean_text, sum_text in cards:
        parts += [
            f'<rect x="{x}" y="135" width="190" height="115" rx="8" fill="{LIGHT}"/>',
            f'<rect x="{x}" y="135" width="8" height="115" rx="4" fill="{color}"/>',
            svg_text(x + 24, 168, heading, 18, weight="bold"),
            svg_text(x + 24, 198, mean_text, 15, fill=color),
            svg_text(x + 24, 225, sum_text, 13, fill=MUTED),
        ]
    parts += [
        svg_text(65, 286, f"Net across 4,864 equal positions: ${summary['equal_position_net']:+.2f} ({summary['equal_combo_net_per_contract']*100:+.2f}¢ each)", 15, weight="bold"),
        svg_text(65, 314, f"5th percentile {summary['p05_net_per_contract']*100:+.2f}¢ · worst {summary['worst_net_per_contract']*100:+.2f}¢", 14, fill=MUTED),
        svg_text(65, 350, "Observed-volume concentration", 18, weight="bold"),
        svg_text(65, 382, f"Top 1 / 10 / 100 combos: {summary['top_1_combo_volume_share']:.1%} / {summary['top_10_combo_volume_share']:.1%} / {summary['top_100_combo_volume_share']:.1%} of contracts", 14),
        svg_text(65, 408, f"Only {summary['loss_combo_volume_share']:.1%} of observed contracts sat in losing combos; volume-weighted results are therefore fragile.", 13, fill=MUTED),
        svg_text(65, 458, "Underlying overlap", 18, weight="bold"),
        svg_text(65, 488, f"{summary['unique_directional_legs']:,} unique directional legs across {summary['directional_leg_slots']:,} leg slots", 14),
        svg_text(65, 515, f"Largest repeated leg: {summary['max_directional_leg']} ({summary['max_directional_leg_share']:.1%} of combos)", 13, fill=MUTED),
        svg_text(65, 565, "Tail interpretation", 18, weight="bold"),
        svg_text(65, 597, "A typical win earns about 5¢; a settled-YES loss costs about 93¢.", 14),
        svg_text(65, 623, "The one-slate average is positive because only 159 of 4,864 positions lost.", 13, fill=MUTED),
        svg_text(625, 115, "Game exposure", 18, weight="bold"),
        svg_text(625, 139, "Share of candidate combos containing each game", 13, fill=MUTED),
    ]
    game_data = games.sort_values("combos", ascending=True).reset_index(drop=True)
    chart_left, chart_right = 760, 1100
    y0, gap = 170, 37
    max_share = 0.30
    for index, row in enumerate(game_data.itertuples()):
        y = y0 + index * gap
        bar_width = row.combo_share / max_share * (chart_right - chart_left)
        parts += [
            svg_text(chart_left - 12, y + 14, row.game, 12, anchor="end"),
            f'<rect x="{chart_left}" y="{y}" width="{bar_width:.1f}" height="19" fill="{BLUE}" opacity="0.85"/>',
            svg_text(chart_left + bar_width + 7, y + 14, f"{row.combo_share:.1%}", 12),
        ]
    parts += [
        svg_text(625, 680, f"Leave-one-game-out equal edge stayed between {summary['leave_one_game_out_edge_range'][0]*100:+.2f}¢ and {summary['leave_one_game_out_edge_range'][1]*100:+.2f}¢.", 13, fill=MUTED),
    ]
    write_svg(
        path,
        width,
        height,
        "Candidate risk and exposure",
        "Gain and loss asymmetry, volume concentration, underlying overlap, and game exposure for cross-game combo fills below ten cents.",
        parts,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    combo_dir = args.data_root / "combos"
    fills = pd.read_parquet(combo_dir / "kalshi_nfl_combo_economics.parquet")
    combos = pd.read_parquet(combo_dir / "kalshi_nfl_combos.parquet")
    legs = pd.read_parquet(combo_dir / "kalshi_nfl_combo_legs.parquet")
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)

    all_positions = aggregate_positions(fills)
    price_buckets = build_price_buckets(fills)
    structures = build_structures(all_positions)
    candidate, games, underlyings, leave_one_out, candidate_summary = candidate_analysis(
        fills, legs
    )

    price_buckets.to_csv(output / "price_buckets.csv", index=False)
    structures.to_csv(output / "structure_breakouts.csv", index=False)
    games.to_csv(output / "candidate_game_exposure.csv", index=False)
    underlyings.head(50).to_csv(output / "candidate_top_underlyings.csv", index=False)
    leave_one_out.to_csv(output / "game_sensitivity.csv", index=False)

    price_neighborhood_chart(output / "price-neighborhood.svg", price_buckets)
    structure_chart(output / "structure-breakouts.svg", structures)
    candidate_risk_chart(output / "candidate-risk-exposure.svg", candidate, games, candidate_summary)

    overall = summarize_positions(all_positions)
    summary = {
        "sample": {
            "combos": int(len(combos)),
            "fills": int(len(fills)),
            "contracts": float(fills["size"].sum()),
            "all_combo_volume_reconciles": bool(combos.volume_reconciles.all()),
            "any_trade_history_truncated": bool(combos.trade_history_truncated.any()),
        },
        "overall": overall,
        "candidate": candidate_summary,
        "notes": [
            "Seller P&L is YES execution price minus settlement, less the existing 7% p(1-p) fee estimate.",
            "Equal-combo is the primary descriptive weighting; observed-volume weighting is also reported.",
            "Price buckets contain executions, so a combo can appear in more than one bucket.",
            "This is one settled Sunday slate with correlated combos, not an out-of-sample strategy test.",
        ],
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"output": str(output), **summary["sample"]}, sort_keys=True))


if __name__ == "__main__":
    main()
