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
