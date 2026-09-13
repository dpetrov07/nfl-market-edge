# NFL Market Edge

NFL Market Edge is a research project for estimating the fair probability of
live Kalshi NFL receiving- and rushing-yard contracts from current game and
player state, then identifying potentially underpriced or overpriced markets
after accounting for executable bid/ask prices.

The project has not demonstrated a profitable strategy. Research and paper
evaluation come before alerts or execution.

## Current scope

- Kalshi NFL markets only
- Receiving yards and rushing yards only
- Historical prices and, later, live in-game prices
- Fair-value research using executable YES and NO prices

## Current data

`data/processed/kalshi_player_prop_history.parquet` is the canonical 2025 table.
It contains:

- 14,893 Kalshi player-prop threshold markets
- 4,206,480 timestamped observations
- YES bid/ask, midpoint, trade price, spread, volume, and open interest
- Player, game, opponent, threshold, kickoff distance, and actual result
- Receiving-yard and rushing-yard contracts

Individual games can have roughly minute-level post-kickoff quote coverage. That
coverage is useful for market-state research, although actual trade observations
are much sparser and wide bid/ask spreads make some snapshots non-executable.

Raw Kalshi catalogs, per-market candle checkpoints, matching tables, and useful
nflverse schedules/player results remain under `data/raw/`.

## Research direction

```text
Kalshi market history
        +
NFL play-by-play
        ↓
historical player/game state
        ↓
fair-value model
        ↓
compare with Kalshi bid/ask
        ↓
YES / NO value alerts
```

## Existing tools

```bash
# Inspect a player's historical markets and threshold ladders.
python scripts/show_prop.py --player "Tee Higgins" --prop receiving_yards

# Inspect quote density and activity for one game.
python scripts/inspect_game_history.py --game 2025_13_LA_CAR

# Build the canonical table from saved Kalshi checkpoints.
python scripts/build_kalshi_canonical.py

# Resume historical Kalshi collection. Completed checkpoints are skipped.
python scripts/build_2025_prop_history.py --platform kalshi --workers 1

# Reusable live top-of-book collector.
python scripts/collect_live_props.py --game AWAY_HOME --kickoff YYYY-MM-DDTHH:MM:SSZ

# Price the two-game sportsbook-history spike before using paid API credits.
python scripts/pull_sportsbook_history.py --estimate-only --include-alternates

# Pull checkpointed 5-minute sportsbook history, then join exact thresholds.
THE_ODDS_API_KEY=... python scripts/pull_sportsbook_history.py --include-alternates
python scripts/join_sportsbook_sample.py
```

## Live sportsbook player props

The lightweight collector polls Bovada receiving yards, rushing
yards, receptions, and each game's main moneyline/spread. It appends every
offered player threshold and the current full-game lines to a daily JSONL file,
along with one source-health record per poll. A temporary source error or an
empty in-game feed is recorded and retried on the next cycle.

```bash
python scripts/collect_live_sportsbook_props.py \
  --game "SF 49ers @ LA Rams" \
  --interval 30
```

For the Railway service named `sportsbook-collector`, attach its own persistent
volume at `/data` and set:

```text
SPORTSBOOK_GAME=SF 49ers @ LA Rams
SPORTSBOOK_BOOKS=bovada
SPORTSBOOK_POLL_SECONDS=30
SPORTSBOOK_OUTPUT_DIR=/data/sportsbook_live
```

Use `python scripts/collect_live_sportsbook_props.py` as that Railway service's
start command. These public sportsbook feeds do not require API credentials,
but they may remove or suspend player props during a game.

### Local Bovada WebSocket collector

The WebSocket collector uses the HTTP feed once to discover every main and
alternate receiving-yards, rushing-yards, and receptions selection, then opens
one independent Bovada subscription per game. It writes one compact
`.jsonl.gz` file per game and records only the initial selection state and real
odds, line, suspension/removal, or reappearance changes. `received_at` is the
local UTC time captured immediately after each WebSocket receive completes.

```bash
# All NFL games on the given Sunday.
python scripts/collect_live_bovada_ws.py --date 2026-09-13

# A short local proof for one or more games.
python scripts/collect_live_bovada_ws.py --date 2026-09-13 \
  --game "Miami Dolphins @ Las Vegas Raiders" --run-seconds 120 --verbose
```

