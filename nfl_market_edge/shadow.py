"""Transparent sportsbook fair values and read-only combo quote prices."""

from __future__ import annotations

import math
import re
import statistics
import unicodedata

from nfl_market_edge.combo import FEE_RATE, fee_per_contract


SUPPORTED_BOOKS = ("bovada", "fanduel", "betrivers")
MAX_SPORTSBOOK_AGE_SECONDS = 120.0
MIN_BOOKS_PER_LEG = 2
LEG_UNCERTAINTY = 0.01
ONE_WAY_UNCERTAINTY = 0.03
INTERPOLATION_UNCERTAINTY = 0.02
STALE_BUFFER_AT_LIMIT = 0.01
MIN_SELLER_EDGE = 0.01
PLAYER_ALIASES = {
    "joshuapalmer": "joshpalmer",
    "kennygainwell": "kennethgainwell",
    "hollywoodbrown": "marquisebrown",
    "notouchdownscorer": "notouchdown",
}

SERIES_PROP_TYPES = {
    "KXNFLGAME": "moneyline",
    "KXNFLSPREAD": "spread",
    "KXNFLTOTAL": "game_total",
    "KXNFLTEAMTOTAL": "team_total",
    "KXNFLTD": "anytime_touchdown",
    "KXNFLFIRSTTD": "first_touchdown",
    "KXNFLRECYDS": "receiving_yards",
    "KXNFLRSHYDS": "rushing_yards",
    "KXNFLPASSYDS": "passing_yards",
    "KXNFLPASSYARDS": "passing_yards",
    "KXNFLREC": "receptions",
    "KXNFLRECEPTIONS": "receptions",
    "KXNFLPASSTD": "passing_touchdowns",
    "KXNFLPASSTDS": "passing_touchdowns",
    "KXNFLPASSINGTDS": "passing_touchdowns",
    "KXNFLPASSINT": "passing_interceptions",
}


