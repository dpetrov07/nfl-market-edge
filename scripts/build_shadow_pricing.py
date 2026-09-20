"""Build timestamp-safe RFQ decisions and conservative sportsbook shadow quotes."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import json
import math
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

from nfl_market_edge.combo import fee_per_contract, score_snapshot
from nfl_market_edge.shadow import (
    INTERPOLATION_UNCERTAINTY,
    LEG_UNCERTAINTY,
    MAX_SPORTSBOOK_AGE_SECONDS,
    MIN_BOOKS_PER_LEG,
    MIN_SELLER_EDGE,
    ONE_WAY_UNCERTAINTY,
    SUPPORTED_BOOKS,
    component_identity,
    devig_probability,
    implied_probability,
    normalize_player,
    price_external_combo,
)
from scripts.collect_live_combo_slate import read_manifest


HORIZONS = (10, 30, 60)


def parse_time(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (TypeError, ValueError):
        return None


def number(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def read_jsonl(paths: list[Path]):
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            while True:
                try:
                    line = handle.readline()
                except EOFError:
                    break
                if not line:
                    break
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    break


def latest(history: list[tuple[datetime, dict]], at: datetime) -> dict | None:
    index = bisect_right(history, at, key=lambda item: item[0]) - 1
    return history[index][1] if index >= 0 else None


class SportsbookHistory:
    """As-of lookup with exact prices preferred over nearby alt-line estimates."""

    MAX_INTERPOLATION_SPAN = {
        "passing_yards": 50.0,
        "receiving_yards": 30.0,
        "rushing_yards": 30.0,
        "receptions": 3.0,
        "passing_touchdowns": 2.0,
        "passing_interceptions": 2.0,
        "spread": 10.0,
        "game_total": 10.0,
        "team_total": 10.0,
    }

    def __init__(self, paths: list[Path], slate_id: str):
        self.histories = defaultdict(list)
        self.candidates = defaultdict(set)
        self.lines = defaultdict(set)
        for row in read_jsonl(paths):
            if row.get("record_type") != "selection_state":
                continue
            if row.get("slate_id") not in {None, slate_id}:
                continue
            at = parse_time(row.get("received_at"))
            line = number(row.get("line"))
            side = row.get("side")
            book = row.get("sportsbook")
            market_id = row.get("market_id")
            player_key = normalize_player(row.get("player"))
            if not at or line is None or side not in {"over", "under"}:
                continue
            if not book or not market_id or not player_key or not row.get("prop_type"):
                continue
            base = (player_key, row["prop_type"], round(line, 3))
            key = (*base, book, str(market_id), side)
            self.histories[key].append((at, {**row, "_at": at}))
            self.candidates[base].add((book, str(market_id)))
            self.lines[(player_key, row["prop_type"], book)].add(round(line, 3))
        for history in self.histories.values():
            history.sort(key=lambda item: item[0])

    def _quote_at(self, leg: dict, at: datetime, book: str, line: float) -> dict | None:
        base = (leg["player_key"], leg["prop_type"], round(line, 3))
        best = None
        for candidate_book, market_id in self.candidates.get(base, ()):
            if candidate_book != book:
                continue
            over = latest(self.histories[(*base, book, market_id, "over")], at)
            under = latest(self.histories[(*base, book, market_id, "under")], at)
            over = over if over and over.get("state") == "open" else None
            under = under if under and under.get("state") == "open" else None
            selected = over if leg["sportsbook_side"] == "over" else under
            opposite = under if leg["sportsbook_side"] == "over" else over
            method = "exact_two_way"
            uncertainty = 0.0
            probability = devig_probability(
                number(over.get("decimal_odds")) if over else None,
                number(under.get("decimal_odds")) if under else None,
                leg["sportsbook_side"],
            )
            if probability is None and selected:
                probability = number(selected.get("fair_probability"))
                method = "exact_multiway_devig"
                uncertainty = LEG_UNCERTAINTY
            if probability is None and opposite:
                fair = number(opposite.get("fair_probability"))
                if fair is not None:
                    probability = 1 - fair
                    method = "exact_multiway_devig"
                    uncertainty = LEG_UNCERTAINTY
            if probability is None and selected:
                probability = implied_probability(number(selected.get("decimal_odds")))
                method = "exact_one_way"
                uncertainty = ONE_WAY_UNCERTAINTY
            if probability is None and opposite:
                raw = implied_probability(number(opposite.get("decimal_odds")))
                probability = 1 - raw if raw is not None else None
                method = "exact_one_way_complement"
                uncertainty = ONE_WAY_UNCERTAINTY
            if probability is None:
                continue
            used = [row for row in (over, under) if row]
            age = max((at - row["_at"]).total_seconds() for row in used)
            candidate = {
                "sportsbook": book,
                "market_id": market_id,
                "game": next((row.get("game") for row in used if row.get("game")), None),
                "line": line,
                "over_decimal_odds": number(over.get("decimal_odds")) if over else None,
                "under_decimal_odds": number(under.get("decimal_odds")) if under else None,
                "devig_probability": probability,
                "price_method": method,
                "probability_uncertainty": uncertainty,
                "age_seconds": age,
                "observed_at": max(row["_at"] for row in used).isoformat(),
            }
            if best is None or age < best["age_seconds"]:
                best = candidate
        return best

    def quotes(self, leg: dict, at: datetime) -> list[dict]:
        target = round(leg["line"], 3)
        by_book = {}
        for book in SUPPORTED_BOOKS:
            exact = self._quote_at(leg, at, book, target)
            if exact:
                by_book[book] = exact
                continue
            available = sorted(
                self.lines.get((leg["player_key"], leg["prop_type"], book), ())
            )
            lower = max((line for line in available if line < target), default=None)
            upper = min((line for line in available if line > target), default=None)
            maximum_span = self.MAX_INTERPOLATION_SPAN.get(leg["prop_type"])
            if (
                lower is None
                or upper is None
                or maximum_span is None
                or upper - lower > maximum_span
            ):
                continue
            low_quote = self._quote_at(leg, at, book, lower)
            high_quote = self._quote_at(leg, at, book, upper)
            if not low_quote or not high_quote:
                continue
            weight = (target - lower) / (upper - lower)
            probability = low_quote["devig_probability"] + weight * (
                high_quote["devig_probability"] - low_quote["devig_probability"]
            )
            by_book[book] = {
                "sportsbook": book,
                "market_id": None,
                "game": low_quote.get("game") or high_quote.get("game"),
                "line": target,
                "source_lines": [lower, upper],
                "devig_probability": probability,
                "price_method": "interpolated_alt_lines",
                "probability_uncertainty": max(
                    low_quote["probability_uncertainty"],
                    high_quote["probability_uncertainty"],
                ) + INTERPOLATION_UNCERTAINTY,
                "age_seconds": max(low_quote["age_seconds"], high_quote["age_seconds"]),
                "observed_at": max(low_quote["observed_at"], high_quote["observed_at"]),
            }
        return [by_book[book] for book in SUPPORTED_BOOKS if book in by_book]


def kalshi_book(row: dict, at: datetime) -> dict:
    return {
        "at": at,
        "yes_bid": number(row.get("yes_bid_dollars")),
        "yes_bid_size": number(row.get("yes_bid_size")),
        "yes_ask": number(row.get("yes_ask_dollars")),
        "yes_ask_size": number(row.get("yes_ask_size")),
    }


def book_mid(book: dict | None) -> float | None:
    if not book or book["yes_bid"] is None or book["yes_ask"] is None:
        return None
    return (book["yes_bid"] + book["yes_ask"]) / 2


def component_snapshot(
    combo: dict,
    book_histories: dict[str, list[tuple[datetime, dict]]],
    at: datetime,
) -> dict:
    values = []
    for leg in combo.get("mve_selected_legs") or []:
        book = latest(book_histories.get(leg.get("market_ticker"), []), at)
        if not book:
            return {}
        if leg.get("side") == "no":
            bid = 1 - book["yes_ask"] if book["yes_ask"] is not None else None
            ask = 1 - book["yes_bid"] if book["yes_bid"] is not None else None
        else:
            bid, ask = book["yes_bid"], book["yes_ask"]
        if bid is None or ask is None:
            return {}
        values.append(
            {
                "bid": bid,
                "mid": (bid + ask) / 2,
                "ask": ask,
                "spread": ask - bid,
                "age": (at - book["at"]).total_seconds(),
            }
        )
    if not values:
        return {}
    product = lambda key: math.prod(value[key] for value in values)
    return {
        "component_bid_product": product("bid"),
        "component_mid_product": product("mid"),
        "component_ask_product": product("ask"),
        "max_leg_spread": max(value["spread"] for value in values),
        "max_quote_age_seconds": max(value["age"] for value in values),
    }


def first_after(rows: list[dict], at: datetime, seconds: float) -> dict | None:
    end = at + timedelta(seconds=seconds)
    for row in rows:
        if at <= row["_at"] <= end:
            return row
        if row["_at"] > end:
            break
    return None


def build_decision_rows(
    manifest: dict,
    kalshi_path: Path,
    sportsbook_paths: list[Path],
    *,
    min_books_per_leg: int = MIN_BOOKS_PER_LEG,
    max_sportsbook_age_seconds: float = MAX_SPORTSBOOK_AGE_SECONDS,
    minimum_edge: float = MIN_SELLER_EDGE,
    trade_window_seconds: float = 60.0,
) -> list[dict]:
    records = list(read_jsonl([kalshi_path]))
    combos, components, settlements = {}, {}, {}
    book_histories = defaultdict(list)
    trades = defaultdict(list)
    communications = defaultdict(list)
    opportunities = {}
    capture_end = max(
        (at for record in records if (at := parse_time(record.get("received_at")))),
        default=None,
    )

    for record in records:
        kind = record.get("record_type")
        at = parse_time(record.get("received_at"))
        if kind == "combo_discovery":
            combo = record.get("combo", {})
            if combo.get("ticker"):
                combos[combo["ticker"]] = combo
        elif kind == "component_discovery":
            for market in record.get("markets", []):
                ticker = market.get("ticker") or market.get("market_ticker")
                if ticker:
                    components[ticker] = market
        elif kind == "top_of_book" and at and record.get("market_ticker"):
            ticker = record["market_ticker"]
            book_histories[ticker].append((at, kalshi_book(record, at)))
        elif kind in {"trade", "fill"} and at and record.get("market_ticker"):
            trades[record["market_ticker"]].append({**record, "_at": at})
        elif kind == "communication" and at:
            rfq_id = record.get("rfq_id")
            if rfq_id:
                communications[str(rfq_id)].append({**record, "_at": at})
            if record.get("communication_type") in {"rfq_created", "rfq_snapshot"}:
                identity = str(rfq_id or f"{record.get('market_ticker')}|{at.isoformat()}")
                existing = opportunities.get(identity)
                if existing is None or at < existing["_at"]:
                    opportunities[identity] = {**record, "_at": at}
        elif kind == "market_status" and record.get("market_ticker"):
            value = number(record.get("settlement_value_dollars"))
            if value is None and record.get("result") in {"yes", "no"}:
                value = float(record["result"] == "yes")
            if value is not None:
                settlements[record["market_ticker"]] = value

    for histories in (book_histories,):
        for history in histories.values():
            history.sort(key=lambda item: item[0])
    for rows in trades.values():
        rows.sort(key=lambda row: row["_at"])
    for rows in communications.values():
        rows.sort(key=lambda row: row["_at"])

    sportsbooks = SportsbookHistory(sportsbook_paths, manifest["slate_id"])
    output = []
    for identity, opportunity in sorted(opportunities.items(), key=lambda item: item[1]["_at"]):
        ticker = opportunity.get("market_ticker")
        combo = combos.get(ticker)
        at = opportunity["_at"]
        if not combo:
            continue
        combo_legs = combo.get("mve_selected_legs") or []
        games = [leg.get("game") for leg in combo_legs if leg.get("game")]
        distinct_games = len(set(games))
        scope = "same_game" if distinct_games == 1 else "cross_game"
        configured_scope = manifest.get("combo_scope", "cross_game")
        if len(combo_legs) not in {2, 3} or (
            configured_scope != "any" and scope != configured_scope
        ):
            continue

        external_legs = []
        identity_errors = []
        for index, leg in enumerate(combo_legs, start=1):
            parsed, error = component_identity(
                leg, components.get(leg.get("market_ticker"), {})
            )
            if error:
                identity_errors.append(f"leg_{index}_{error}")
                continue
            parsed["book_quotes"] = sportsbooks.quotes(parsed, at)
            external_legs.append(parsed)

        if identity_errors:
            external = {
                "fair_value": None,
                "fair_value_low": None,
                "fair_value_high": None,
                "fair_value_method": None,
                "stale_probability_buffer": None,
                "conservative_probability": None,
                "minimum_desired_edge": minimum_edge,
                "proposed_yes_sell_price": None,
                "proposed_fee": None,
                "proposed_edge_vs_consensus": None,
                "books_available": sorted(
                    {q["sportsbook"] for leg in external_legs for q in leg["book_quotes"]}
                ),
                "minimum_books_on_any_leg": 0,
                "maximum_sportsbook_age_seconds": None,
                "skip_reason": ";".join(identity_errors),
                "legs": external_legs,
            }
        else:
            external = price_external_combo(
                external_legs,
                min_books_per_leg=min_books_per_leg,
                max_age_seconds=max_sportsbook_age_seconds,
                minimum_edge=minimum_edge,
            )
        if scope == "same_game":
            external["fair_value_method"] = (
                "naive_independent_leg_product_not_correlation_adjusted"
            )
            external["proposed_yes_sell_price"] = None
            external["proposed_fee"] = None
            external["proposed_edge_vs_consensus"] = None
            reason = "same_game_correlation_not_modeled"
            if external.get("skip_reason"):
                reason += ";" + external["skip_reason"]
            external["skip_reason"] = reason

        combo_book = latest(book_histories.get(ticker, []), at)
        component = component_snapshot(combo, book_histories, at)
        rfq_rows = communications.get(str(opportunity.get("rfq_id")), [])
        actual_quote = next(
            (
                row
                for row in rfq_rows
                if row.get("communication_type", "").startswith("quote_")
                and number(row.get("yes_bid_dollars")) is not None
            ),
            None,
        )
        observed_trade = first_after(trades.get(ticker, []), at, trade_window_seconds)
        observed_trade_price = (
            number(observed_trade.get("yes_price_dollars")) if observed_trade else None
        )
        observed_quote_price = (
            number(actual_quote.get("yes_bid_dollars")) if actual_quote else None
        )
        observed_quote_no_price = (
            number(actual_quote.get("no_bid_dollars")) if actual_quote else None
        )
        benchmark_price = observed_trade_price
        benchmark_source = "next_trade" if observed_trade_price is not None else None
        if benchmark_price is None and combo_book and combo_book["yes_bid"] is not None:
            benchmark_price = combo_book["yes_bid"]
            benchmark_source = "decision_time_combo_bid"

        frozen = None
        if benchmark_price is not None:
            frozen = score_snapshot(
                yes_price=benchmark_price,
                scope=scope,
                leg_count=len(combo_legs),
                distinct_leg_games=len(set(games)),
                component_bid_product=component.get("component_bid_product"),
                component_mid_product=component.get("component_mid_product"),
                component_ask_product=component.get("component_ask_product"),
                max_quote_age_seconds=component.get("max_quote_age_seconds"),
                max_leg_spread=component.get("max_leg_spread"),
                combo_bid=combo_book.get("yes_bid") if combo_book else None,
                combo_ask=combo_book.get("yes_ask") if combo_book else None,
                combo_quote_age_seconds=(at - combo_book["at"]).total_seconds()
                if combo_book
                else None,
            )

        settlement = settlements.get(ticker)
        proposed = external.get("proposed_yes_sell_price")
        row = {
            "slate_id": manifest["slate_id"],
            "opportunity_id": identity,
            "opportunity_type": opportunity.get("communication_type"),
            "observed_at": at,
            "exchange_timestamp": opportunity.get("exchange_timestamp"),
            "rfq_id": opportunity.get("rfq_id"),
            "combo_market_ticker": ticker,
            "rfq_size": number(opportunity.get("contracts")),
            "rfq_target_cost": number(opportunity.get("target_cost_dollars")),
            "leg_count": len(combo_legs),
            "distinct_leg_games": distinct_games,
            "scope": scope,
            "games": ", ".join(sorted(set(games))),
            "external_fair_value": external.get("fair_value"),
            "external_fair_value_low": external.get("fair_value_low"),
            "external_fair_value_high": external.get("fair_value_high"),
            "external_fair_value_method": external.get("fair_value_method"),
            "stale_probability_buffer": external.get("stale_probability_buffer"),
            "conservative_probability": external.get("conservative_probability"),
            "minimum_desired_edge": external.get("minimum_desired_edge"),
            "proposed_yes_sell_price": proposed,
            "proposed_fee": external.get("proposed_fee"),
            "proposed_edge_vs_consensus": external.get("proposed_edge_vs_consensus"),
            "shadow_action": "quote" if proposed is not None else "skip",
            "skip_reason": external.get("skip_reason"),
            "books_available": ",".join(external.get("books_available", [])),
            "minimum_books_on_any_leg": external.get("minimum_books_on_any_leg"),
            "maximum_sportsbook_age_seconds": external.get("maximum_sportsbook_age_seconds"),
            "leg_pricing_json": json.dumps(external.get("legs", []), sort_keys=True),
            "market_yes_bid": combo_book.get("yes_bid") if combo_book else None,
            "market_yes_ask": combo_book.get("yes_ask") if combo_book else None,
            "market_midpoint": book_mid(combo_book),
            "market_quote_age_seconds": (at - combo_book["at"]).total_seconds()
            if combo_book
            else None,
            "observed_quote_yes_price": observed_quote_price,
            "observed_quote_no_price": observed_quote_no_price,
            "observed_quote_status": actual_quote.get("status") if actual_quote else None,
            "observed_quote_accepted_side": actual_quote.get("accepted_side")
            if actual_quote
            else None,
            "observed_quote_accepted": any(
                row.get("communication_type") in {"quote_accepted", "quote_executed"}
                for row in rfq_rows
            ),
            "observed_trade_yes_price": observed_trade_price,
            "observed_trade_at": observed_trade["_at"] if observed_trade else None,
            "observed_trade_size": number(observed_trade.get("count"))
            if observed_trade
            else None,
            "observed_trade_taker_side": (
                observed_trade.get("taker_outcome_side") or observed_trade.get("side")
                if observed_trade
                else None
            ),
            "benchmark_yes_price": benchmark_price,
            "benchmark_price_source": benchmark_source,
            "frozen_below_10c_candidate": frozen is not None
            and frozen["historical_net_edge_prior"] is not None,
            "frozen_recommendation": frozen.get("recommendation") if frozen else None,
            "frozen_reason": frozen.get("reason") if frozen else "no observable benchmark price",
            "settlement_value": settlement,
            "shadow_settlement_pnl_if_filled": (
                proposed - settlement - fee_per_contract(proposed)
                if proposed is not None and settlement is not None
                else None
            ),
            "observed_trade_settlement_pnl": (
                observed_trade_price
                - settlement
                - fee_per_contract(observed_trade_price)
                if observed_trade_price is not None and settlement is not None
                else None
            ),
            "frozen_benchmark_settlement_pnl": (
                benchmark_price - settlement - fee_per_contract(benchmark_price)
                if frozen is not None
                and frozen["historical_net_edge_prior"] is not None
                and settlement is not None
                else None
            ),
        }
        for horizon in HORIZONS:
            target = at + timedelta(seconds=horizon)
            future = (
                latest(book_histories.get(ticker, []), target)
                if capture_end is not None and target <= capture_end
                else None
            )
            midpoint = book_mid(future)
            row[f"market_midpoint_{horizon}s"] = midpoint
            row[f"shadow_quote_markout_{horizon}s"] = (
                proposed - midpoint
                if proposed is not None and midpoint is not None
                else None
            )
            trade_target = (
                observed_trade["_at"] + timedelta(seconds=horizon)
                if observed_trade
                else None
            )
            trade_future = (
                latest(book_histories.get(ticker, []), trade_target)
                if trade_target is not None
                and capture_end is not None
                and trade_target <= capture_end
                else None
            )
            trade_midpoint = book_mid(trade_future)
            row[f"observed_trade_market_midpoint_{horizon}s"] = trade_midpoint
            row[f"observed_trade_markout_{horizon}s"] = (
                observed_trade_price - trade_midpoint
                if observed_trade_price is not None and trade_midpoint is not None
                else None
            )
        output.append(row)
    return output


def summary(rows: list[dict]) -> dict:
    skips = Counter(row["skip_reason"] for row in rows if row["skip_reason"])
    settled_shadow = [
        row["shadow_settlement_pnl_if_filled"]
        for row in rows
        if row["shadow_settlement_pnl_if_filled"] is not None
    ]
    settled_frozen = [
        row["frozen_benchmark_settlement_pnl"]
        for row in rows
        if row["frozen_benchmark_settlement_pnl"] is not None
    ]
    return {
        "opportunities": len(rows),
        "external_values": sum(row["external_fair_value"] is not None for row in rows),
        "shadow_quotes": sum(row["shadow_action"] == "quote" for row in rows),
        "frozen_below_10c_candidates": sum(row["frozen_below_10c_candidate"] for row in rows),
        "settled_opportunities": sum(row["settlement_value"] is not None for row in rows),
        "skip_reasons": dict(sorted(skips.items())),
        "shadow_mean_settlement_pnl_if_filled": (
            sum(settled_shadow) / len(settled_shadow) if settled_shadow else None
        ),
        "frozen_mean_settlement_pnl": (
            sum(settled_frozen) / len(settled_frozen) if settled_frozen else None
        ),
        "warning": "shadow P&L assumes every proposed quote filled; opportunity rows are not independent fills",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--kalshi-input", type=Path)
    parser.add_argument("--sportsbook-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--min-books-per-leg", type=int, default=MIN_BOOKS_PER_LEG)
    parser.add_argument(
        "--max-sportsbook-age-seconds",
        type=float,
        default=MAX_SPORTSBOOK_AGE_SECONDS,
    )
    parser.add_argument("--minimum-edge", type=float, default=MIN_SELLER_EDGE)
    parser.add_argument("--trade-window-seconds", type=float, default=60.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = read_manifest(args.manifest)
    slate_root = ROOT / "data/live/combo_slates" / manifest["slate_id"]
    kalshi = args.kalshi_input or slate_root / "kalshi/events.jsonl.gz"
    sportsbook_root = args.sportsbook_root or slate_root / "sportsbooks"
    sportsbook_paths = sorted(sportsbook_root.glob("*/*.jsonl.gz"))
    rows = build_decision_rows(
        manifest,
        kalshi,
        sportsbook_paths,
        min_books_per_leg=args.min_books_per_leg,
        max_sportsbook_age_seconds=args.max_sportsbook_age_seconds,
        minimum_edge=args.minimum_edge,
        trade_window_seconds=args.trade_window_seconds,
    )
    output = args.output_dir or ROOT / "research/output" / manifest["slate_id"] / "shadow"
    output.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    frame.to_parquet(output / "shadow_decisions.parquet", index=False)
    frame.to_csv(output / "shadow_decisions.csv", index=False)
    report = {
        "slate_id": manifest["slate_id"],
        "kalshi_input": str(kalshi),
        "sportsbook_inputs": [str(path) for path in sportsbook_paths],
        "configuration": {
            "min_books_per_leg": args.min_books_per_leg,
            "max_sportsbook_age_seconds": args.max_sportsbook_age_seconds,
            "minimum_edge": args.minimum_edge,
            "markout_horizons_seconds": list(HORIZONS),
        },
        **summary(rows),
    }
    (output / "shadow_summary.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n"
    )
    print(json.dumps({key: value for key, value in report.items() if key != "sportsbook_inputs"}))


if __name__ == "__main__":
    main()
