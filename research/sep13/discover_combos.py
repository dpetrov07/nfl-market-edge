"""Exhaustive traded Kalshi combo pull for the Sep. 13 NFL slate."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from nfl_market_edge.kalshi import API_ROOT, KalshiClient


DEFAULT_ROOT = Path("data/sunday_2026-09-13")
CHECKPOINT_EVERY_PAGES = 25
# The four exchange settlement batches containing the 13 Sunday games.
SETTLEMENT_SLICES = (
    ("2026-09-13T18:00:00Z", "2026-09-13T20:00:00Z"),
    ("2026-09-13T20:00:00Z", "2026-09-13T22:00:00Z"),
    ("2026-09-13T22:00:00Z", "2026-09-14T00:00:00Z"),
    ("2026-09-14T02:00:00Z", "2026-09-14T04:00:00Z"),
)


def parse_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def local_market_catalog(root: Path):
    markets, event_games = {}, {}
    for path in sorted((root / "kalshi_live").glob("*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                if record.get("record_type") != "market_discovery":
                    continue
                for market in record.get("markets", []):
                    markets[market["ticker"]] = market
                    event_games[market["event_ticker"]] = market["game"]
                break
    return markets, event_games


class DiscoveryCheckpoint:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        if path.exists():
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                self.data = json.load(handle)
        else:
            self.data = {"version": 1, "slices": {}}

    @staticmethod
    def key(window):
        return "..".join(window)

    def load_slice(self, window):
        with self.lock:
            state = self.data["slices"].get(self.key(window), {})
            return {
                "cursor": state.get("cursor"),
                "complete": state.get("complete", False),
                "pages": state.get("pages", 0),
                "scanned": state.get("scanned", 0),
                "markets": list(state.get("markets", [])),
            }

    def save_slice(self, window, cursor, complete, pages, scanned, markets):
        with self.lock:
            self.data["slices"][self.key(window)] = {
                "cursor": cursor,
                "complete": complete,
                "pages": pages,
                "scanned": scanned,
                "markets": markets,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            with gzip.open(temporary, "wt", encoding="utf-8") as handle:
                json.dump(self.data, handle)
            os.replace(temporary, self.path)


def discover_markets(client: KalshiClient, event_games: dict, cache_path: Path):
    checkpoint = DiscoveryCheckpoint(cache_path)

    def fetch(window):
        start, end = map(parse_time, window)

        def wanted(market):
            legs = market.get("mve_selected_legs") or []
            return (
                len(legs) in (2, 3)
                and float(market.get("volume_fp") or 0) > 0
                and all(leg.get("event_ticker") in event_games for leg in legs)
            )

        state = checkpoint.load_slice(window)
        found = {market["ticker"]: market for market in state["markets"]}
        cursor, pages, scanned = state["cursor"], state["pages"], state["scanned"]
        if state["complete"]:
            for market in found.values():
                market["discovery_slice_end"] = window[1]
            print(
                f"settlement slice {window[0]}..{window[1]}: resumed complete "
                f"checkpoint ({scanned} scanned, {len(found)} retained)",
                file=sys.stderr,
                flush=True,
            )
            return window[1], list(found.values()), pages

        seen_cursors = {cursor} if cursor else set()
        while True:
            params = {
                "mve_filter": "only",
                "status": "settled",
                "min_settled_ts": int(start.timestamp()),
                "max_settled_ts": int(end.timestamp()),
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor
            payload = client.get(API_ROOT + "/markets", params)
            page_rows = payload.get("markets", [])
            scanned += len(page_rows)
            for market in page_rows:
                if wanted(market):
                    found[market["ticker"]] = market
            pages += 1
            cursor = payload.get("cursor")
            if cursor and cursor in seen_cursors:
                raise RuntimeError(f"repeated market cursor in settlement slice {window}")
            if cursor:
                seen_cursors.add(cursor)
            complete = not cursor
            if pages % CHECKPOINT_EVERY_PAGES == 0 or complete:
                checkpoint.save_slice(
                    window, cursor, complete, pages, scanned, list(found.values())
                )
                print(
                    f"{window[1]}: {scanned} markets scanned ({pages} pages), "
                    f"{len(found)} Sunday traded 2/3-leg combos retained; checkpoint saved",
                    file=sys.stderr,
                    flush=True,
                )
            if complete:
                break

        markets = list(found.values())
        for market in markets:
            market["discovery_slice_end"] = window[1]
        print(
            f"settlement slice {window[0]}..{window[1]}: "
            f"{scanned} markets scanned in {pages} pages; {len(markets)} retained",
            file=sys.stderr,
            flush=True,
        )
        return window[1], markets, pages

    found, page_counts = {}, {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for slice_end, markets, pages in pool.map(fetch, SETTLEMENT_SLICES):
            page_counts[slice_end] = pages
            for market in markets:
                found[market["ticker"]] = market
    return sorted(found.values(), key=lambda row: row["ticker"]), page_counts


def fetch_activity(client: KalshiClient, market):
    ticker = market["ticker"]
    trades, trade_pages, _ = client.paginate(
        API_ROOT + "/markets/trades", {"ticker": ticker, "limit": 1000}, "trades"
    )
    rfq_rows, rfq_pages, _ = client.paginate(
        API_ROOT + "/communications/rfqs",
        {"market_ticker": ticker, "limit": 100},
        "rfqs",
        auth=True,
    )
    quote_rows, quote_pages, _ = client.paginate(
        API_ROOT + "/communications/quotes",
        {"market_ticker": ticker, "limit": 500},
        "quotes",
        auth=True,
    )
    trades = [row for row in trades if row.get("ticker") == ticker]
    rfq_rows = [row for row in rfq_rows if row.get("market_ticker") == ticker]
    quote_rows = [row for row in quote_rows if row.get("market_ticker") == ticker]
    return (
        ticker,
        trades,
        rfq_rows,
        quote_rows,
        trade_pages,
        rfq_pages,
        quote_pages,
    )


def underlying_resolutions(client: KalshiClient, tickers: set[str]):
    chunks = [sorted(tickers)[index : index + 50] for index in range(0, len(tickers), 50)]

    def fetch(chunk):
        return client.get(
            API_ROOT + "/markets", {"tickers": ",".join(chunk), "limit": len(chunk)}
        ).get("markets", [])

    with ThreadPoolExecutor(max_workers=4) as pool:
        return {market["ticker"]: market for markets in pool.map(fetch, chunks) for market in markets}


def coverage(dataset_path: Path, id_column: str, wanted: set[str]):
    result = {}
    if not wanted:
        return result
    dataset = ds.dataset(dataset_path, format="parquet")
    scanner = dataset.scanner(
        columns=[id_column, "received_at"],
        filter=ds.field(id_column).isin(sorted(wanted)),
        batch_size=131072,
    )
    for batch in scanner.to_batches():
        ids = batch.column(id_column).to_pylist()
        times = batch.column("received_at").to_pylist()
        for key, timestamp in zip(ids, times):
            if key not in wanted or timestamp is None:
                continue
            timestamp = timestamp.astimezone(timezone.utc)
            if key not in result:
                result[key] = [timestamp, timestamp]
            else:
                result[key][0] = min(result[key][0], timestamp)
                result[key][1] = max(result[key][1], timestamp)
    return result


def stage_parquet(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(pa.Table.from_pylist(rows), temporary, compression="zstd")
    return temporary


def coverage_summary(combo_rows, activity_rows):
    combo_lookup = {row["combo_market_ticker"]: row for row in combo_rows}
    combos_by_game, fills_by_game, contracts_by_game = Counter(), Counter(), Counter()
    fill_times_by_game = defaultdict(list)
    for combo in combo_rows:
        for game in combo["games"].split(", "):
            combos_by_game[game] += 1
    fills = [row for row in activity_rows if row["activity_type"] == "trade"]
    for fill in fills:
        for game in combo_lookup[fill["combo_market_ticker"]]["games"].split(", "):
            fills_by_game[game] += 1
            contracts_by_game[game] += fill["size"]
            fill_times_by_game[game].append(fill["activity_at"])
    by_game = {
        game: {
            "combos": combos_by_game[game],
            "fills": fills_by_game[game],
            "contracts": round(contracts_by_game[game], 4),
            "first_fill_at": min(fill_times_by_game[game]).isoformat()
            if fill_times_by_game[game] else None,
            "last_fill_at": max(fill_times_by_game[game]).isoformat()
            if fill_times_by_game[game] else None,
        }
        for game in sorted(combos_by_game)
    }
    by_fill_hour = {}
    for fill in fills:
        hour = fill["activity_at"].replace(minute=0, second=0, microsecond=0).isoformat()
        bucket = by_fill_hour.setdefault(hour, {"fills": 0, "contracts": 0.0})
        bucket["fills"] += 1
        bucket["contracts"] += fill["size"]
    for bucket in by_fill_hour.values():
        bucket["contracts"] = round(bucket["contracts"], 4)
    return by_game, dict(sorted(by_fill_hour.items()))


def validate_full_sample(combo_rows, leg_rows, activity_rows, by_game):
    if len({row["combo_market_ticker"] for row in combo_rows}) != len(combo_rows):
        raise RuntimeError("duplicate combo market tickers in full sample")
    if any(not row["volume_reconciles"] for row in combo_rows):
        raise RuntimeError("one or more combo market volumes do not reconcile to fills")
    if sum(row["leg_count"] for row in combo_rows) != len(leg_rows):
        raise RuntimeError("combo and leg row counts do not reconcile")
    trade_count = sum(row["activity_type"] == "trade" for row in activity_rows)
    if sum(row["recovered_trade_count"] for row in combo_rows) != trade_count:
        raise RuntimeError("combo and activity fill counts do not reconcile")
    if len(by_game) != 13 or any(row["fills"] == 0 for row in by_game.values()):
        raise RuntimeError(f"expected fill coverage across 13 games, got {len(by_game)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--skip-communications", action="store_true")
    args = parser.parse_args()
    root = args.data_root
    output = args.output_dir or root / "combos"
    local, event_games = local_market_catalog(root)
    client = KalshiClient(authenticated=not args.skip_communications)
    market_cache = output / ".kalshi_nfl_combo_market_cache.json.gz"
    markets, discovery_pages = discover_markets(client, event_games, market_cache)

    activities = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = pool.map(lambda market: fetch_activity(client, market), markets)
        for completed, result in enumerate(results, start=1):
            ticker, trades, rfqs, quotes, trade_pages, rfq_pages, quote_pages = result
            activities[ticker] = (trades, rfqs, quotes, trade_pages, rfq_pages, quote_pages)
            if completed % 250 == 0:
                print(
                    f"activity: {completed}/{len(markets)} combo histories exhausted",
                    file=sys.stderr,
                    flush=True,
                )

    mapping_path = root / "mappings" / "bovada_kalshi_props.parquet"
    bovada_map = pq.read_table(mapping_path).to_pylist() if mapping_path.exists() else []
    mapped_bovada = {}
    for row in bovada_map:
        mapped_bovada.setdefault(row["kalshi_market_ticker"], set()).add(row["bovada_selection_id"])

    leg_tickers = {
        leg["market_ticker"] for market in markets for leg in market["mve_selected_legs"]
    }
    resolutions = underlying_resolutions(client, leg_tickers)
    bovada_ids = {sid for ticker in leg_tickers for sid in mapped_bovada.get(ticker, ())}
    kalshi_coverage = coverage(root / "processed" / "kalshi", "market_ticker", leg_tickers)
    bovada_coverage = coverage(root / "processed" / "bovada", "selection_id", bovada_ids)

    combo_rows, leg_rows, activity_rows = [], [], []
    for market in markets:
        ticker = market["ticker"]
        legs = market["mve_selected_legs"]
        games = [event_games[leg["event_ticker"]] for leg in legs]
        trades, rfqs, quotes, trade_pages, rfq_pages, quote_pages = activities[ticker]
        raw_activity = []
        for trade in trades:
            raw_activity.append(
                {
                    "activity_type": "trade",
                    "activity_id": trade.get("trade_id"),
                    "activity_at": parse_time(trade.get("created_time")),
                    "size": float(trade.get("count_fp") or 0),
                    "yes_price": float(trade.get("yes_price_dollars") or 0),
                    "no_price": float(trade.get("no_price_dollars") or 0),
                    "target_cost": None,
                    "status": "filled",
                    "taker_side": trade.get("taker_side"),
                    "accepted_side": None,
                    "rfq_id": None,
                }
            )
        for rfq in rfqs:
            raw_activity.append(
                {
                    "activity_type": "rfq",
                    "activity_id": rfq.get("id"),
                    "activity_at": parse_time(rfq.get("created_ts")),
                    "size": float(rfq.get("contracts_fp") or 0),
                    "yes_price": None,
                    "no_price": None,
                    "target_cost": float(rfq["target_cost_dollars"])
                    if rfq.get("target_cost_dollars") else None,
                    "status": rfq.get("status"),
                    "taker_side": None,
                    "accepted_side": None,
                    "rfq_id": rfq.get("id"),
                }
            )
        for quote in quotes:
            raw_activity.append(
                {
                    "activity_type": "quote",
                    "activity_id": quote.get("id"),
                    "activity_at": parse_time(quote.get("created_ts")),
                    "size": float(quote.get("contracts_fp") or 0),
                    "yes_price": float(quote["yes_bid_dollars"])
                    if quote.get("yes_bid_dollars") else None,
                    "no_price": float(quote["no_bid_dollars"])
                    if quote.get("no_bid_dollars") else None,
                    "target_cost": float(quote["rfq_target_cost_dollars"])
                    if quote.get("rfq_target_cost_dollars") else None,
                    "status": quote.get("status"),
                    "taker_side": None,
                    "accepted_side": quote.get("accepted_side"),
                    "rfq_id": quote.get("rfq_id"),
                }
            )

        for activity in raw_activity:
            at = activity["activity_at"]
            k_join = at is not None and all(
                leg["market_ticker"] in kalshi_coverage
                and kalshi_coverage[leg["market_ticker"]][0] <= at <= kalshi_coverage[leg["market_ticker"]][1]
                for leg in legs
            )
            b_join = at is not None and any(
                sid in bovada_coverage
                and bovada_coverage[sid][0] <= at <= bovada_coverage[sid][1]
                for leg in legs
                for sid in mapped_bovada.get(leg["market_ticker"], ())
            )
            activity_rows.append(
                {
                    "combo_market_ticker": ticker,
                    "leg_count": len(legs),
                    "scope": "same_game" if len(set(games)) == 1 else "cross_game",
                    **activity,
                    "kalshi_joinable": k_join,
                    "bovada_joinable": b_join,
                }
            )

        trade_size = sum(float(trade.get("count_fp") or 0) for trade in trades)
        combo_rows.append(
            {
                "combo_market_ticker": ticker,
                "combo_event_ticker": market.get("event_ticker"),
                "collection_ticker": market.get("mve_collection_ticker"),
                "created_at": parse_time(market.get("created_time")),
                "settled_at": parse_time(market.get("settlement_ts")),
                "result": market.get("result"),
                "settlement_value": float(market["settlement_value_dollars"])
                if market.get("settlement_value_dollars") else None,
                "leg_count": len(legs),
                "scope": "same_game" if len(set(games)) == 1 else "cross_game",
                "games": ", ".join(sorted(set(games))),
                "mapped_leg_count": sum(leg["market_ticker"] in local for leg in legs),
                "bovada_mappable_leg_count": sum(
                    leg["market_ticker"] in mapped_bovada for leg in legs
                ),
                "market_volume": float(market.get("volume_fp") or 0),
                "recovered_trade_count": len(trades),
                "recovered_trade_size": trade_size,
                "volume_reconciles": abs(trade_size - float(market.get("volume_fp") or 0)) < 1e-6,
                "trade_history_truncated": False,
                "recovered_rfq_count": len(rfqs),
                "rfq_history_truncated": False,
                "recovered_quote_count": len(quotes),
                "quote_history_truncated": False,
                "discovery_slice_end": parse_time(market["discovery_slice_end"]),
            }
        )

        for index, leg in enumerate(legs, start=1):
            market_info = local.get(leg["market_ticker"], {})
            resolution = resolutions.get(leg["market_ticker"], {})
            raw_yes_value = leg.get("yes_settlement_value_dollars") or resolution.get(
                "settlement_value_dollars"
            )
            yes_value = float(raw_yes_value) if raw_yes_value is not None else None
            selected_value = yes_value if leg.get("side") == "yes" else (
                1 - yes_value if yes_value is not None else None
            )
            leg_rows.append(
                {
                    "combo_market_ticker": ticker,
                    "leg_index": index,
                    "underlying_event_ticker": leg.get("event_ticker"),
                    "underlying_market_ticker": leg.get("market_ticker"),
                    "side": leg.get("side"),
                    "game": event_games.get(leg.get("event_ticker")),
                    "prop_type": market_info.get("prop_type"),
                    "player": market_info.get("player"),
                    "player_id": market_info.get("player_id"),
                    "threshold": market_info.get("threshold"),
                    "outcome_team": market_info.get("outcome_team"),
                    "mapped_to_sunday_kalshi": leg.get("market_ticker") in local,
                    "mapped_to_bovada": leg.get("market_ticker") in mapped_bovada,
                    "underlying_result": resolution.get("result"),
                    "yes_settlement_value": yes_value,
                    "selected_leg_settlement_value": selected_value,
                }
            )

    by_game, by_fill_hour = coverage_summary(combo_rows, activity_rows)
    validate_full_sample(combo_rows, leg_rows, activity_rows, by_game)

    outputs = [
        (output / "kalshi_nfl_combos.parquet", combo_rows),
        (output / "kalshi_nfl_combo_legs.parquet", leg_rows),
        (output / "kalshi_nfl_combo_activity.parquet", activity_rows),
    ]
    staged = [(path, stage_parquet(path, rows)) for path, rows in outputs]
    for (path, rows), (_, temporary) in zip(outputs, staged):
        if pq.ParquetFile(temporary).metadata.num_rows != len(rows):
            raise RuntimeError(f"staged row count mismatch for {path}")
    for path, temporary in staged:
        os.replace(temporary, path)
    market_cache.unlink(missing_ok=True)
    print(
        json.dumps(
            {
                "combos": len(combo_rows),
                "legs": len(leg_rows),
                "trades": sum(row["activity_type"] == "trade" for row in activity_rows),
                "rfqs": sum(row["activity_type"] == "rfq" for row in activity_rows),
                "quotes": sum(row["activity_type"] == "quote" for row in activity_rows),
                "quote_history_available": API_ROOT + "/communications/quotes"
                not in client.denied_paths,
                "discovery_pages_by_slice_end": discovery_pages,
                "all_market_cursors_exhausted": True,
                "all_activity_cursors_exhausted": True,
                "volume_reconciled_combos": sum(row["volume_reconciles"] for row in combo_rows),
                "coverage_by_game": by_game,
                "fills_by_hour_utc": by_fill_hour,
                "output": str(output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
