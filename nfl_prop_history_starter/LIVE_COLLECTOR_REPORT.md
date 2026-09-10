# Live Kalshi collection result: NE at SEA

The local collector is no longer running. It saved 86 one-minute snapshots from
2026-09-10 00:12:25 UTC through 03:32:59 UTC, spanning the final eight minutes
before the official 00:20 UTC (8:20 PM ET) kickoff and portions of the game
through a postgame snapshot.

- 12,900 normalized market rows
- 150 player/prop/threshold contracts across 16 players
- 97 receiving-yard markets across 13 players
- 53 rushing-yard markets across 6 players
- 8 pregame snapshots (1,200 rows)
- 78 in-game or postgame snapshots (11,700 rows)
- Storage: `data/live/2026-09-09_NE_SEA/`

The Mac sleeping interrupted collection. The largest missing interval was about
60.5 minutes immediately after kickoff. Other material gaps were about 17, 16,
and 15 minutes, plus several four-minute gaps. From the first saved timestamp to
the last, the collector captured about 43% of the possible one-minute snapshots.
It resumed after sleep and did capture the end of the game, but this is not a
complete minute-by-minute game record.

The original run interpreted Kalshi's `occurrence_datetime` as kickoff, but that
field was three hours later than the official scheduled kickoff. The raw market
timestamps and prices were unaffected. All 86 saved snapshot files have been
corrected to the official 00:20 UTC kickoff, and `hours_to_kickoff`,
`minutes_to_kickoff`, and `is_pregame` were recalculated. Future runs support an
explicit authoritative kickoff:

```bash
.venv/bin/python scripts/collect_live_props.py \
  --game NE_SEA \
  --kickoff 2026-09-10T00:20:00Z
```

## Pregame examples

The available pregame window is short, so most contracts moved little between
the first snapshot at 00:12:25 UTC and the last at 00:19:46 UTC.

| Player | Prop | Threshold | First mid | Pregame low | Pregame high | Final bid/ask | Final mid | Final volume |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Jadarian Price | Rushing | >49.5 | 54.0c | 52.5c | 54.0c | 52c / 53c | 52.5c | 112,847.35 |
| A.J. Brown | Receiving | >59.5 | 54.5c | 54.5c | 54.5c | 54c / 55c | 54.5c | 95,548.17 |
| Rhamondre Stevenson | Receiving | >24.5 | 48.5c | 47.0c | 48.5c | 46c / 48c | 47.0c | 80,171.86 |
| AJ Barner | Receiving | >24.5 | 50.5c | 50.5c | 50.5c | 50c / 51c | 50.5c | 74,073.98 |
| Rhamondre Stevenson | Rushing | >59.5 | 46.5c | 45.0c | 46.5c | 44c / 46c | 45.0c | 63,650.49 |
| Drake Maye | Rushing | >24.5 | 52.5c | 52.5c | 52.5c | 52c / 53c | 52.5c | 53,930.79 |

## Example threshold ladder

A.J. Brown receiving yards at 00:13:55 UTC:

| Threshold | YES bid | YES ask | Midpoint |
|---:|---:|---:|---:|
| >39.5 | 72c | 73c | 72.5c |
| >49.5 | 65c | 66c | 65.5c |
| >59.5 | 54c | 55c | 54.5c |
| >69.5 | 44c | 45c | 44.5c |
| >79.5 | 35c | 36c | 35.5c |
| >89.5 | 27c | 28c | 27.5c |
| >99.5 | 19c | 21c | 20.0c |
| >109.5 | 13c | 16c | 14.5c |
| >119.5 | 10c | 12c | 11.0c |
| >129.5 | 5c | 8c | 6.5c |
| >139.5 | 4c | 6c | 5.0c |

## Postgame evidence

The final 03:32:59 UTC snapshot contains outcome-like prices. For example,
A.J. Brown receiving >59.5 was 0c bid / 1c ask, Rhamondre Stevenson receiving
>24.5 was 99c / 100c, Jadarian Price rushing >49.5 was 99c / 100c, and
Rhamondre Stevenson rushing >59.5 was 0c / 1c. These show the market's apparent
outcomes near game end; they are not official settlement records or exact player
yardage totals.

## Limits

This feed is a top-of-book snapshot series, not a complete order-event or trade
tape. Volume and open interest are cumulative market fields. Exact final player
yardages were not collected and still need to be joined from the NFL results
source. The local collector cannot record while the Mac is asleep or offline.
