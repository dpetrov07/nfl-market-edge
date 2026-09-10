# 2025 NFL player-prop historical data spike

Run completed September 9, 2026. Requested window: September 1, 2025–February 10, 2026.

**Recommendation: proceed with a limited research dataset, using Kalshi as the primary foundation and Polymarket as a secondary comparison.** Both provide usable historical paths. Kalshi has substantially broader game/threshold coverage and historical bid/ask, volume, and open interest. The downloaded Kalshi history is a sample, not a completed season backfill. Nothing here establishes a profitable timing strategy.

**1. What changed**

- Modified the existing `scripts/build_2025_prop_history.py`: restricted discovery to verified individual-game series/types, corrected Polymarket pagination and archive queries, matched games to nflverse kickoff, normalized Kalshi dollar prices without losing zeros, fixed the observed 5,000-minute Kalshi request limit, and added resumable per-market files and request audits.
- Added `scripts/report_coverage.py` for measured coverage, a small player/game join, source-quality flags, and representative examples. It normalizes punctuation/suffixes plus Hollywood Brown → Marquise Brown and Cameron Ward → Cam Ward; no fuzzy matches or fabricated zero outcomes.
- Updated README/run commands and saved the installed package versions in `requirements.lock.txt`. No model, full feature pipeline, infrastructure, or broad test suite was added.

**2. What successfully downloaded**

Original market/event metadata and compressed API histories, consolidated Parquet histories, seven nflverse datasets, per-market coverage CSV/Parquet, join results, and a machine-readable coverage summary are in `data/raw/` (about 303 MB including checkpoints and original responses).

Kalshi: all pages of the three target historical series were enumerated. A 400-market sample spread over prop types and kickoff dates was completed, plus 221 earlier exploratory markets concentrated in the playoffs. All 621 queried markets now have history and no remaining request errors. The other 14,272 discovered contracts were not queried for candles during this spike. The full backfill was curtailed after rate limits; successful work was retained.

Polymarket: 477 closed NFL-tagged events were enumerated; all 1,328 relevant markets and both tokens were queried. Five histories were empty, including four markets lacking opening metadata; creation timestamps were used only as query bounds for those four, leaving their opening times null.

| nflverse dataset | Rows |
|---|---:|
| ngs_receiving | 1,402 |
| ngs_rushing | 648 |
| pfr_receiving | 4,533 |
| pfr_rushing | 2,355 |
| player_stats | 19,422 |
| schedules | 285 |
| snap_counts | 26,612 |

Player stats include GSIS IDs, game/week, team/opponent, receptions, targets, receiving yards, carries, rushing yards, EPA and air-yard fields. Snap files include offense snaps/share and PFR IDs. NGS includes rushing efficiency/expected yards and receiving separation/cushion/YAC metrics; weekly PFR advanced rushing/receiving also downloaded.

**3. Dataset statistics**

| Coverage | Kalshi | Polymarket |
|---|---:|---:|
| Receiving-yard contracts | 10,254 | 831 |
| Rushing-yard contracts | 4,639 | 497 |
| Rushing-attempt contracts | 0 | 0 |
| Total relevant contracts | 14,893 | 1,328 |
| Unique normalized players | 330 | 279 |
| Unique NFL games | 205 | 92 |
| Unique player-game-stat props, ignoring thresholds | 2,931 | 1,233 |
| Markets queried for history | 621 | 1,328 |
| Markets with usable history | 621 | 1,323 |
| Markets with pregame reference prices | 621 | 1,296 |
| Games with downloaded history | 201 | 92 |
| Players with downloaded history | 215 | 279 |
| Player-game-stat props with downloaded history | 449 | 1,229 |
| Timestamped observations | 409,989 | 13,592,889 |
| Pregame reference-price observations | 345,059 | 5,454,497 |

Kalshi game coverage begins **October 6, 2025 Eastern**; Polymarket target props begin **November 30, 2025**. Both reach the February 8 Super Bowl. No relevant September game contracts were returned by these discovery paths. nflverse has 285 regular/postseason games.

Across platforms there are **3,054 unique player-game-stat props** and **15,826 unique player-game-stat-threshold combinations** after normalized-name deduplication. The histories actually saved cover **1,494 unique player-game-stat props**, of which **1,472 have pregame observations**. Alternate thresholds and opposite outcome tokens are correlated observations, not independent games.

| History quality | Kalshi: all 621 saved markets | Polymarket |
|---|---:|---:|
| Median observations per queried market | 249 | 9,802, both tokens |
| Median observations per market with history | 249 | 9,808, both tokens; 4,904 Over |
| Median pregame reference observations | 149 | 3,882 Over |
| Requested resolution | 1-minute candles | 1-minute fidelity |
| Median within-market timestamp spacing | 120 seconds | 60 seconds |
| Median pregame history span | 47.48 hours | 64.68 hours |
| Median first observation before kickoff | 47.52 hours | 64.70 hours |
| 10th–90th percentile of first-observation lead | 4.48–162.68 hours | 21.23–124.88 hours |
| Median last pregame observation before kickoff | 1 minute | 0.85 minutes |
| Median latest saved observation after kickoff | 3.20 hours | 13.17 hours |
| Historical bid/ask | YES bid/ask OHLC | None retrieved |
| Historical traded price | OHLC/mean; 41,910 non-null trade candles | Sampled market price; not a trade tape |
| Historical volume | Per candle; also market totals | Market total only in this pull |
| Historical open interest | Per candle | None retrieved |

