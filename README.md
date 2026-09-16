# NFL Market Edge

This repo captures prospective Kalshi combo slates and sportsbook player props,
then evaluates settled cross-game combos with the frozen Sep 13 scorer. The
current workflow is collection first, evaluation after settlement; nothing here
submits orders.

## Current workflow

Create an environment and install the direct dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Copy [`config/combo_slate.example.json`](config/combo_slate.example.json), set
the slate ID and exact Kalshi event tickers, then launch the configured Kalshi
and sportsbook collectors together:

```bash
cp config/combo_slate.example.json config/combo_slate.json
.venv/bin/python -m scripts.collect_combo_slate \
  --manifest config/combo_slate.json
```

The local runner starts four independent workers: Kalshi, Bovada, FanDuel, and
BetRivers. The capture lands under `data/live/combo_slates/<slate_id>/`. Keep
Kalshi running through settlement so the evaluator sees lifecycle results. Then
run:

```bash
.venv/bin/python -m scripts.evaluate_prospective_combo_slate \
  --manifest config/combo_slate.json
```

Evaluation writes matched fill features, scorer decisions, and the fixed-rule
summary to `research/output/<slate_id>/prospective/`.

## Sportsbook scorer side branch

The Bovada-to-Kalshi player-prop scorer and Streamlit UI are still preserved in
[`market_scorer/`](market_scorer/README.md) for continued work. They are
independent of the combo scorer and remain read-only.

```bash
# Historical rankings
.venv/bin/python market_scorer/rank.py --latest-only --top 10

# Live shadow score
.venv/bin/python market_scorer/live.py --game DET_BUF --date 2026-09-17

# Live/replay UI
.venv/bin/streamlit run market_scorer/app.py
```

The UI uses the frozen models under the git-ignored `model_output/`. Rebuild
them from the preserved Sep 13 data with:

```bash
.venv/bin/python market_scorer/model.py
.venv/bin/python market_scorer/train_v2.py
```

## Frozen combo scorer

[`nfl_market_edge/combo.py`](nfl_market_edge/combo.py) is the reusable scorer.
Its shortlist remains exactly the settled Sep 13 rule: cross-game YES trades
strictly below 10¢. Component or combo books affect the fair-value diagnostic
and operational `consider`/`watch` label, but do not replace that historical
rule.

Score one JSON snapshot from a file or stdin:

```bash
.venv/bin/python -m nfl_market_edge.combo --input combo_snapshot.json
```

The historical prior is intentionally low-confidence and is not a calibrated
model or an order signal.

## Collectors

The prospective pipeline is deliberately narrow:

- Kalshi records cross-game 2/3-leg combo discovery, RFQs/quotes, combo fills,
  combo books, component books, and lifecycle/settlement. It does not subscribe
  to standalone component trades.
- Bovada, FanDuel, and BetRivers each run independently and write the same
  per-selection sportsbook schema with local receive time, source time when
  available, a shared wall-clock snapshot bucket, slate/game IDs,
  player/prop/line/side, odds, and source IDs.

Individual worker entry points:

```bash
# Kalshi combo/RFQ slate capture
.venv/bin/python -m scripts.collect_live_combo_slate --help

# One normalized sportsbook source per process
.venv/bin/python -m scripts.collect_live_sportsbook_props --help
```

All sportsbook adapters emit the selection-state contract in
[`nfl_market_edge/sportsbook.py`](nfl_market_edge/sportsbook.py). Shared Kalshi
authentication and REST access live in
[`nfl_market_edge/kalshi.py`](nfl_market_edge/kalshi.py).

### Railway

Create four Railway worker services from this repo and point each service at its
config file:

| Service | Config | Start command |
|---|---|---|
| Kalshi | `railway.kalshi.toml` | `scripts.collect_live_combo_slate` |
| Bovada | `railway.bovada.toml` | sportsbook worker with `--book bovada` |
| FanDuel | `railway.fanduel.toml` | sportsbook worker with `--book fanduel` |
| BetRivers | `railway.betrivers.toml` | sportsbook worker with `--book betrivers` |

Attach a persistent volume to each service. Kalshi needs
`SLATE_MANIFEST_JSON`, `KALSHI_API_KEY_ID`, and a private-key variable. Each
sportsbook needs `SLATE_ID`, `SPORTSBOOK_SPORT=nfl|ncaaf`, and comma-separated
`SPORTSBOOK_GAMES`. See
[`config/railway.env.example`](config/railway.env.example).

Each worker emits structured `collector_status` JSON to Railway logs and keeps
its latest status in `health.json`. `ok` means props were collected, `empty`
means the source responded but has not posted matching props, `partial` means
some games failed, and `error` means the poll failed. Kalshi reports `starting`,
`ready`, `connected`, periodic `heartbeat`, reconnects, sequence gaps, and
lifecycle/settlement messages. Its heartbeat also reports whether authenticated
fill access is available and how many matching account fills were retained.

The common layout is:

```text
combo_slates/<slate_id>/
  kalshi/events.jsonl.gz
  kalshi/health.json
  sportsbooks/bovada/{nfl|ncaaf}_props_<utc-date>.jsonl.gz
  sportsbooks/bovada/health.json
  sportsbooks/fanduel/...
  sportsbooks/betrivers/...
```

Manifest event tickers are matched by their dated game suffix, so one event
ticker per game is enough even when component legs come from different Kalshi
series. Optional `combo_tickers` seed already-open combos after a restart; new
ones are discovered from RFQs and multivariate lifecycle messages.

## Repo map

- `nfl_market_edge/`: frozen scoring and shared collection primitives
- `market_scorer/`: sportsbook/Kalshi player-prop scorer and Streamlit UI
- `scripts/`: current collectors, slate runner, and evaluators
- `config/`: prospective slate manifest example
- `research/sep13/`: reproducible historical analysis code and findings
- `research/output/`: preserved benchmark results and future slate evaluations
- `data/`: local captures and derived Parquet; live captures are git-ignored
- `railway.*.toml`: collector service entry points

## Sep 13 benchmark

The preserved study remains the source of the frozen prior: 4,864 cross-game
combos below 10¢ returned +1.77¢ per equal-weighted combo and +1.19¢ when
weighted by observed volume, with a 3.27% losing-combo rate and severe loss
tails. Component midpoint products did not improve selection on that slate.

- Cross-game 2-leg and 3-leg combos returned +3.11¢ and +4.40¢ net per equal
  combo (+4.08¢ and +5.95¢ volume weighted).
- The `<10¢` group was positive for both 2-leg (+2.27¢ equal weighted) and
  3-leg (+1.60¢) combos, but 159 losing positions averaged -92.70¢.
- The top 100 combos supplied 57.1% of volume, so the slate is descriptive
  evidence rather than an out-of-sample strategy test.

![Cross-game price buckets around the candidate](research/output/sunday_combo_2026-09-13/price-neighborhood.svg)

![Seller edge and downside by combo structure](research/output/sunday_combo_2026-09-13/structure-breakouts.svg)

![Candidate tail risk and concentration](research/output/sunday_combo_2026-09-13/candidate-risk-exposure.svg)

The component fair-value follow-up retained 3,629 combos with fresh, complete
leg books. The component midpoint product had a 0.14032 Brier score versus
0.13967 for combo price, and component premium did not improve selection.

![Combo and component calibration](research/output/sunday_combo_2026-09-13/fair_value/component-calibration.svg)

![Component premium versus seller edge](research/output/sunday_combo_2026-09-13/fair_value/premium-signal.svg)

The full methodology, caveats, reproduction commands, and retained charts are
in [`research/sep13/README.md`](research/sep13/README.md).
