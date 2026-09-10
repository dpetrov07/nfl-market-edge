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
```

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