def normalize_player(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    parts = re.findall(r"[a-z0-9]+", text.lower())
    while parts and parts[-1] in {"jr", "sr", "ii", "iii", "iv", "v"}:
        parts.pop()
    key = "".join(parts)
    return PLAYER_ALIASES.get(key, key)


def game_subject(value: str, game: str | None) -> str:
    """Expand city-only Kalshi labels to the sportsbook's full team name."""
    key = normalize_player(value)
    for team in re.split(r"\s+@\s+|\s+vs\.?\s+", game or "", flags=re.I):
        team_key = normalize_player(team)
        if key and (key in team_key or team_key in key):
            return team.strip()
    return value.strip()


def implied_probability(decimal_odds: float | None) -> float | None:
    if decimal_odds is None or decimal_odds <= 1:
        return None
    return 1 / decimal_odds


def devig_probability(
    over_decimal: float | None,
    under_decimal: float | None,
    selected_side: str,
) -> float | None:
    """Use proportional two-way de-vigging, deliberately simple and auditable."""
    over = implied_probability(over_decimal)
    under = implied_probability(under_decimal)
    if over is None or under is None or selected_side not in {"over", "under"}:
        return None
    total = over + under
    return (over if selected_side == "over" else under) / total


def component_identity(leg: dict, market: dict) -> tuple[dict | None, str | None]:
    """Extract the sportsbook subject, market type, and line for a Kalshi leg."""
    event_ticker = leg.get("event_ticker") or market.get("event_ticker") or ""
    series = event_ticker.split("-", 1)[0]
    title = market.get("title") or market.get("yes_sub_title") or ""
    lowered = title.lower()
    prop_type = market.get("prop_type") or SERIES_PROP_TYPES.get(series)
    if not prop_type:
        labels = (
            ("receiving yards", "receiving_yards"),
            ("rushing yards", "rushing_yards"),
            ("passing yards", "passing_yards"),
            ("receptions", "receptions"),
            ("passing touchdowns", "passing_touchdowns"),
            ("touchdown passes", "passing_touchdowns"),
        )
        prop_type = next((value for label, value in labels if label in lowered), None)
    if not prop_type:
        return None, "unsupported_component_market"

    player = market.get("player")
    if not player:
        player = (market.get("yes_sub_title") or title).split(":", 1)[0].strip()
    if prop_type in {"moneyline", "spread", "team_total"}:
        player = re.split(
            r"\s+(?:wins?|over|under)\b", player, maxsplit=1, flags=re.I
        )[0]
        player = game_subject(player, leg.get("game"))
    elif prop_type == "game_total":
        player = "Full Game"
    if not normalize_player(player):
        return None, "missing_component_player"

    threshold = market.get("threshold")
    plus_match = re.search(r"(\d+(?:\.\d+)?)\s*\+", title)
    if plus_match:
        threshold = float(plus_match.group(1)) - 0.5
    elif threshold is None:
        strike = market.get("floor_strike")
        if isinstance(strike, dict):
            strike = strike.get("value")
        try:
            threshold = float(strike)
        except (TypeError, ValueError):
            threshold = None
    if threshold is None and prop_type in {"moneyline", "first_touchdown"}:
        threshold = 0.0 if prop_type == "moneyline" else 0.5
    if threshold is None:
        return None, "missing_component_threshold"

    side = leg.get("side", "yes").lower()
    if side not in {"yes", "no"}:
        return None, "unsupported_component_side"
    return {
        "market_ticker": leg.get("market_ticker"),
        "game": leg.get("game"),
        "player": player,
        "player_key": normalize_player(player),
        "prop_type": prop_type,
        "line": float(threshold),
        "kalshi_side": side,
        "sportsbook_side": "over" if side == "yes" else "under",
    }, None


def minimum_sell_price(
    conservative_probability: float,
    minimum_edge: float = MIN_SELLER_EDGE,
    fee_rate: float = FEE_RATE,
) -> float | None:
    """Return the lowest whole-cent YES sale price meeting the net edge target."""
    required_net = conservative_probability + minimum_edge
    for cents in range(1, 100):
        price = cents / 100
        if price - fee_per_contract(price, fee_rate) + 1e-12 >= required_net:
            return price
    return None


def price_external_combo(
    legs: list[dict],
    *,
    min_books_per_leg: int = MIN_BOOKS_PER_LEG,
    max_age_seconds: float = MAX_SPORTSBOOK_AGE_SECONDS,
    leg_uncertainty: float = LEG_UNCERTAINTY,
    stale_buffer_at_limit: float = STALE_BUFFER_AT_LIMIT,
    minimum_edge: float = MIN_SELLER_EDGE,
) -> dict:
    """Price independent cross-game legs from per-book de-vigged probabilities."""
    if not legs:
        return {"skip_reason": "no_combo_legs", "proposed_yes_sell_price": None}

    priced_legs = []
    skip_reasons = []
    for index, leg in enumerate(legs, start=1):
        quotes = [
            quote
            for quote in leg.get("book_quotes", [])
            if quote.get("devig_probability") is not None
        ]
        probabilities = [quote["devig_probability"] for quote in quotes]
        uncertainties = [quote.get("probability_uncertainty", 0.0) for quote in quotes]
        ages = [quote["age_seconds"] for quote in quotes]
        detail = {
            **{key: value for key, value in leg.items() if key != "book_quotes"},
            "book_quotes": quotes,
            "books_available": sorted({quote["sportsbook"] for quote in quotes}),
            "book_count": len({quote["sportsbook"] for quote in quotes}),
            "max_age_seconds": max(ages) if ages else None,
            "consensus_probability": statistics.median(probabilities)
            if probabilities
            else None,
            "fair_low": max(
                0.0,
                min(
                    probability - uncertainty
                    for probability, uncertainty in zip(probabilities, uncertainties)
                ) - leg_uncertainty,
            )
            if probabilities
            else None,
            "fair_high": min(
                1.0,
                max(
                    probability + uncertainty
                    for probability, uncertainty in zip(probabilities, uncertainties)
                ) + leg_uncertainty,
            )
            if probabilities
            else None,
        }
        priced_legs.append(detail)
        if not probabilities:
            skip_reasons.append(f"leg_{index}_no_two_sided_sportsbook_price")
        elif detail["book_count"] < min_books_per_leg:
            skip_reasons.append(f"leg_{index}_only_{detail['book_count']}_book")
        elif detail["max_age_seconds"] > max_age_seconds:
            skip_reasons.append(f"leg_{index}_sportsbook_price_stale")

    complete = all(leg["consensus_probability"] is not None for leg in priced_legs)
    fair_value = (
        math.prod(leg["consensus_probability"] for leg in priced_legs)
        if complete
        else None
    )
    fair_low = (
        math.prod(leg["fair_low"] for leg in priced_legs) if complete else None
    )
    fair_high = (
        math.prod(leg["fair_high"] for leg in priced_legs) if complete else None
    )
    maximum_age = max(
        (leg["max_age_seconds"] for leg in priced_legs if leg["max_age_seconds"] is not None),
        default=None,
    )
    stale_buffer = (
        min(1.0, maximum_age / max_age_seconds) * stale_buffer_at_limit
        if maximum_age is not None
        else None
    )
    conservative_probability = (
        min(1.0, fair_high + stale_buffer)
        if fair_high is not None and stale_buffer is not None
        else None
    )
    quote = None
    if not skip_reasons and conservative_probability is not None:
        quote = minimum_sell_price(conservative_probability, minimum_edge)
        if quote is None:
            skip_reasons.append("required_quote_above_99c")

    return {
        "fair_value": fair_value,
        "fair_value_low": fair_low,
        "fair_value_high": fair_high,
        "fair_value_method": "median_sportsbook_probability_then_independent_leg_product",
        "stale_probability_buffer": stale_buffer,
        "conservative_probability": conservative_probability,
        "minimum_desired_edge": minimum_edge,
        "proposed_yes_sell_price": quote,
        "proposed_fee": fee_per_contract(quote) if quote is not None else None,
        "proposed_edge_vs_consensus": (
            quote - fee_per_contract(quote) - fair_value
            if quote is not None and fair_value is not None
            else None
        ),
        "books_available": sorted(
            {book for leg in priced_legs for book in leg["books_available"]}
        ),
        "minimum_books_on_any_leg": min(
            (leg["book_count"] for leg in priced_legs), default=0
        ),
        "maximum_sportsbook_age_seconds": maximum_age,
        "skip_reason": ";".join(skip_reasons) if skip_reasons else None,
        "legs": priced_legs,
    }
