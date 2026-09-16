# NFL Market Edge

NFL Market Edge collects live NFL market and play-by-play data from Kalshi,
Bovada, and ESPN. Historical backfills and fair-value research are no longer in
scope.

## Install

```bash
python -m pip install -r requirements.txt
```

## Live Bovada WebSocket

The collector discovers receiving-yard, rushing-yard, and reception props over
HTTP, then records selection changes from one WebSocket subscription per game.

```bash
python scripts/collect_live_bovada_ws.py --date YYYY-MM-DD
```

For Railway, use `railway.bovada.toml`, attach a volume at `/data`, and set:

```text
BOVADA_GAME_DATE=YYYY-MM-DD
BOVADA_OUTPUT_DIR=/data/bovada_live
```

`BOVADA_GAMES` can optionally contain comma-separated Bovada game descriptions.

## Live Kalshi WebSocket

The collector records material top-of-book changes, trades, lifecycle events,
reconnect/gap events, and compact heartbeats for each requested game.

```bash
KALSHI_API_KEY_ID=... \
KALSHI_PRIVATE_KEY=/absolute/path/to/kalshi-private-key.pem \
python scripts/collect_live_kalshi_ws.py \
  --game SF_LAR --game BUF_HOU --date YYYY-MM-DD
```

For Railway, use `railway.kalshi.toml`, attach a volume at `/data`, and set:

```text
KALSHI_API_KEY_ID=<your key id>
KALSHI_PRIVATE_KEY=<the complete PEM private key>
KALSHI_GAMES=SF_LAR,BUF_HOU
KALSHI_GAME_DATE=YYYY-MM-DD
KALSHI_CHANNELS=ticker,orderbook_delta,trade,market_lifecycle_v2
KALSHI_HEARTBEAT_SECONDS=5
KALSHI_TOP_SIZE_CHANGE=10
KALSHI_OUTPUT_DIR=/data/kalshi_live
```

`KALSHI_PRIVATE_KEY_B64` can be used instead of `KALSHI_PRIVATE_KEY`.

## Live NFL play-by-play

The ESPN collector discovers a date's games and concurrently records new plays,
corrections/removals, and source status events.

```bash
python scripts/collect_live_nfl_pbp.py --date YYYY-MM-DD --poll-seconds 1.5
```

For Railway, use `railway.nfl-live.toml`, attach a volume at `/data`, and set:

```text
NFL_LIVE_DATE=YYYY-MM-DD
NFL_LIVE_POLL_SECONDS=1.5
NFL_LIVE_OUTPUT_DIR=/data/nfl_live
NFL_LIVE_START_MINUTES_BEFORE=10
NFL_LIVE_STOP_HOURS_AFTER=8
NFL_LIVE_FINAL_GRACE_MINUTES=10
```

`NFL_LIVE_GAME_IDS` can optionally contain comma-separated ESPN event IDs.

The preserved local live capture is under `data/sunday_2026-09-13/`.

## Player-prop opportunity ranking

Rank executable Kalshi prices observed immediately after clean Bovada repricings:

```bash
python market_timing/rank_props.py --top 10
```

Add `--latest-only` for a live-style view with one most-recent signal per market;
without it, the output retains every historical decision-time snapshot for audit.

Look up a specific line with player, prop, and threshold filters:

```bash
python market_timing/rank_props.py --latest-only \
  --player "Nico Collins" --prop-type receiving_yards --threshold 79.5
```

The Parquet output includes the executable side/price and size, Bovada no-vig
fair probability, gross and fee/slippage-adjusted edge, recent repricing scope,
quote availability, neighboring-threshold sanity check, a coarse recommendation,
the numeric heuristic labeled `signal_score`, and the recommendation tier's
historical 10- and 30-second executable markouts. Sample size and historical
hit/median statistics are displayed separately from the signal score.
`strong` is the former `enter` group in the validated 50+ display-score band;
the remaining `enter`, `watch`, and `pass` decisions are unchanged.
`enter` requires the filters supported by the Sunday timing study: a game-wide
Bovada update, at least 5 points of disagreement, spread no wider than 5 cents,
and at least 50 contracts available. The reported fair-value range is the old to
new Bovada no-vig interval, not a calibrated confidence interval.
