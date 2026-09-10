
from __future__ import annotations

import argparse
import gzip
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"

# Full 2025 NFL season by default, including playoffs / Super Bowl LX.
DEFAULT_START = "2025-09-01T00:00:00Z"
DEFAULT_END = "2026-02-10T23:59:59Z"

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download 2025 NFL receiving/rushing market histories from Kalshi and nflverse."
    )
    p.add_argument("--out", default="data/raw", help="Output directory")
    p.add_argument("--start", default=DEFAULT_START)
    p.add_argument("--end", default=DEFAULT_END)
    p.add_argument(
        "--platform",
        choices=["all", "kalshi", "nflverse"],
        default="all",
    )
    p.add_argument(
        "--include-season-long",
        action="store_true",
        help="Deprecated: this research spike only supports individual-game props.",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=0.12,
        help="Delay between API requests.",
    )
    p.add_argument("--discovery-only", action="store_true")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument(
        "--prop-type",
        choices=["receiving_yards", "rushing_yards"],
        help="Limit history retrieval to one prop type; discovery still refreshes the full catalog.",
    )
    p.add_argument("--history-limit", type=int, default=0, help="Deterministic stratified history sample; 0 pulls all markets")
    return p.parse_args()


def to_ts(value: str | datetime) -> int:
    return int(pd.Timestamp(value).timestamp())


def as_dt(value: Any) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    try:
        return pd.to_datetime(value, utc=True)
    except Exception:
        return None


def request_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
    retries: int = 4,
    sleep: float = 0.12,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=45)
            if r.status_code == 429:
                wait = min(2 ** attempt, 10)
                print(f"429 rate limit: sleeping {wait}s -> {r.url}")
                time.sleep(wait)
                continue
            if 400 <= r.status_code < 500 and r.status_code != 429:
                raise RuntimeError(f"HTTP {r.status_code}: {r.url}: {r.text[:300]}")
            r.raise_for_status()
            time.sleep(sleep)
            return r.json()
        except RuntimeError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt == retries - 1:
                raise
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(last_error or f"HTTP 429 persisted after {retries} attempts: {url}")