Alternate `50+` selections are stored as `line: 49.5`,
`semantic_operator: "at_least"`, and `semantic_threshold: 50` so their event
semantics are explicit for later Kalshi/NFL-live joins. This collector is
local-only; it is not wired into either Railway service.

## Live Kalshi WebSocket

The Kalshi collector discovers one game's active receiving/rushing thresholds
plus its game moneyline and spread ladders,
subscribes to `ticker`, `orderbook_delta`, public `trade`, and market lifecycle.
It keeps full books in memory and writes only material top-of-book changes,
trades, status/reconnect/gap events, and a compact five-second heartbeat to
per-game `.jsonl.gz` files. Same-price size changes are saved after accumulating
10 contracts by default.

```bash
KALSHI_API_KEY_ID=... \
KALSHI_PRIVATE_KEY=/absolute/path/to/kalshi-private-key.pem \
python scripts/collect_live_kalshi_ws.py --game SF_LAR --date 2026-09-10
```

For the separate Railway service named `kalshi-collector`, use
`python scripts/collect_live_kalshi_ws.py` as the start command and attach a
separate persistent volume at `/data`. Set:

```text
KALSHI_API_KEY_ID=<your key id>
KALSHI_PRIVATE_KEY=<the complete PEM private key>
KALSHI_GAME=SF_LAR
KALSHI_GAME_DATE=2026-09-10
KALSHI_CHANNELS=ticker,orderbook_delta,trade,market_lifecycle_v2
KALSHI_HEARTBEAT_SECONDS=5
KALSHI_TOP_SIZE_CHANGE=10
KALSHI_OUTPUT_DIR=/data/kalshi_live
```

`KALSHI_PRIVATE_KEY` accepts a normal multiline PEM or a single-line PEM with
literal `\\n` characters. As a Railway-friendly alternative, omit it and set
`KALSHI_PRIVATE_KEY_B64` to the base64-encoded PEM. The two
services are independent: each has its own process, variables, and `/data`
volume. The shared `railway.toml` intentionally contains no start command, so
each service uses the command configured in its Railway settings.

To create both workers, connect the GitHub repository to a new Railway project
twice. Name the services `sportsbook-collector` and `kalshi-collector`, set the
respective start command and variables above, then add one volume to each
service with mount path `/data`. No public domain or cron schedule is needed.

## Live NFL play-by-play

The ESPN collector discovers every NFL game on a date and polls the games
concurrently. It writes only new plays, corrections/removals, and source status
events to a separate gzip JSONL file per game. Full API responses are discarded.

```bash
python scripts/collect_live_nfl_pbp.py --date 2026-09-13 --poll-seconds 1.5
```

For one Railway worker serving all games, attach one persistent volume at
`/data`, set its Railway config-file path to `/railway.nfl-live.toml`, and set:

```text
NFL_LIVE_DATE=2026-09-13
NFL_LIVE_POLL_SECONDS=1.5
NFL_LIVE_OUTPUT_DIR=/data/nfl_live
NFL_LIVE_START_MINUTES_BEFORE=10
NFL_LIVE_STOP_HOURS_AFTER=8
NFL_LIVE_FINAL_GRACE_MINUTES=10
```

`NFL_LIVE_GAME_IDS` may optionally contain comma-separated ESPN event IDs.
Leave it unset to collect the entire date. The worker waits until shortly before
each kickoff, then keeps that game alive through ESPN's final play plus the
configured correction grace period.

Install the small Python dependency set from `requirements.txt`. The completed
canonical dataset does not need to be rebuilt for normal inspection.

## Near-term milestones

1. Join one 2025 game's Kalshi observations to nflverse play-by-play.
2. Validate game and player-state reconstruction.
3. Scale the historical join across useful 2025 games.
4. Build a baseline fair-probability model.
5. Backtest executable YES/NO value signals.
6. Build a live alert system.

## Future ideas

- Short-term price-movement prediction and buy/sell timing
- Threshold-ladder inconsistencies and possible arbitrage
- Position hedging and locked-profit opportunities
- Combo/parlay pricing
- Automated execution only if the research supports it

The current dataset and findings are summarized in
`KALSHI_2025_FINAL_REPORT.md`.
