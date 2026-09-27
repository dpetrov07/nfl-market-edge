"""Build a compact, timestamp-safe RFQ dataset from archived slate captures."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nfl_market_edge.shadow import (
    INTERPOLATION_UNCERTAINTY,
    LEG_UNCERTAINTY,
    MAX_SPORTSBOOK_AGE_SECONDS,
    MIN_BOOKS_PER_LEG,
    ONE_WAY_UNCERTAINTY,
    SUPPORTED_BOOKS,
    component_identity,
    devig_probability,
    implied_probability,
    normalize_player,
)
from scripts.collect_live_combo_slate import event_key, read_manifest


HORIZONS = (10, 30, 60)
UTC_TYPE = pa.timestamp("us", tz="UTC")


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


def midpoint(book: dict | None) -> float | None:
    if not book or book.get("yes_bid") is None or book.get("yes_ask") is None:
        return None
    return (book["yes_bid"] + book["yes_ask"]) / 2


class BatchWriter:
    def __init__(self, path: Path, schema: pa.Schema, batch_size: int):
        self.path = path
        self.temp = path.with_suffix(path.suffix + ".tmp")
        self.schema = schema
        self.batch_size = batch_size
        self.rows: list[dict] = []
        self.writer: pq.ParquetWriter | None = None
        self.count = 0

    def write(self, row: dict) -> None:
        self.rows.append(row)
        if len(self.rows) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        table = pa.Table.from_pylist(self.rows, schema=self.schema)
        if self.writer is None:
            self.writer = pq.ParquetWriter(
                self.temp,
                self.schema,
                compression="zstd",
                use_dictionary=True,
                write_statistics=True,
            )
        self.writer.write_table(table)
        self.count += len(self.rows)
        self.rows.clear()

    def close(self) -> None:
        self.flush()
        if self.writer is None:
            pq.write_table(
                pa.Table.from_pylist([], schema=self.schema),
                self.temp,
                compression="zstd",
            )
        else:
            self.writer.close()
        os.replace(self.temp, self.path)


BASE_SCHEMA = pa.schema(
    [
        ("slate_id", pa.string()),
        ("opportunity_id", pa.string()),
        ("opportunity_type", pa.string()),
        ("received_at", UTC_TYPE),
        ("exchange_timestamp", UTC_TYPE),
        ("rfq_id", pa.string()),
        ("combo_market_ticker", pa.string()),
        ("rfq_size", pa.float64()),
        ("rfq_target_cost", pa.float64()),
        ("leg_count", pa.int8()),
        ("distinct_leg_games", pa.int8()),
        ("scope", pa.string()),
        ("games", pa.string()),
        ("legs_json", pa.large_string()),
        ("market_yes_bid", pa.float64()),
        ("market_yes_ask", pa.float64()),
        ("market_yes_bid_size", pa.float64()),
        ("market_yes_ask_size", pa.float64()),
        ("market_book_observed_at", UTC_TYPE),
    ]
)

BOOK_SCHEMA = pa.schema(
    [
        ("received_at", UTC_TYPE),
        ("exchange_timestamp", UTC_TYPE),
        ("market_ticker", pa.string()),
        ("market_role", pa.string()),
        ("yes_bid", pa.float64()),
        ("yes_ask", pa.float64()),
        ("yes_bid_size", pa.float64()),
        ("yes_ask_size", pa.float64()),
    ]
)

COMPONENT_SCHEMA = pa.schema(
    [
        ("received_at", UTC_TYPE),
        ("market_ticker", pa.string()),
        ("metadata_json", pa.large_string()),
    ]
)

SETTLEMENT_SCHEMA = pa.schema(
    [
        ("market_ticker", pa.string()),
        ("settled_at", UTC_TYPE),
        ("settlement_value", pa.float64()),
        ("result", pa.string()),
    ]
)

OPPORTUNITY_SCHEMA = pa.schema(
    [
        ("slate_id", pa.string()),
        ("opportunity_id", pa.string()),
        ("opportunity_type", pa.string()),
        ("received_at", UTC_TYPE),
        ("exchange_timestamp", UTC_TYPE),
        ("rfq_id", pa.string()),
        ("combo_market_ticker", pa.string()),
        ("rfq_size", pa.float64()),
        ("rfq_target_cost", pa.float64()),
        ("leg_count", pa.int8()),
        ("distinct_leg_games", pa.int8()),
        ("scope", pa.string()),
        ("games", pa.string()),
        ("pricing_snapshot_id", pa.int64()),
        ("identity_complete", pa.bool_()),
        ("external_fair_value", pa.float64()),
        ("external_fair_value_low", pa.float64()),
        ("external_fair_value_high", pa.float64()),
        ("fair_value_method", pa.string()),
        ("books_available", pa.string()),
        ("minimum_books_on_any_leg", pa.int8()),
        ("maximum_sportsbook_age_seconds", pa.float64()),
        ("sportsbook_coverage_pass", pa.bool_()),
        ("coverage_reason", pa.string()),
        ("market_yes_bid", pa.float64()),
        ("market_yes_ask", pa.float64()),
        ("market_yes_bid_size", pa.float64()),
        ("market_yes_ask_size", pa.float64()),
        ("market_midpoint", pa.float64()),
        ("market_book_observed_at", UTC_TYPE),
        ("market_quote_age_seconds", pa.float64()),
        ("market_midpoint_10s", pa.float64()),
        ("midpoint_change_10s", pa.float64()),
        ("seller_bid_markout_10s", pa.float64()),
        ("market_midpoint_30s", pa.float64()),
        ("midpoint_change_30s", pa.float64()),
        ("seller_bid_markout_30s", pa.float64()),
        ("market_midpoint_60s", pa.float64()),
        ("midpoint_change_60s", pa.float64()),
        ("seller_bid_markout_60s", pa.float64()),
        ("settled_at", UTC_TYPE),
        ("settlement_value", pa.float64()),
    ]
)

PRICING_SCHEMA = pa.schema(
    [
        ("pricing_snapshot_id", pa.int64()),
        ("first_used_at", UTC_TYPE),
        ("combo_market_ticker", pa.string()),
        ("leg_count", pa.int8()),
        ("identity_complete", pa.bool_()),
        ("external_fair_value", pa.float64()),
        ("external_fair_value_low", pa.float64()),
        ("external_fair_value_high", pa.float64()),
        ("fair_value_method", pa.string()),
        ("books_available", pa.string()),
        ("minimum_books_on_any_leg", pa.int8()),
    ]
)


def leg_schema() -> pa.Schema:
    fields = [
        ("pricing_snapshot_id", pa.int64()),
        ("leg_index", pa.int8()),
        ("market_ticker", pa.string()),
        ("event_ticker", pa.string()),
        ("game", pa.string()),
        ("kalshi_side", pa.string()),
        ("sportsbook_side", pa.string()),
        ("player", pa.string()),
        ("player_key", pa.string()),
        ("prop_type", pa.string()),
        ("line", pa.float64()),
        ("identity_error", pa.string()),
        ("consensus_probability", pa.float64()),
        ("fair_low", pa.float64()),
        ("fair_high", pa.float64()),
        ("book_count", pa.int8()),
        ("kalshi_yes_bid", pa.float64()),
        ("kalshi_yes_ask", pa.float64()),
        ("kalshi_book_observed_at", UTC_TYPE),
    ]
    for book in SUPPORTED_BOOKS:
        fields.extend(
            [
                (f"{book}_probability", pa.float64()),
                (f"{book}_price_method", pa.string()),
                (f"{book}_observed_at", UTC_TYPE),
                (f"{book}_line", pa.float64()),
                (f"{book}_source_lines", pa.string()),
                (f"{book}_over_decimal_odds", pa.float64()),
                (f"{book}_under_decimal_odds", pa.float64()),
            ]
        )
    return pa.schema(fields)


def kalshi_paths(root: Path) -> list[Path]:
    return sorted((root / "kalshi" / "kalshi").glob("*.jsonl.gz"))


def sportsbook_paths(root: Path, book: str) -> list[Path]:
    return sorted((root / book / "sportsbooks" / book).glob("*.jsonl.gz"))


def compact_book(record: dict, at: datetime) -> dict:
    return {
        "received_at": at,
        "yes_bid": number(record.get("yes_bid_dollars")),
        "yes_ask": number(record.get("yes_ask_dollars")),
        "yes_bid_size": number(record.get("yes_bid_size")),
        "yes_ask_size": number(record.get("yes_ask_size")),
    }


def settlement_value(record: dict) -> float | None:
    value = number(record.get("settlement_value_dollars"))
    if value is None and record.get("result") in {"yes", "no"}:
        value = float(record["result"] == "yes")
    return value


def extract_kalshi(
    root: Path,
    output: Path,
    manifest: dict,
    batch_size: int,
    start_at: datetime | None,
    max_opportunities: int | None,
) -> dict:
    base = BatchWriter(output / ".opportunities_base.parquet", BASE_SCHEMA, batch_size)
    books = BatchWriter(output / "kalshi_books.parquet", BOOK_SCHEMA, batch_size)
    components_out = BatchWriter(
        output / "component_versions.parquet", COMPONENT_SCHEMA, batch_size
    )
    components: dict[str, dict] = {}
    combos: dict[str, dict] = {}
    latest_books: dict[str, dict] = {}
    settlements: dict[str, dict] = {}
    counters = Counter()
    recent_rfq_ids: dict[str, None] = {}
    capture_start = capture_end = None
    started = time.monotonic()
    stop = False

    for path in kalshi_paths(root):
        if stop:
            break
        with gzip.open(path, "rb") as handle:
            for line in handle:
                counters["raw_records"] += 1
                if counters["raw_records"] % 1_000_000 == 0:
                    elapsed = max(0.001, time.monotonic() - started)
                    print(
                        f"kalshi scan {counters['raw_records']:,} records; "
                        f"{counters['opportunities']:,} opportunities; "
                        f"{counters['raw_records'] / elapsed:,.0f} rows/s",
                        flush=True,
                    )
                if b'"record_type":"communication"' in line:
                    if not (
                        b'"communication_type":"rfq_created"' in line
                        or b'"communication_type":"rfq_snapshot"' in line
                    ):
                        continue
                elif not any(
                    marker in line
                    for marker in (
                        b'"record_type":"combo_discovery"',
                        b'"record_type":"component_discovery"',
                        b'"record_type":"top_of_book"',
                        b'"record_type":"market_status"',
                    )
                ):
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    counters["invalid_json"] += 1
                    continue
                at = parse_time(record.get("received_at"))
                if at is None:
                    counters["missing_timestamp"] += 1
                    continue
                capture_start = at if capture_start is None else min(capture_start, at)
                capture_end = at if capture_end is None else max(capture_end, at)
                kind = record.get("record_type")

                if kind == "component_discovery":
                    for market in record.get("markets") or []:
                        ticker = market.get("ticker") or market.get("market_ticker")
                        if not ticker:
                            continue
                        components[ticker] = market
                        components_out.write(
                            {
                                "received_at": at,
                                "market_ticker": ticker,
                                "metadata_json": json.dumps(
                                    market, sort_keys=True, separators=(",", ":")
                                ),
                            }
                        )
                        counters["component_versions"] += 1
                    continue

                if kind == "combo_discovery":
                    combo = record.get("combo") or {}
                    ticker = combo.get("ticker") or combo.get("market_ticker")
                    if ticker:
                        combos[ticker] = combo
                        value = settlement_value(combo)
                        if value is not None:
                            settlements[ticker] = {
                                "market_ticker": ticker,
                                "settled_at": at,
                                "settlement_value": value,
                                "result": combo.get("result"),
                            }
                    continue

                if kind == "top_of_book":
                    ticker = record.get("market_ticker")
                    if not ticker:
                        continue
                    book = compact_book(record, at)
                    latest_books[ticker] = book
                    books.write(
                        {
                            **book,
                            "exchange_timestamp": parse_time(
                                record.get("exchange_timestamp")
                            ),
                            "market_ticker": ticker,
                            "market_role": record.get("market_role"),
                        }
                    )
                    counters["book_updates"] += 1
                    continue

                if kind == "market_status":
                    ticker = record.get("market_ticker")
                    value = settlement_value(record)
                    if ticker and value is not None:
                        settlements[ticker] = {
                            "market_ticker": ticker,
                            "settled_at": at,
                            "settlement_value": value,
                            "result": record.get("result"),
                        }
                    continue

                if kind != "communication":
                    continue
                if start_at is not None and at < start_at:
                    continue
                ticker = record.get("market_ticker")
                payload = record.get("payload") or {}
                raw_legs = payload.get("mve_selected_legs") or (
                    combos.get(ticker, {}).get("mve_selected_legs") if ticker else None
                )
                if not ticker or not raw_legs:
                    counters["missing_combo_definition"] += 1
                    continue
                legs = []
                games = []
                for raw_leg in raw_legs:
                    leg = dict(raw_leg)
                    game = manifest["event_games"].get(event_key(leg.get("event_ticker")))
                    leg["game"] = game
                    if game:
                        games.append(game)
                    metadata = components.get(leg.get("market_ticker"), {})
                    parsed, error = component_identity(leg, metadata)
                    if parsed:
                        leg.update(parsed)
                    leg["identity_error"] = error
                    component_book = latest_books.get(leg.get("market_ticker"))
                    leg["kalshi_book"] = (
                        {
                            "received_at": component_book["received_at"].isoformat(),
                            "yes_bid": component_book.get("yes_bid"),
                            "yes_ask": component_book.get("yes_ask"),
                        }
                        if component_book
                        else None
                    )
                    legs.append(leg)
                distinct_games = len(set(games))
                scope = "same_game" if distinct_games == 1 else "cross_game"
                combo_book = latest_books.get(ticker)
                rfq_id = record.get("rfq_id")
                opportunity_id = str(
                    rfq_id
                    or f"{ticker}|{record.get('exchange_timestamp')}|{record.get('received_at')}"
                )
                if record.get("communication_type") == "rfq_snapshot" and opportunity_id in recent_rfq_ids:
                    counters["duplicate_snapshots_skipped"] += 1
                    continue
                recent_rfq_ids[opportunity_id] = None
                if len(recent_rfq_ids) > 500_000:
                    recent_rfq_ids.pop(next(iter(recent_rfq_ids)))
                base.write(
                    {
                        "slate_id": manifest["slate_id"],
                        "opportunity_id": opportunity_id,
                        "opportunity_type": record.get("communication_type"),
                        "received_at": at,
                        "exchange_timestamp": parse_time(
                            record.get("exchange_timestamp")
                        ),
                        "rfq_id": str(rfq_id) if rfq_id is not None else None,
                        "combo_market_ticker": ticker,
                        "rfq_size": number(record.get("contracts")),
                        "rfq_target_cost": number(record.get("target_cost_dollars")),
                        "leg_count": len(legs),
                        "distinct_leg_games": distinct_games,
                        "scope": scope,
                        "games": ", ".join(sorted(set(games))),
                        "legs_json": json.dumps(
                            legs, sort_keys=True, separators=(",", ":")
                        ),
                        "market_yes_bid": combo_book.get("yes_bid")
                        if combo_book
                        else None,
                        "market_yes_ask": combo_book.get("yes_ask")
                        if combo_book
                        else None,
                        "market_yes_bid_size": combo_book.get("yes_bid_size")
                        if combo_book
                        else None,
                        "market_yes_ask_size": combo_book.get("yes_ask_size")
                        if combo_book
                        else None,
                        "market_book_observed_at": combo_book.get("received_at")
                        if combo_book
                        else None,
                    }
                )
                counters["opportunities"] += 1
                if (
                    max_opportunities is not None
                    and counters["opportunities"] >= max_opportunities
                ):
                    stop = True
                    break

    base.close()
    books.close()
    components_out.close()
    settlement_writer = BatchWriter(
        output / "settlements.parquet", SETTLEMENT_SCHEMA, batch_size
    )
    for row in sorted(
        settlements.values(), key=lambda value: (value["market_ticker"], value["settled_at"])
    ):
        settlement_writer.write(row)
    settlement_writer.close()
    result = {
        "capture_start": capture_start.isoformat() if capture_start else None,
        "capture_end": capture_end.isoformat() if capture_end else None,
        **dict(counters),
        "settlements": len(settlements),
    }
    (output / ".extract_summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


class SportsbookStream:
    def __init__(self, paths: list[Path]):
        self.paths = paths

    def __iter__(self):
        for path in self.paths:
            with gzip.open(path, "rb") as handle:
                for line in handle:
                    if b'"record_type":"selection_state"' not in line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue


class LiveSportsbooks:
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

    def __init__(self):
        self.states: dict[tuple, dict] = {}
        self.candidates = defaultdict(set)
        self.lines = defaultdict(set)
        self.versions = Counter()
        self.quote_cache: dict[tuple, tuple[int, list[dict]]] = {}

    def update(self, row: dict) -> None:
        at = parse_time(row.get("received_at"))
        line = number(row.get("line"))
        side = row.get("side")
        book = row.get("sportsbook")
        market_id = row.get("market_id")
        player_key = normalize_player(row.get("player"))
        prop_type = row.get("prop_type")
        if (
            at is None
            or line is None
            or side not in {"over", "under"}
            or not book
            or not market_id
            or not player_key
            or not prop_type
        ):
            return
        line = round(line, 3)
        base = (player_key, prop_type, line)
        key = (*base, book, str(market_id), side)
        self.states[key] = {
            "received_at": at,
            "game": row.get("game"),
            "decimal_odds": number(row.get("decimal_odds")),
            "fair_probability": number(row.get("fair_probability")),
            "state": row.get("state"),
        }
        self.candidates[(*base, book)].add(str(market_id))
        self.lines[(player_key, prop_type, book)].add(line)
        self.versions[(player_key, prop_type)] += 1

    def _quote_at(self, leg: dict, book: str, line: float) -> dict | None:
        base = (leg["player_key"], leg["prop_type"], round(line, 3))
        best = None
        for market_id in self.candidates.get((*base, book), ()):
            over = self.states.get((*base, book, market_id, "over"))
            under = self.states.get((*base, book, market_id, "under"))
            over = over if over and over.get("state") == "open" else None
            under = under if under and under.get("state") == "open" else None
            selected = over if leg["sportsbook_side"] == "over" else under
            opposite = under if leg["sportsbook_side"] == "over" else over
            method = "exact_two_way"
            uncertainty = 0.0
            probability = devig_probability(
                over.get("decimal_odds") if over else None,
                under.get("decimal_odds") if under else None,
                leg["sportsbook_side"],
            )
            if probability is None and selected:
                probability = selected.get("fair_probability")
                method = "exact_multiway_devig"
                uncertainty = LEG_UNCERTAINTY
            if probability is None and opposite and opposite.get("fair_probability") is not None:
                probability = 1 - opposite["fair_probability"]
                method = "exact_multiway_devig"
                uncertainty = LEG_UNCERTAINTY
            if probability is None and selected:
                probability = implied_probability(selected.get("decimal_odds"))
                method = "exact_one_way"
                uncertainty = ONE_WAY_UNCERTAINTY
            if probability is None and opposite:
                raw = implied_probability(opposite.get("decimal_odds"))
                probability = 1 - raw if raw is not None else None
                method = "exact_one_way_complement"
                uncertainty = ONE_WAY_UNCERTAINTY
            if probability is None:
                continue
            used = [value for value in (over, under) if value]
            observed_at = max(value["received_at"] for value in used)
            candidate = {
                "sportsbook": book,
                "market_id": market_id,
                "line": line,
                "over_decimal_odds": over.get("decimal_odds") if over else None,
                "under_decimal_odds": under.get("decimal_odds") if under else None,
                "devig_probability": probability,
                "price_method": method,
                "probability_uncertainty": uncertainty,
                "observed_at": observed_at,
                "source_lines": None,
            }
            if best is None or observed_at > best["observed_at"]:
                best = candidate
        return best

    def quotes(self, leg: dict) -> list[dict]:
        identity = (
            leg["player_key"],
            leg["prop_type"],
            round(leg["line"], 3),
            leg["sportsbook_side"],
        )
        version = self.versions[(leg["player_key"], leg["prop_type"])]
        cached = self.quote_cache.get(identity)
        if cached and cached[0] == version:
            return cached[1]
        target = identity[2]
        by_book = {}
        for book in SUPPORTED_BOOKS:
            exact = self._quote_at(leg, book, target)
            if exact:
                by_book[book] = exact
                continue
            available = sorted(self.lines.get((identity[0], identity[1], book), ()))
            lower = max((value for value in available if value < target), default=None)
            upper = min((value for value in available if value > target), default=None)
            maximum_span = self.MAX_INTERPOLATION_SPAN.get(identity[1])
            if (
                lower is None
                or upper is None
                or maximum_span is None
                or upper - lower > maximum_span
            ):
                continue
            low = self._quote_at(leg, book, lower)
            high = self._quote_at(leg, book, upper)
            if not low or not high:
                continue
            weight = (target - lower) / (upper - lower)
            by_book[book] = {
                "sportsbook": book,
                "market_id": None,
                "line": target,
                "source_lines": (lower, upper),
                "over_decimal_odds": None,
                "under_decimal_odds": None,
                "devig_probability": low["devig_probability"]
                + weight * (high["devig_probability"] - low["devig_probability"]),
                "price_method": "interpolated_alt_lines",
                "probability_uncertainty": max(
                    low["probability_uncertainty"], high["probability_uncertainty"]
                )
                + INTERPOLATION_UNCERTAINTY,
                "observed_at": max(low["observed_at"], high["observed_at"]),
            }
        result = [by_book[book] for book in SUPPORTED_BOOKS if book in by_book]
        self.quote_cache[identity] = (version, result)
        return result


def load_book_histories(path: Path) -> dict[str, dict]:
    histories = defaultdict(lambda: {"times": [], "books": []})
    parquet = pq.ParquetFile(path)
    for batch in parquet.iter_batches(batch_size=100_000):
        for row in batch.to_pylist():
            ticker = row["market_ticker"]
            histories[ticker]["times"].append(row["received_at"].timestamp())
            histories[ticker]["books"].append(
                {
                    "yes_bid": row["yes_bid"],
                    "yes_ask": row["yes_ask"],
                    "received_at": row["received_at"],
                }
            )
    return dict(histories)


def latest_book(histories: dict, ticker: str, timestamp: float) -> dict | None:
    history = histories.get(ticker)
    if not history:
        return None
    index = bisect_right(history["times"], timestamp) - 1
    return history["books"][index] if index >= 0 else None


def quote_signature(quote: dict) -> tuple:
    return (
        quote["sportsbook"],
        quote["observed_at"].isoformat(),
        round(quote["devig_probability"], 12),
        quote["price_method"],
        quote.get("line"),
        quote.get("source_lines"),
        quote.get("over_decimal_odds"),
        quote.get("under_decimal_odds"),
    )


def price_legs(legs: list[dict], sportsbooks: LiveSportsbooks) -> tuple[dict, tuple]:
    details = []
    signature = []
    identity_complete = True
    for leg in legs:
        error = leg.get("identity_error")
        if error or not all(
            leg.get(key) is not None
            for key in ("player_key", "prop_type", "line", "sportsbook_side")
        ):
            identity_complete = False
            details.append({**leg, "book_quotes": [], "identity_error": error or "missing_identity"})
            signature.append((leg.get("market_ticker"), error or "missing_identity"))
            continue
        quotes = sportsbooks.quotes(leg)
        probabilities = [quote["devig_probability"] for quote in quotes]
        uncertainties = [quote["probability_uncertainty"] for quote in quotes]
        detail = {
            **leg,
            "book_quotes": quotes,
            "book_count": len({quote["sportsbook"] for quote in quotes}),
            "consensus_probability": statistics.median(probabilities)
            if probabilities
            else None,
            "fair_low": max(
                0.0,
                min(
                    probability - uncertainty
                    for probability, uncertainty in zip(probabilities, uncertainties)
                )
                - LEG_UNCERTAINTY,
            )
            if probabilities
            else None,
            "fair_high": min(
                1.0,
                max(
                    probability + uncertainty
                    for probability, uncertainty in zip(probabilities, uncertainties)
                )
                + LEG_UNCERTAINTY,
            )
            if probabilities
            else None,
            "identity_error": None,
        }
        details.append(detail)
        signature.append(
            (
                leg.get("market_ticker"),
                leg.get("kalshi_side"),
                leg.get("player_key"),
                leg.get("prop_type"),
                leg.get("line"),
                (
                    (leg.get("kalshi_book") or {}).get("received_at"),
                    (leg.get("kalshi_book") or {}).get("yes_bid"),
                    (leg.get("kalshi_book") or {}).get("yes_ask"),
                ),
                tuple(quote_signature(quote) for quote in quotes),
            )
        )
    complete = identity_complete and all(
        detail.get("consensus_probability") is not None for detail in details
    )
    result = {
        "identity_complete": identity_complete,
        "details": details,
        "external_fair_value": math.prod(
            detail["consensus_probability"] for detail in details
        )
        if complete
        else None,
        "external_fair_value_low": math.prod(detail["fair_low"] for detail in details)
        if complete
        else None,
        "external_fair_value_high": math.prod(detail["fair_high"] for detail in details)
        if complete
        else None,
        "fair_value_method": "median_sportsbook_probability_then_independent_leg_product"
        if complete
        else None,
        "books_available": sorted(
            {
                quote["sportsbook"]
                for detail in details
                for quote in detail.get("book_quotes", [])
            }
        ),
        "minimum_books_on_any_leg": min(
            (detail.get("book_count", 0) for detail in details), default=0
        ),
    }
    return result, tuple(signature)


def pricing_leg_row(snapshot_id: int, index: int, detail: dict) -> dict:
    kalshi = detail.get("kalshi_book") or {}
    row = {
        "pricing_snapshot_id": snapshot_id,
        "leg_index": index,
        "market_ticker": detail.get("market_ticker"),
        "event_ticker": detail.get("event_ticker"),
        "game": detail.get("game"),
        "kalshi_side": detail.get("kalshi_side") or detail.get("side"),
        "sportsbook_side": detail.get("sportsbook_side"),
        "player": detail.get("player"),
        "player_key": detail.get("player_key"),
        "prop_type": detail.get("prop_type"),
        "line": number(detail.get("line")),
        "identity_error": detail.get("identity_error"),
        "consensus_probability": detail.get("consensus_probability"),
        "fair_low": detail.get("fair_low"),
        "fair_high": detail.get("fair_high"),
        "book_count": detail.get("book_count", 0),
        "kalshi_yes_bid": number(kalshi.get("yes_bid")),
        "kalshi_yes_ask": number(kalshi.get("yes_ask")),
        "kalshi_book_observed_at": parse_time(kalshi.get("received_at")),
    }
    quotes = {quote["sportsbook"]: quote for quote in detail.get("book_quotes", [])}
    for book in SUPPORTED_BOOKS:
        quote = quotes.get(book, {})
        row.update(
            {
                f"{book}_probability": quote.get("devig_probability"),
                f"{book}_price_method": quote.get("price_method"),
                f"{book}_observed_at": quote.get("observed_at"),
                f"{book}_line": quote.get("line"),
                f"{book}_source_lines": ",".join(
                    str(value) for value in quote.get("source_lines", ())
                )
                if quote.get("source_lines")
                else None,
                f"{book}_over_decimal_odds": quote.get("over_decimal_odds"),
                f"{book}_under_decimal_odds": quote.get("under_decimal_odds"),
            }
        )
    return row


def build_final(
    root: Path,
    output: Path,
    batch_size: int,
    extract_summary: dict,
) -> dict:
    books = load_book_histories(output / "kalshi_books.parquet")
    settlement_rows = pq.read_table(output / "settlements.parquet").to_pylist()
    settlements = {row["market_ticker"]: row for row in settlement_rows}
    capture_end = parse_time(extract_summary.get("capture_end"))
    capture_end_ts = capture_end.timestamp() if capture_end else None

    streams = {
        book: iter(SportsbookStream(sportsbook_paths(root, book)))
        for book in SUPPORTED_BOOKS
    }
    next_rows = {book: next(stream, None) for book, stream in streams.items()}
    live = LiveSportsbooks()
    opportunities = BatchWriter(
        output / "opportunities.parquet", OPPORTUNITY_SCHEMA, batch_size
    )
    pricing = BatchWriter(
        output / "pricing_snapshots.parquet", PRICING_SCHEMA, batch_size
    )
    legs_out = BatchWriter(output / "legs.parquet", leg_schema(), batch_size)
    last_pricing: dict[str, tuple[tuple, int, dict]] = {}
    snapshot_id = 0
    counters = Counter()
    skip_reasons = Counter()
    started = time.monotonic()

    base_file = pq.ParquetFile(output / ".opportunities_base.parquet")
    for batch in base_file.iter_batches(batch_size=batch_size):
        for base in batch.to_pylist():
            at = base["received_at"]
            at_iso = at.isoformat()
            at_ts = at.timestamp()
            for book in SUPPORTED_BOOKS:
                while next_rows[book] is not None and next_rows[book].get(
                    "received_at", ""
                ) <= at_iso:
                    live.update(next_rows[book])
                    counters["sportsbook_observations"] += 1
                    next_rows[book] = next(streams[book], None)

            raw_legs = json.loads(base["legs_json"])
            priced, signature = price_legs(raw_legs, live)
            ticker = base["combo_market_ticker"]
            cached = last_pricing.get(ticker)
            if cached and cached[0] == signature:
                current_snapshot_id, snapshot = cached[1], cached[2]
            else:
                snapshot_id += 1
                current_snapshot_id = snapshot_id
                snapshot = priced
                last_pricing[ticker] = (signature, current_snapshot_id, snapshot)
                pricing.write(
                    {
                        "pricing_snapshot_id": current_snapshot_id,
                        "first_used_at": at,
                        "combo_market_ticker": ticker,
                        "leg_count": base["leg_count"],
                        "identity_complete": priced["identity_complete"],
                        "external_fair_value": priced["external_fair_value"],
                        "external_fair_value_low": priced["external_fair_value_low"],
                        "external_fair_value_high": priced["external_fair_value_high"],
                        "fair_value_method": priced["fair_value_method"],
                        "books_available": ",".join(priced["books_available"]),
                        "minimum_books_on_any_leg": priced[
                            "minimum_books_on_any_leg"
                        ],
                    }
                )
                for index, detail in enumerate(priced["details"], start=1):
                    legs_out.write(pricing_leg_row(current_snapshot_id, index, detail))

            quote_times = [
                quote["observed_at"]
                for detail in snapshot["details"]
                for quote in detail.get("book_quotes", [])
            ]
            max_age = max((at - value).total_seconds() for value in quote_times) if quote_times else None
            identity_complete = snapshot["identity_complete"]
            minimum_books = snapshot["minimum_books_on_any_leg"]
            reasons = []
            if not identity_complete:
                reasons.append("component_identity_unavailable_at_rfq")
            if snapshot["external_fair_value"] is None:
                reasons.append("missing_leg_consensus")
            if minimum_books < MIN_BOOKS_PER_LEG:
                reasons.append(f"minimum_books_{minimum_books}")
            if max_age is None:
                reasons.append("no_sportsbook_observation")
            elif max_age > MAX_SPORTSBOOK_AGE_SECONDS:
                reasons.append("sportsbook_observation_stale")
            coverage_pass = not reasons
            reason = ";".join(reasons) if reasons else None
            if reason:
                skip_reasons[reason] += 1

            decision_book = {
                "yes_bid": base["market_yes_bid"],
                "yes_ask": base["market_yes_ask"],
            }
            decision_mid = midpoint(decision_book)
            future_values = {}
            for horizon in HORIZONS:
                target = at_ts + horizon
                future = (
                    latest_book(books, ticker, target)
                    if capture_end_ts is not None and target <= capture_end_ts
                    else None
                )
                future_mid = midpoint(future)
                future_values[f"market_midpoint_{horizon}s"] = future_mid
                future_values[f"midpoint_change_{horizon}s"] = (
                    future_mid - decision_mid
                    if future_mid is not None and decision_mid is not None
                    else None
                )
                future_values[f"seller_bid_markout_{horizon}s"] = (
                    base["market_yes_bid"] - future_mid
                    if base["market_yes_bid"] is not None and future_mid is not None
                    else None
                )
            settlement = settlements.get(ticker, {})
            market_book_at = base["market_book_observed_at"]
            market_age = (
                (at - market_book_at).total_seconds() if market_book_at else None
            )
            if market_age is not None and market_age < -1e-9:
                counters["timestamp_violations"] += 1
            if any(value > at for value in quote_times):
                counters["timestamp_violations"] += 1
            fair = snapshot["external_fair_value"]
            if fair is not None and not 0 <= fair <= 1:
                counters["probability_violations"] += 1

            opportunities.write(
                {
                    **{key: base.get(key) for key in (
                        "slate_id", "opportunity_id", "opportunity_type",
                        "received_at", "exchange_timestamp", "rfq_id",
                        "combo_market_ticker", "rfq_size", "rfq_target_cost",
                        "leg_count", "distinct_leg_games", "scope", "games",
                        "market_yes_bid", "market_yes_ask", "market_yes_bid_size",
                        "market_yes_ask_size", "market_book_observed_at",
                    )},
                    "pricing_snapshot_id": current_snapshot_id,
                    "identity_complete": identity_complete,
                    "external_fair_value": fair,
                    "external_fair_value_low": snapshot[
                        "external_fair_value_low"
                    ],
                    "external_fair_value_high": snapshot[
                        "external_fair_value_high"
                    ],
                    "fair_value_method": snapshot["fair_value_method"],
                    "books_available": ",".join(snapshot["books_available"]),
                    "minimum_books_on_any_leg": minimum_books,
                    "maximum_sportsbook_age_seconds": max_age,
                    "sportsbook_coverage_pass": coverage_pass,
                    "coverage_reason": reason,
                    "market_midpoint": decision_mid,
                    "market_quote_age_seconds": market_age,
                    **future_values,
                    "settled_at": settlement.get("settled_at"),
                    "settlement_value": settlement.get("settlement_value"),
                }
            )
            counters["opportunities"] += 1
            counters["identity_complete"] += int(identity_complete)
            counters["fair_values"] += int(fair is not None)
            counters["sportsbook_coverage_pass"] += int(coverage_pass)
            counters["market_book"] += int(decision_mid is not None)
            counters["markout_10s"] += int(future_values["market_midpoint_10s"] is not None)
            counters["markout_30s"] += int(future_values["market_midpoint_30s"] is not None)
            counters["markout_60s"] += int(future_values["market_midpoint_60s"] is not None)
            counters["settled"] += int(settlement.get("settlement_value") is not None)
            if counters["opportunities"] % 100_000 == 0:
                elapsed = max(0.001, time.monotonic() - started)
                print(
                    f"pricing {counters['opportunities']:,} opportunities; "
                    f"{snapshot_id:,} pricing snapshots; "
                    f"{counters['opportunities'] / elapsed:,.0f} rfqs/s",
                    flush=True,
                )

    opportunities.close()
    pricing.close()
    legs_out.close()
    counters["pricing_snapshots"] = snapshot_id
    counters["leg_rows"] = legs_out.count
    result = {**dict(counters), "coverage_reasons": dict(skip_reasons.most_common())}
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=50_000)
    parser.add_argument("--start-at", type=str)
    parser.add_argument("--max-opportunities", type=int)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = read_manifest(args.manifest)
    output = args.output_dir
    if output.exists() and any(output.iterdir()) and not args.force:
        raise SystemExit(f"output directory is not empty: {output}; pass --force")
    output.mkdir(parents=True, exist_ok=True)
    start_at = parse_time(args.start_at)
    extract = extract_kalshi(
        args.input_root,
        output,
        manifest,
        args.batch_size,
        start_at,
        args.max_opportunities,
    )
    final = build_final(args.input_root, output, args.batch_size, extract)
    base = output / ".opportunities_base.parquet"
    if base.exists():
        base.unlink()
    summary = {
        "slate_id": manifest["slate_id"],
        "input_root": str(args.input_root),
        "configuration": {
            "minimum_books_per_leg": MIN_BOOKS_PER_LEG,
            "maximum_sportsbook_age_seconds": MAX_SPORTSBOOK_AGE_SECONDS,
            "markout_horizons_seconds": list(HORIZONS),
            "start_at": start_at.isoformat() if start_at else None,
            "max_opportunities": args.max_opportunities,
        },
        "extract": extract,
        "canonical": final,
        "files": {
            path.name: {"bytes": path.stat().st_size}
            for path in sorted(output.glob("*.parquet"))
        },
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    marker = output / ".extract_summary.json"
    if marker.exists():
        marker.unlink()
    print(json.dumps(summary["canonical"], sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