Time/price quality medians use markets with relevant observations; pregame metrics use strictly timestamps before scheduled kickoff. Kalshi reference prices are **derived closing bid/ask midpoints**, not executed trades. Polymarket reference metrics use the Over token only; the total row count includes Over and Under. Kalshi candles preserve exact interval endpoints, and Polymarket preserves the returned timestamp seconds; neither provides every tick or every order-book change.

The 400-market Kalshi sample alone has 106,020 observations, median 220 observations/market, median 180-second spacing, and median first observation 44.63 hours before kickoff. The additional playoff pilot biases pooled quality statistics upward; do not extrapolate the pooled medians to every discovered market.

**4. Problems and limitations**

- Kalshi backfill remains partial. The archive imposed HTTP 429 limits and rejected windows longer than 5,000 minutes; three-day chunks repaired all sampled failures. Resume the remaining archive with modest concurrency.
- Liquidity matters more than row count. Of the saved Kalshi markets, 502 have a pregame trade candle, 512 ever have a pregame spread of 10 cents or less, and the median per-market pregame spread is 7 cents. These are not guarantees of executable depth. There are 29,889 pregame trade candles. The sparse early Brenton Strange example below has no pregame trades and a 60.5-cent median spread.
- Polymarket is dense but often repetitive: 1,276 markets have changing pregame prices, yet only 278 have at least 10 distinct pregame prices, versus 429/621 for the Kalshi sample/pilot. Sampled market prices alone cannot establish achievable entry/exit prices. A representative `orderbook-history` probe returned HTTP 200 with zero rows; an OHLC probe returned 404. These limited probes do not establish universal unavailability.
- Sixteen Polymarket contracts have published thresholds above 150 yards and are flagged for review, including Jacoby Brissett rushing O/U 282.5. The title, type and rules all say rushing; no relabeling was made. A quick check found no binary settlement/stat disagreements among joined, comparable contracts on either platform, which does not make every listed contract useful for timing research.
- Player/game joins found actual-stat rows for 14,396/14,893 Kalshi contracts (96.66%) and 1,283/1,328 Polymarket contracts (96.61%). All market games matched schedules. Remaining player-game matches need participation/roster/name review; a missing stats row was never converted to zero.
- NFL kickoff is the schedule reference, not verified first-snap time. NGS includes week-0 season summaries and labels the Super Bowl week 23, whereas schedules/player stats use week 22. Exclude summaries and align postseason weeks before joining features.
- Participation settlement rules differ: sampled Kalshi rules can settle a player with no snaps to a pregame fair price, while sampled Polymarket rules resolve an inactive/nonparticipant to Under. Retain raw rule text and settlement policy when comparing platforms.
- Still missing for sportsbook comparison: timestamped DraftKings/FanDuel Over/Under lines and odds, alternate-line ladders, suspensions, limits, book-specific settlement/void rules, fees/vig, and actual executable availability. Injuries, inactive announcements, lineup/news timestamps and weather histories would also help explain movements.

**5. Actual example markets**

All times below are UTC. The observation column is total reference observations / pregame reference observations; for Polymarket this counts **Over only**. Prices are dollar probabilities. “First” is the earliest saved reference price; min/max are pregame reference prices, not intraminute trade extremes.

**Kalshi — derived YES bid/ask midpoint**

| Player; prop; threshold | Game ID | Open UTC | Kickoff UTC | Obs total / pregame | First | Last pregame | Pregame min–max |
|---|---|---|---|---:|---:|---:|---:|
| Brenton Strange; receiving; >19.5 | 2025_05_KC_JAX | 2025-10-06 21:53 | 2025-10-07 00:15 | 20 / 6 | 0.495 | 0.545 | 0.495–0.640 |
| Tee Higgins; receiving; >49.5 | 2025_18_CLE_CIN | 2026-01-02 11:15 | 2026-01-04 18:00 | 315 / 178 | 0.495 | 0.530 | 0.495–0.560 |
| Jake Bobo; receiving; >29.5 | 2025_22_SEA_NE | 2026-02-06 21:27 | 2026-02-08 23:30 | 988 / 945 | 0.495 | 0.390 | 0.040–0.495 |
| Travis Etienne Jr.; rushing; >89.5 | 2025_05_KC_JAX | 2025-10-06 21:53 | 2025-10-07 00:15 | 200 / 49 | 0.495 | 0.205 | 0.160–0.495 |
| Brian Robinson Jr.; rushing; >19.5 | 2025_13_SF_CLE | 2025-11-28 23:10 | 2025-11-30 18:00 | 264 / 131 | 0.460 | 0.580 | 0.305–0.580 |
| Sam Darnold; rushing; >29.5 | 2025_22_SEA_NE | 2026-01-26 03:36 | 2026-02-08 23:30 | 2,326 / 2,172 | 0.460 | 0.185 | 0.035–0.460 |

