# 2025 Kalshi receiving/rushing prop dataset

The checkpointed backfill is complete. All 10,254 receiving-yard markets and all
4,639 rushing-yard markets succeeded with no remaining errors. The consolidated
raw history and canonical table each contain 4,206,480 observed
`(market_id, timestamp)` rows across 14,893 threshold markets.

The canonical file is `data/processed/kalshi_player_prop_history.parquet` (152 MB).
It contains player and normalized player ID when matched, game/opponent, prop,
threshold, timestamp, kickoff and hours to kickoff, YES bid/ask, midpoint, trade
price, spread, candle volume, open interest, actual result, and source tickers.

## Actual examples

To avoid treating placeholder opening books as prices, the early, low, high, and
final columns below use pregame quotes with spread at most 10¢. Movement is early
to final in percentage points of contract price.

| Player | Opponent | Prop | Threshold | Early | Low | High | Final | Actual | Move |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| James Cook III | JAX | Receiving | >19.5 | 36.5¢ | 35.0¢ | 58.5¢ | 58.5¢ | 5 | +22.0¢ |
| Jaxon Smith-Njigba | NE | Receiving | >119.5 | 15.0¢ | 15.0¢ | 40.0¢ | 31.5¢ | 27 | +16.5¢ |
| De'Von Achane | CLE | Receiving | >9.5 | 92.0¢ | 81.5¢ | 92.0¢ | 81.5¢ | 16 | -10.5¢ |
| Davante Adams | JAX | Receiving | >89.5 | 49.0¢ | 40.0¢ | 49.5¢ | 40.0¢ | 35 | -9.0¢ |
| Puka Nacua | CAR | Receiving | >89.5 | 50.5¢ | 48.5¢ | 53.5¢ | 50.5¢ | 111 | 0.0¢ |
| Lamar Jackson | PIT | Rushing | >29.5 | 42.0¢ | 42.0¢ | 57.5¢ | 57.0¢ | 9 | +15.0¢ |
| Josh Jacobs | CHI | Rushing | >69.5 | 51.0¢ | 49.5¢ | 62.5¢ | 61.5¢ | 55 | +10.5¢ |
| Josh Jacobs | BAL | Rushing | >39.5 | 75.5¢ | 59.5¢ | 75.5¢ | 64.0¢ | 3 | -11.5¢ |
| Christian McCaffrey | SEA | Rushing | >69.5 | 46.5¢ | 36.0¢ | 53.5¢ | 37.0¢ | 23 | -9.5¢ |
| Derrick Henry | NE | Rushing | >69.5 | 60.5¢ | 57.5¢ | 61.0¢ | 60.5¢ | 128 | 0.0¢ |

## Final pregame ladders


Tee Higgins receiving yards vs CLE, using each contract's last quote before the
2026-01-04 18:00 UTC kickoff:

| Threshold | Midpoint | Bid / ask | Observation time |
|---:|---:|---:|---|
| >39.5 | 65.5¢ | 63¢ / 68¢ | 17:58 UTC |
| >49.5 | 53.0¢ | 51¢ / 55¢ | 17:56 UTC |
| >59.5 | 41.0¢ | 39¢ / 43¢ | 17:56 UTC |
| >69.5 | 31.5¢ | 30¢ / 33¢ | 17:59 UTC |
| >79.5 | 23.5¢ | 22¢ / 25¢ | 17:56 UTC |

Derrick Henry rushing yards vs PIT, using the last quote before the 2026-01-05
01:20 UTC kickoff: >79.5 62.0¢, >89.5 51.0¢, >99.5 41.0¢, >109.5 33.5¢,
and >119.5 30.5¢.

## What the records mean

Each market is a binary contract for one player, one game, one stat, and one
threshold. `>49.5` means YES settles at $1 if the player records at least 50 yards
under Kalshi's rules; otherwise it settles at $0. A 53¢ YES bid is the best visible
price a buyer offers. A 53¢ YES ask is the cheapest visible sell offer. The midpoint
is a research convenience and is not necessarily executable.

Kalshi published a median of five thresholds per player/game/prop; the observed
range was two to seven. Among 12,330 markets that had pregame quotes no wider than
10¢, the median early-to-final move was 1.5¢; 11.4% moved at least 5¢ and 1.55%
moved at least 10¢. The median tight-quote intragame range was 3¢. Across all valid
pregame observations, a typical market changed midpoint on roughly 17% of
successive observed candles.

For the top quartile of markets by final reported volume, the median observed
pregame spread was 4¢; 53.2% of quote observations were at most 5¢ wide and 81.5%
were at most 10¢ wide. The archive is usable for studying pregame timing, especially
on liquid central thresholds. Since the typical price move is smaller than the
typical spread, any buy/sell study must model entry at the ask and exit at the bid,
not midpoint-to-midpoint returns.

## Limits and next step

Candles are activity-dependent rather than a guaranteed contiguous minute grid.
The first quotes are often extremely wide, trade prices are missing when no trade
occurred, kickoff is scheduled kickoff rather than first snap, and approximately
3.3% of market rows lack a normalized nflverse player match. Adjacent thresholds
are correlated observations of the same player-game distribution.

Before building ML, define an executable timing target on the liquid subset—for
example, whether buying at the current YES ask can later exit at a pregame YES bid
at least 5¢ higher—and inspect that target with `show_prop.py` across several weeks.