def write_parquet(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        print(f"No rows for {path.name}")
        pd.DataFrame().to_parquet(path, index=False)
        return
    pd.DataFrame(rows).drop_duplicates().to_parquet(path, index=False)
    print(f"Wrote {len(rows):,} rows -> {path}")


# -------------------------
# Kalshi
# -------------------------

def kalshi_series(session: requests.Session, sleep: float) -> list[dict[str, Any]]:
    data = request_json(
        session,
        f"{KALSHI_BASE}/series",
        params={"category": "Sports", "include_product_metadata": "true"},
        sleep=sleep,
    )
    return data.get("series", [])


def paginate_kalshi_historical_markets(
    session: requests.Session,
    series_ticker: str,
    sleep: float,
) -> Iterable[dict[str, Any]]:
    cursor = None
    while True:
        params: dict[str, Any] = {
            "limit": 1000,
            "series_ticker": series_ticker,
        }
        if cursor:
            params["cursor"] = cursor
        data = request_json(
            session,
            f"{KALSHI_BASE}/historical/markets",
            params=params,
            sleep=sleep,
        )
        for market in data.get("markets", []):
            yield market
        cursor = data.get("cursor")
        print(f"  {series_ticker}: page {len(data.get('markets', []))} markets", flush=True)
        if not cursor:
            break


# Three verified individual-game threshold series. Excludes leaders, combined
# rushing+receiving, head-to-head, college, and season-long contracts.
KALSHI_SERIES = {
    "KXNFLRECYDS": "receiving_yards",
    "KXNFLRSHYDS": "rushing_yards",
}


def dump_json(value, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if str(path).endswith(".gz"):
        with gzip.open(path, "wt") as f:
            json.dump(value, f, default=str)
    else:
        path.write_text(json.dumps(value, indent=2, default=str))


def schedules(out_dir):
    d = pd.read_parquet(out_dir / "nflverse_schedules_2025.parquet")
    # nflverse gametime is US Eastern local time, including DST.
    d["kickoff"] = pd.to_datetime(d.gameday + " " + d.gametime).dt.tz_localize(
        "America/New_York").dt.tz_convert("UTC")
    return d


def kalshi_game(event_ticker, games):
    part = event_ticker.split("-")[1]
    day = pd.to_datetime(part[:7], format="%y%b%d").strftime("%Y-%m-%d")
    pair = part[7:]
    aliases = {"LA": "LAR", "WSH": "WAS", "JAX": "JAC"}
    for g in games[games.gameday.eq(day)].to_dict("records"):
        combinations = {a+b for a in {g['away_team'], aliases.get(g['away_team'], g['away_team'])}
                        for b in {g['home_team'], aliases.get(g['home_team'], g['home_team'])}}
        if pair in combinations:
            return g
    return {}


def scrape_kalshi(out_dir, start, end, include_season_long, sleep):
    s = requests.Session()
    series_path = out_dir / "kalshi_sports_series.json"
    if series_path.exists():
        series = json.loads(series_path.read_text())
    else:
        series = kalshi_series(s, sleep)
        dump_json(series, series_path)
    cutoff_path = out_dir / "kalshi_cutoff.json"
    if not cutoff_path.exists():
        dump_json(
            request_json(s, f"{KALSHI_BASE}/historical/cutoff", sleep=sleep),
            cutoff_path,
        )
    games = schedules(out_dir)
    rows = []
    for st, prop in KALSHI_SERIES.items():
        cache = out_dir / f"kalshi_{st}_markets.json.gz"
        if cache.exists():
            with gzip.open(cache, "rt") as f: markets = json.load(f)
        else:
            markets = list(paginate_kalshi_historical_markets(s, st, sleep))
            dump_json(markets, cache)
        for m in markets:
            g = kalshi_game(m["event_ticker"], games)
            if not g or not start <= g["kickoff"] <= end:
                continue
            rows.append({"platform": "kalshi", "market_id": m["ticker"],
                "event_ticker": m["event_ticker"], "series_ticker": st,
                "prop_type": prop, "title": m.get("title"),
                "player": m.get("yes_sub_title", m.get("title", "")).split(":")[0].strip(),
                "threshold": m.get("floor_strike"), "strike_type": m.get("strike_type"),
                "threshold_label": m.get("yes_sub_title"),
                "player_source_id": m.get("custom_strike", {}).get("football_player"),
                "game_id": g["game_id"], "game": g["away_team"]+" @ "+g["home_team"],
                "kickoff": g["kickoff"], "open_time": as_dt(m.get("open_time")),
                "close_time": as_dt(m.get("close_time")), "result": m.get("result"),
                "settlement_ts": m.get("settlement_ts"),
                "market_volume": m.get("volume_fp"), "market_open_interest": m.get("open_interest_fp")})
    write_parquet(rows, out_dir / "kalshi_prop_markets.parquet")


def candle_number(obj, key):
    # Current historical endpoint returns dollar strings under plain keys.
    # Legacy numeric plain prices were cents; explicit *_dollars wins.
    value = obj.get(key+"_dollars")
    if value is not None: return float(value)
    value = obj.get(key)
    if value is None: return None
    return float(value) if isinstance(value, str) else float(value)/100


def kalshi_candles(session, ticker, start_ts, end_ts, sleep):
    rows = []
    # Live API probe: historical requests cap the time range at 5,000 minutes.
    for current in range(start_ts, end_ts+1, 3*86400):
        data = request_json(session, f"{KALSHI_BASE}/historical/markets/{ticker}/candlesticks",
            params={"start_ts": current, "end_ts": min(current+3*86400-1, end_ts),
                    "period_interval": 1}, sleep=sleep)
        rows.extend(data.get("candlesticks", []))
    return list({c["end_period_ts"]: c for c in rows}.values())


def pull_histories(out_dir, platform, sleep, workers, limit=0, prop_type=None):
    catalog = pd.read_parquet(out_dir / f"{platform}_prop_markets.parquet")
    markets = catalog[catalog.prop_type.eq(prop_type)].copy() if prop_type else catalog.copy()
    all_markets = markets.copy()
    if limit and len(markets) > limit:
        # Even coverage over game dates and prop types; reproducible random sample.
        markets = markets.sort_values(["prop_type", "kickoff", "market_id"])
        import numpy as np
        markets = markets.iloc[np.linspace(0, len(markets)-1, limit, dtype=int)]
    selection_name = (
        f"{platform}_{prop_type}_history_selection.json"
        if prop_type
        else f"{platform}_history_selection.json"
    )
    dump_json(markets.market_id.tolist(), out_dir / selection_name)
    folder = out_dir / f"{platform}_price_parts"
    folder.mkdir(exist_ok=True)
    # Keep and repair the earlier pilot alongside the selected season sample.
    cached_ids = {p.stem for p in folder.glob("*.json")}
    markets = all_markets[all_markets.market_id.isin(set(markets.market_id) | cached_ids)]
    def pull(m):
        path = folder / (m["market_id"]+".parquet")
        audit_path = folder / (m["market_id"]+".json")
        if path.exists() and audit_path.exists():
            audit = json.loads(audit_path.read_text())
            if audit["status"] != "error": return audit
        s = requests.Session()
        rows, raw, errors = [], {}, []
        lo = hi = None
        try:
            opening = m["open_time"] if pd.notna(m["open_time"]) else m.get("created_time")
            lo = int(opening.timestamp())
            hi = int(m["close_time"].timestamp())
            raw = kalshi_candles(s, m["market_id"], lo, hi, sleep)
            for c in raw:
                r = {"market_id": m["market_id"], "timestamp": pd.to_datetime(c["end_period_ts"], unit="s", utc=True)}
                for source, prefix in [("yes_bid", "yes_bid"), ("yes_ask", "yes_ask"), ("price", "trade")]:
                    for field in ["open", "high", "low", "close", "mean", "previous"]:
                        if field in c.get(source, {}) or field+"_dollars" in c.get(source, {}):
                            r[prefix+"_"+field] = candle_number(c[source], field)
                for field in ["volume", "open_interest"]:
                    v = c.get(field+"_fp", c.get(field))
                    r[field] = float(v) if v is not None else None
                rows.append(r)
        except Exception as exc: errors.append(str(exc))
        except Exception as exc: errors.append(str(exc))
        dump_json(raw, folder / (m["market_id"]+".json.gz"))
        df = pd.DataFrame(rows)
        if df.empty: df = pd.DataFrame(columns=["market_id", "timestamp"])
        df.drop_duplicates().to_parquet(path, index=False)
        audit = {"market_id": m["market_id"], "status": "error" if errors else "ok" if rows else "empty",
                 "observations": len(df), "errors": errors, "start_ts": lo, "end_ts": hi,
                 "retrieved_utc": datetime.now(timezone.utc).isoformat()}
        dump_json(audit, audit_path)
        return audit
    audits = []
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(pull, m) for m in markets.to_dict("records")]
        for f in as_completed(futures):
            audits.append(f.result())
            if len(audits) % 100 == 0 or len(audits) == len(futures):
                print(f"{platform}: {len(audits)}/{len(futures)} histories; "
                      f"{sum(a['observations'] for a in audits):,} observations; "
                      f"{sum(a['status']=='error' for a in audits)} errors", flush=True)
    except KeyboardInterrupt:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        print("Interrupted; completed per-market checkpoints were preserved.", flush=True)
        raise
    else:
        pool.shutdown()
    dump_json(audits, out_dir / f"{platform}_history_audit.json")
    # Stream parts rather than holding millions of candles in Python dictionaries.
    import pyarrow as pa
    import pyarrow.parquet as pq
    catalog_ids = set(catalog.market_id)
    parts = sorted(
        p for p in folder.glob("*.parquet") if p.stem in catalog_ids
    )
    schemas = [pq.read_schema(p) for p in parts if pq.read_metadata(p).num_rows]
    destination = out_dir / f"{platform}_prop_prices_1m.parquet"
    if not schemas:
        pd.DataFrame(columns=["market_id", "timestamp", "price"]).to_parquet(destination, index=False)
    else:
        schema = pa.unify_schemas(schemas, promote_options="permissive")
        with pq.ParquetWriter(destination, schema, compression="zstd") as writer:
            for p in parts:
                if not pq.read_metadata(p).num_rows: continue
                table = pq.read_table(p)
                for field in schema:
                    if field.name not in table.column_names:
                        table = table.append_column(field.name, pa.nulls(len(table), type=field.type))
                writer.write_table(table.select(schema.names).cast(schema))


# -------------------------
# nflverse
# -------------------------

def save_polars(df: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)
    print(f"Wrote {df.height:,} rows -> {path}")


def scrape_nflverse(out_dir: Path) -> None:
    print("\n=== NFLVERSE 2025 ===")
    import nflreadpy as nfl

    stats = nfl.load_player_stats(2025, summary_level="week")
    save_polars(stats, out_dir / "nflverse_player_stats_2025.parquet")

    schedules = nfl.load_schedules(2025)
    save_polars(schedules, out_dir / "nflverse_schedules_2025.parquet")

    snaps = nfl.load_snap_counts(2025)
    save_polars(snaps, out_dir / "nflverse_snap_counts_2025.parquet")

    ngs_rush = nfl.load_nextgen_stats(2025, stat_type="rushing")
    save_polars(ngs_rush, out_dir / "nflverse_ngs_rushing_2025.parquet")

    ngs_rec = nfl.load_nextgen_stats(2025, stat_type="receiving")
    save_polars(ngs_rec, out_dir / "nflverse_ngs_receiving_2025.parquet")

    # These provide extra route / usage / opportunity features and can be
    # useful later. Failure should not block the base dataset.
    try:
        adv_rush = nfl.load_pfr_advstats(
            2025, stat_type="rush", summary_level="week"
        )
        save_polars(adv_rush, out_dir / "nflverse_pfr_rushing_2025.parquet")
    except Exception as exc:
        print(f"PFR rushing advanced stats unavailable: {exc}")

    try:
        adv_rec = nfl.load_pfr_advstats(
            2025, stat_type="rec", summary_level="week"
        )
        save_polars(adv_rec, out_dir / "nflverse_pfr_receiving_2025.parquet")
    except Exception as exc:
        print(f"PFR receiving advanced stats unavailable: {exc}")


def main() -> None:
    args = parse_args()
    if args.include_season_long:
        raise SystemExit("Only individual-game props are supported in this spike.")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)

    print(f"Window: {start} -> {end}")
    print("Targets: receiving_yards, rushing_yards")

    if args.platform in ("all", "nflverse") or not (out_dir / "nflverse_schedules_2025.parquet").exists():
        scrape_nflverse(out_dir)
    if args.platform in ("all", "kalshi"):
        scrape_kalshi(out_dir, start, end, args.include_season_long, args.sleep)
        if not args.discovery_only:
            pull_histories(
                out_dir,
                "kalshi",
                args.sleep,
                args.workers,
                args.history_limit,
                args.prop_type,
            )

    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "start": str(start),
        "end": str(end),
        "targets": ["receiving_yards", "rushing_yards"],
        "notes": [
            "Kalshi output uses official historical 1-minute candlesticks when markets are discoverable.",
            "nflverse output contains actual game results and predictive player usage/performance features.",
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print("\nDone.")


if __name__ == "__main__":
    main()