**Polymarket — returned Over-token price**

| Player; prop; threshold | Game ID | Open UTC | Kickoff UTC | Obs total / pregame | First | Last pregame | Pregame min–max |
|---|---|---|---|---:|---:|---:|---:|
| Puka Nacua; receiving; >88.5 | 2025_13_LA_CAR | 2025-11-29 20:05 | 2025-11-30 18:00 | 1,937 / 1,299 | 0.500 | 0.500 | 0.490–0.550 |
| Jahmyr Gibbs; receiving; >39.5 | 2025_16_PIT_DET | 2025-12-17 13:16 | 2025-12-21 21:25 | 7,060 / 6,233 | 0.500 | 0.495 | 0.490–0.510 |
| Rhamondre Stevenson; receiving; >20.5 | 2025_22_SEA_NE | 2026-01-26 12:16 | 2026-02-08 23:30 | 21,011 / 19,373 | 0.500 | 0.640 | 0.500–0.755 |
| Kyren Williams; rushing; >61.5 | 2025_13_LA_CAR | 2025-11-29 20:05 | 2025-11-30 18:00 | 1,934 / 1,299 | 0.500 | 0.500 | 0.100–0.515 |
| Bryce Young; rushing; >12.5 | 2025_16_TB_CAR | 2025-12-18 15:16 | 2025-12-21 18:00 | 5,283 / 4,468 | 0.500 | 0.505 | 0.495–0.515 |
| AJ Barner; rushing; >0.5 | 2025_22_SEA_NE | 2026-01-27 01:16 | 2026-02-08 23:30 | 20,240 / 18,593 | 0.500 | 0.465 | 0.310–0.760 |

Exact source market/token IDs are retained in `coverage_summary.json`, market Parquet files and per-market coverage CSVs. Kalshi >19.5 corresponds to its displayed 20+ contract.

**6. Recommendation and next canonical dataset**

**Kalshi is sufficient for a meaningful pilot**, and its discovered universe of 2,931 player-game-stat props is a reasonable basis for a larger movement experiment after the remaining backfill and liquidity filters. The saved 449 independent player-game-stat props are enough to prototype analysis, not demonstrate durable returns. **Polymarket is also sufficient for exploratory descriptive work** with 1,229 downloaded independent props, but late-season coverage, repeated prices, and missing historical execution fields make it a weaker primary source for market-timing backtests.

Focus initially on **receiving and rushing yards**. No rushing-attempt market was returned by either discovery path; the Kalshi rushing-attempt series exists but its archive returned zero markets. Do not assume that stat is represented just because nflverse has carries.

The next canonical table should retain `(platform, market_id, outcome, player_id, game_id, prop_type, threshold, timestamp)` as its key, with a separate cross-platform proposition key. Include source provenance, kickoff, hours-to-kickoff, current price and price type, bid/ask/spread where observed, quote age, observation gaps, volume and open interest where observed; then lagged 1h/6h/24h changes, momentum, and volatility. Keep pregame future maximum/minimum and closing price as **labels**, alongside actual stat and participation/settlement policy. Features may use prior 3/5-game targets, carries, receptions, snap share, and relevant NGS/PFR metrics available before that timestamp. Never use game-final volume, current-game performance, season aggregates, or future outcomes as contemporaneous inputs. Evaluate on chronological held-out games/weeks, keeping related thresholds and both platforms for the same game together.

**7. Single next engineering task**

**Finish the remaining Kalshi minute-candle backfill using the corrected resumable pull, and regenerate coverage.** This expands the strongest source from a verified sample to the discovered 14,893-contract universe before full feature engineering.

```bash
.venv/bin/python scripts/build_2025_prop_history.py --platform kalshi --workers 2 --sleep 0.6
.venv/bin/python scripts/report_coverage.py
```

Validation completed: both consolidated Parquet row counts match their request audits; discovered market IDs are unique; all discovered game IDs match nflverse schedules; downloaded prices are within [0, 1]; all final selected requests completed without errors. Empty successful responses remain explicitly distinguishable from unqueried markets.

**Source references**

The numerical results above are computed from the saved public API responses. API interpretation was checked against [Kalshi historical routing](https://docs.kalshi.com/getting_started/historical_data), [historical market discovery](https://docs.kalshi.com/api-reference/historical/get-historical-markets), [historical candles](https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks), [Polymarket price history](https://docs.polymarket.com/api-reference/markets/get-prices-history), [Polymarket events](https://docs.polymarket.com/api-reference/events/list-events), and the [nflverse schedule dictionary](https://nflreadr.nflverse.com/articles/dictionary_schedules.html). Live response details and failed probe evidence are retained locally.
