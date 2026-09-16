"""Read-only Kalshi/Bovada snapshot scorer; never places orders."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.collect_live_bovada_ws import (  # noqa: E402
    fetch_sunday_events,
    selection_rows,
)
from collect_live_props import discover, snapshot_rows  # noqa: E402
from helpers import no_vig  # noqa: E402
from matching import normalize_player  # noqa: E402
from model import FEATURES, feature_row  # noqa: E402


ET = ZoneInfo("America/New_York")
TEAM_NAMES = {
    "ARI": "cardinals", "ATL": "falcons", "BAL": "ravens", "BUF": "bills",
    "CAR": "panthers", "CHI": "bears", "CIN": "bengals", "CLE": "browns",
    "DAL": "cowboys", "DEN": "broncos", "DET": "lions", "GB": "packers",
    "HOU": "texans", "IND": "colts", "JAC": "jaguars", "JAX": "jaguars",
    "KC": "chiefs", "LA": "rams", "LAC": "chargers", "LAR": "rams",
    "LV": "raiders", "MIA": "dolphins", "MIN": "vikings", "NE": "patriots",
    "NO": "saints", "NYG": "giants", "NYJ": "jets", "PHI": "eagles",
    "PIT": "steelers", "SEA": "seahawks", "SF": "49ers", "TB": "buccaneers",
    "TEN": "titans", "WAS": "commanders", "WSH": "commanders",
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", help="Optional game such as DET_BUF")
    parser.add_argument("--date", help="Local game date; inferred from Kalshi")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument(
        "--model", type=Path,
        default=ROOT / "model_output/player_prop_markout_v2/model.json",
    )
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "model_output/shadow/latest.json",
    )
    return parser.parse_args()


def model_predict(model, row):
    values = feature_row(row)
    specs = model["horizons"]["10"]["features"]
    if [item["name"] for item in specs] != list(FEATURES):
        raise RuntimeError("model feature order does not match live feature generator")
    return model["horizons"]["10"]["intercept"] + sum(
        (value - item["training_mean"]) / item["training_scale"]
        * item["standardized_coefficient"]
        for value, item in zip(values, specs)
    )


def combine(rule_tier, prediction):
    """Take the stronger ordinal signal; never average unlike evidence."""
    if rule_tier in {"strong", "enter"}:
        return rule_tier, "rule+model" if prediction > 0 else "rule"
    if rule_tier == "watch":
        return "watch", "rule+model" if prediction > 0 else "rule"
    if prediction > 0:
        return "watch", "model_shadow"
    return "pass", "no_positive_signal"


def choose_game(events, requested):
    if requested:
        wanted = requested.replace("@", "_").split("_")
        matches = [
            event for event in events
            if [event["away_team"], event["home_team"]] == wanted
        ]
    else:
        first = min(events, key=lambda event: event["kickoff"])
        matches = [
            event for event in events
            if event["away_team"] == first["away_team"]
            and event["home_team"] == first["home_team"]
        ]
    if not matches:
        raise RuntimeError("no open Kalshi receiving/rushing markets for requested game")
    return matches


def bovada_game(events, away, home):
    away_name, home_name = TEAM_NAMES[away], TEAM_NAMES[home]
    for event in events:
        description = event.get("description", "").lower()
        if away_name in description and home_name in description:
            return event
    raise RuntimeError(f"no Bovada game matched {away} @ {home}")


def bovada_pairs(event):
    grouped = defaultdict(lambda: defaultdict(dict))
    for row in selection_rows(event):
        if (
            row["prop_type"] not in {"receiving_yards", "rushing_yards"}
            or row["state"] != "open" or not row["decimal_odds"]
        ):
            continue
        key = normalize_player(row["player"]), row["prop_type"], float(row["line"])
        grouped[key][row["market_id"]][row["side"]] = row
    pairs = {}
    for key, markets in grouped.items():
        options = [pair for pair in markets.values() if set(pair) == {"over", "under"}]
        if options:
            pairs[key] = min(options, key=lambda pair: pair["over"]["is_alternate"])
    return pairs


def ladder_status(row, ladder, entry_side, entry_price):
    ordered = sorted(ladder, key=lambda item: item["threshold"])
    index = next(i for i, item in enumerate(ordered) if item["ticker"] == row["ticker"])
    neighbors = ordered[max(0, index - 1):index] + ordered[index + 1:index + 2]
    violation = False
    for neighbor in neighbors:
        price = (
            neighbor["yes_ask"] if entry_side == "yes"
            else 1 - neighbor["yes_bid"]
        )
        if neighbor["threshold"] < row["threshold"]:
            violation |= price + 0.01 < entry_price if entry_side == "yes" else price > entry_price + 0.01
        else:
            violation |= price > entry_price + 0.01 if entry_side == "yes" else price + 0.01 < entry_price
    return "violation" if violation else "ok" if neighbors else "unavailable"


def score_snapshot(kalshi, pair, ladder, quote_age, model):
    over_probability = no_vig(pair["over"]["decimal_odds"], pair["under"]["decimal_odds"])
    yes_edge = over_probability - kalshi["yes_ask"]
    no_edge = kalshi["yes_bid"] - over_probability
    entry_side = "yes" if yes_edge >= no_edge else "no"
    entry_price = kalshi["yes_ask"] if entry_side == "yes" else 1 - kalshi["yes_bid"]
    fair = over_probability if entry_side == "yes" else 1 - over_probability
    size = kalshi["yes_ask_size"] if entry_side == "yes" else kalshi["yes_bid_size"]
    edge = fair - entry_price
    net_edge = edge - 0.07 * entry_price * (1 - entry_price) - 0.01
    ladder = ladder_status(kalshi, ladder, entry_side, entry_price)
    quality = 0.4
    quality *= 1.0 if kalshi["spread"] <= 0.05 else 0.5
    quality *= min(1.0, size / 50)
    quality *= 0.5 if ladder == "violation" else 1.0
    signal_score = round(100 * max(0.0, min(1.0, net_edge / 0.10)) * quality)
    rule_tier = "watch" if net_edge > 0 and ladder != "violation" else "pass"
    features = {
        "gross_disagreement": edge,
        "bovada_move": 0.0,
        "kalshi_spread": kalshi["spread"],
        "kalshi_available_size": size,
        "kalshi_quote_age_seconds": quote_age,
        "bovada_fair_probability": fair,
        "kalshi_executable_price": entry_price,
        "repricing_selection_count": 2,
        "repricing_scope": "isolated",
        "ladder_check": ladder,
        "entry_side": entry_side,
        "prop_type": kalshi["prop_type"],
    }
    prediction = model_predict(model, features)
    combined, basis = combine(rule_tier, prediction)
    return {
        "player": kalshi["player"],
        "prop_type": kalshi["prop_type"],
        "threshold": kalshi["threshold"],
        "ticker": kalshi["ticker"],
        "entry_side": entry_side,
        "executable_price": entry_price,
        "available_size": size,
        "spread": kalshi["spread"],
        "bovada_fair_probability": fair,
        "gross_disagreement": edge,
        "net_edge": net_edge,
        "ladder_check": ladder,
        "rule_tier": rule_tier,
        "signal_score": signal_score,
        "model_predicted_markout_10s": prediction,
        "combined_recommendation": combined,
        "combined_basis": basis,
        "repricing_context": "unavailable_initial_snapshot",
    }


def live_snapshot(game=None, date=None, model_path=None, top=None):
    model_path = model_path or ROOT / "model_output/player_prop_markout_v2/model.json"
    model = json.loads(Path(model_path).read_text())
    session = requests.Session()
    all_events = discover(session, SimpleNamespace(game=game, today=False))
    events = choose_game(all_events, game)
    away, home = events[0]["away_team"], events[0]["home_team"]
    date = date or events[0]["kickoff"].tz_convert(ET).date().isoformat()

    kalshi_at = pd.Timestamp.now(tz="UTC")
    kalshi_rows = []
    for event in events:
        rows, _ = snapshot_rows(session, event, kalshi_at)
        kalshi_rows.extend(row for row in rows if row["yes_bid"] is not None)
    bovada_events, bovada_at_text = fetch_sunday_events(date, [])
    bovada_at = pd.Timestamp(bovada_at_text)
    bovada_event = bovada_game(bovada_events, away, home)
    pairs = bovada_pairs(bovada_event)

    kalshi_by_key = {}
    ladders = defaultdict(list)
    for row in kalshi_rows:
        key = normalize_player(row["player"]), row["prop_type"], float(row["threshold"])
        kalshi_by_key[key] = row
        ladders[key[:2]].append(row)
    matched = sorted(set(kalshi_by_key) & set(pairs))
    quote_age = max(0.0, (bovada_at - kalshi_at).total_seconds())
    opportunities = [
        score_snapshot(
            kalshi_by_key[key], pairs[key], ladders[key[:2]], quote_age, model
        )
        for key in matched
    ]
    opportunities.sort(
        key=lambda row: (
            {"strong": 3, "enter": 2, "watch": 1, "pass": 0}[row["combined_recommendation"]],
            row["model_predicted_markout_10s"],
        ),
        reverse=True,
    )
    result = {
        "mode": "read_only_shadow",
        "game": f"{away} @ {home}",
        "game_date": date,
        "kalshi_timestamp": kalshi_at.isoformat(),
        "bovada_timestamp": bovada_at.isoformat(),
        "timestamp_skew_seconds": abs((bovada_at - kalshi_at).total_seconds()),
        "model": str(model_path),
        "verification": {
            "kalshi_markets": len(kalshi_rows),
            "kalshi_executable_quotes": sum(
                row["yes_bid_size"] > 0 and row["yes_ask_size"] > 0
                for row in kalshi_rows
            ),
            "bovada_two_sided_props": len(pairs),
            "exact_player_prop_threshold_matches": len(matched),
            "features_generated": len(opportunities),
            "feature_count": len(FEATURES),
            "orders_submitted": 0,
        },
        "opportunities": opportunities if top is None else opportunities[:top],
    }
    return result


def main():
    args = parse_args()
    result = live_snapshot(args.game, args.date, args.model, args.top)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, default=str, sort_keys=True) + "\n")

    print(
        f'{result["game"]}: '
        f'{result["verification"]["exact_player_prop_threshold_matches"]} exact match(es), '
        f'skew {result["timestamp_skew_seconds"]:.2f}s'
    )
    print("tier   model10 combined side price fair edge spread size player / prop")
    for row in result["opportunities"]:
        prop = "rec" if row["prop_type"] == "receiving_yards" else "rush"
        print(
            f'{row["rule_tier"]:<6} {row["model_predicted_markout_10s"] * 100:+6.2f}c '
            f'{row["combined_recommendation"]:<8} {row["entry_side"].upper():<3} '
            f'{row["executable_price"]:.2f} {row["bovada_fair_probability"]:.2f} '
            f'{row["gross_disagreement"]:+.2f} {row["spread"]:.2f} '
            f'{row["available_size"]:.0f} {row["player"]} {prop} >{row["threshold"]:g}'
        )
    print(json.dumps(result["verification"], sort_keys=True))


if __name__ == "__main__":
    main()
