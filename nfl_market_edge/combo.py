"""Conservative live/RFQ scoring scaffold for cross-game Kalshi combos."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


FEE_RATE = 0.07
MAX_QUOTE_AGE_SECONDS = 30.0
MAX_LEG_SPREAD = 0.05
FROZEN_PRICE_CEILING = 0.10
FROZEN_BENCHMARK = {
    "source": "2026-09-13 settled Sunday slate",
    "combos": 4864,
    "fills": 22016,
    "equal_combo_net_edge": 0.017657304000917744,
    "volume_weighted_net_edge": 0.0118671227710273,
    "loss_rate": 0.032689144736842105,
    "mean_gain": 0.049580336836630316,
    "mean_loss": -0.9269833846281872,
}


def fee_per_contract(price: float, rate: float = FEE_RATE):
    return rate * price * (1 - price)


def _valid_probability(value):
    return value is not None and 0 <= value <= 1


def score_snapshot(
    *,
    yes_price: float,
    scope: str,
    leg_count: int,
    distinct_leg_games: int,
    component_bid_product: float | None = None,
    component_mid_product: float | None = None,
    component_ask_product: float | None = None,
    max_quote_age_seconds: float | None = None,
    max_leg_spread: float | None = None,
    available_size: float | None = None,
    combo_bid: float | None = None,
    combo_ask: float | None = None,
    combo_quote_age_seconds: float | None = None,
):
    """Score a combo without pretending the one-slate research is a trained model."""
    if not _valid_probability(yes_price):
        raise ValueError("yes_price must be between 0 and 1")

    component_complete = all(
        _valid_probability(value)
        for value in (
            component_bid_product,
            component_mid_product,
            component_ask_product,
        )
    )
    if component_complete and not (
        component_bid_product <= component_mid_product <= component_ask_product
    ):
        raise ValueError("component bid/mid/ask products must be ordered")

    combo_book_complete = _valid_probability(combo_bid) and _valid_probability(
        combo_ask
    )
    if combo_book_complete and combo_bid > combo_ask:
        raise ValueError("combo_bid cannot exceed combo_ask")
    combo_book_quality_pass = (
        combo_book_complete
        and combo_quote_age_seconds is not None
        and combo_quote_age_seconds <= MAX_QUOTE_AGE_SECONDS
        and combo_ask - combo_bid <= MAX_LEG_SPREAD
    )

    independent_legs = distinct_leg_games == leg_count
    quote_quality_pass = (
        component_complete
        and max_quote_age_seconds is not None
        and max_quote_age_seconds <= MAX_QUOTE_AGE_SECONDS
        and max_leg_spread is not None
        and max_leg_spread <= MAX_LEG_SPREAD
    )

    if combo_book_quality_pass:
        fair_value = (combo_bid + combo_ask) / 2
        fair_low, fair_high = combo_bid, combo_ask
        fair_method = "combo_book_midpoint"
    elif component_complete and independent_legs:
        fair_value = component_mid_product
        fair_low, fair_high = component_bid_product, component_ask_product
        fair_method = "independent_component_midpoint_product"
    elif combo_book_complete:
        fair_value = (combo_bid + combo_ask) / 2
        fair_low, fair_high = combo_bid, combo_ask
        fair_method = "stale_or_wide_combo_book_midpoint"
    else:
        fair_value = fair_low = fair_high = None
        fair_method = "unavailable"

    fee = fee_per_contract(yes_price)
    structural_edge = (
        yes_price - fair_value - fee if fair_value is not None else None
    )
    frozen_candidate = scope == "cross_game" and yes_price < FROZEN_PRICE_CEILING
    expected_edge = (
        FROZEN_BENCHMARK["equal_combo_net_edge"] if frozen_candidate else None
    )

    if not frozen_candidate:
        recommendation = "pass"
        reason = "outside the frozen cross-game YES below 10 cents benchmark"
    elif not independent_legs and not combo_book_quality_pass:
        recommendation = "watch"
        reason = "same-game leg dependence needs a joint probability estimate"
    elif not quote_quality_pass and not combo_book_quality_pass:
        recommendation = "watch"
        reason = "component book is missing, stale, or too wide"
    else:
        recommendation = "consider"
        reason = "passes the frozen price rule and operational quote checks"

    return {
        "yes_price": yes_price,
        "scope": scope,
        "leg_count": leg_count,
        "distinct_leg_games": distinct_leg_games,
        "fair_value": fair_value,
        "fair_value_low": fair_low,
        "fair_value_high": fair_high,
        "fair_value_method": fair_method,
        "structural_seller_edge_after_fee": structural_edge,
        "entry_fee": fee,
        "expected_net_edge": expected_edge,
        "expected_edge_method": (
            "frozen historical group mean" if frozen_candidate else "unavailable"
        ),
        "historical_net_edge_prior": expected_edge,
        "historical_prior_basis": (
            "frozen below-10-cent cross-game group" if frozen_candidate else None
        ),
        "uncertainty": {
            "confidence": "low" if recommendation == "consider" else "insufficient",
            "quote_quality_pass": bool(quote_quality_pass),
            "combo_book_quality_pass": bool(combo_book_quality_pass),
            "independent_leg_product_valid": bool(independent_legs),
            "max_quote_age_seconds": max_quote_age_seconds,
            "max_leg_spread": max_leg_spread,
            "combo_quote_age_seconds": combo_quote_age_seconds,
            "historical_loss_rate": (
                FROZEN_BENCHMARK["loss_rate"] if frozen_candidate else None
            ),
            "historical_mean_loss": (
                FROZEN_BENCHMARK["mean_loss"] if frozen_candidate else None
            ),
            "note": "one-slate research prior; not a calibrated confidence interval",
        },
        "available_size": available_size,
        "recommendation": recommendation,
        "worth_considering": recommendation == "consider",
        "reason": reason,
        "component_premium_used_for_selection": False,
    }


def score_legs(
    *,
    yes_price: float,
    scope: str,
    leg_quotes: list[dict],
    available_size: float | None = None,
    combo_bid: float | None = None,
    combo_ask: float | None = None,
    combo_quote_age_seconds: float | None = None,
):
    """Convert selected-side leg books into products, then score the combo."""
    if not leg_quotes:
        raise ValueError("leg_quotes cannot be empty")
    normalized = []
    for leg in leg_quotes:
        bid, ask = leg.get("bid"), leg.get("ask")
        if not _valid_probability(bid) or not _valid_probability(ask) or bid > ask:
            raise ValueError("each leg requires ordered bid and ask probabilities")
        normalized.append(
            {
                **leg,
                "mid": (bid + ask) / 2,
                "spread": ask - bid,
            }
        )
    ages = [leg.get("age_seconds") for leg in normalized]
    return score_snapshot(
        yes_price=yes_price,
        scope=scope,
        leg_count=len(normalized),
        distinct_leg_games=len({leg["game"] for leg in normalized}),
        component_bid_product=math.prod(leg["bid"] for leg in normalized),
        component_mid_product=math.prod(leg["mid"] for leg in normalized),
        component_ask_product=math.prod(leg["ask"] for leg in normalized),
        max_quote_age_seconds=max(ages) if all(age is not None for age in ages) else None,
        max_leg_spread=max(leg["spread"] for leg in normalized),
        available_size=available_size,
        combo_bid=combo_bid,
        combo_ask=combo_ask,
        combo_quote_age_seconds=combo_quote_age_seconds,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        help="JSON snapshot path; omit to read JSON from stdin",
    )
    args = parser.parse_args()
    payload = json.loads(args.input.read_text()) if args.input else json.load(sys.stdin)
    print(json.dumps(score_legs(**payload), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
