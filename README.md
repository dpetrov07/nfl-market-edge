# NFL Market Edge

Research and collection tools for Kalshi NFL combo markets. The current work
asks a narrow question: did selling very cheap cross-game YES combos show an
edge on the settled September 13, 2026 slate? Nothing in this repo submits
orders, and one slate is not enough to establish a live strategy.

## What the research found

A **cross-game combo** joins player props from different games; every leg must
win for the combo to settle YES. Results are viewed from the YES seller's side:
the seller keeps the sale price when the combo loses, but owes $1 when it wins.
All reported profit figures include the existing fee estimate.

The clearest result was in cross-game combos sold below 10¢:

- 4,864 unique combos averaged **+1.77¢ per contract** when every combo counted
  once, or **+1.19¢** when weighted by the number of contracts traded.
- 4,705 combos made about **+4.96¢** each, while 159 losing combos lost about
  **92.70¢** each. The average was positive, but the occasional loss was roughly
  19 times a typical win.
- The 100 most-traded combos supplied 57.1% of all volume, so the
  volume-weighted result depended heavily on a small part of the slate.
- Reconstructing a combo's probability from its standalone legs did not improve
  prediction or produce a better selection rule.

## Reading the charts

![Cross-game prices and seller profit by price bucket](research/output/sunday_combo_2026-09-13/price-neighborhood.svg)

The top compares the price paid for each YES combo with the share that actually
settled YES. The bottom shows the seller's average profit. The result was not
unique to the under-10¢ group, but that group is the frozen candidate rule used
for prospective evaluation.

![Seller profit and downside by combo type](research/output/sunday_combo_2026-09-13/structure-breakouts.svg)

The lower panel describes the **worst 5% of outcomes**. For example, -74¢ means 
only about 5% of that combo type finished worse than a 74¢ loss per contract. 
This is the downside hidden by a positive average.

![Risk and concentration of the under-10-cent group](research/output/sunday_combo_2026-09-13/candidate-risk-exposure.svg)

This makes the asymmetry explicit: most positions earned a few cents, while a
small number lost nearly the full dollar. It also shows how often the same legs
and games appeared across combos, so these were overlapping bets rather than
4,864 independent observations.

![Calibration of combo prices and standalone-leg estimates](research/output/sunday_combo_2026-09-13/fair_value/component-calibration.svg)

The horizontal axis is the predicted chance of a YES settlement; the vertical 
axis is the share that actually settled YES. Points near the diagonal are better. 
A Brier score is the average squared prediction error, so lower is better: 0.13967 
for the combo price versus 0.14032 for the standalone-leg estimate. Those scores 
are nearly identical, with the combo price slightly better on this sample.

![Standalone-leg pricing versus seller profit](research/output/sunday_combo_2026-09-13/fair_value/premium-signal.svg)

This groups combos by how far their traded price sat above or below the
probability implied by their standalone legs. Seller profit does not improve
consistently as that gap grows, so the leg-price comparison did not add a useful
signal beyond the combo's own price.

These are descriptive findings from one settled Sunday with many correlated
positions—not an out-of-sample strategy test. Full methodology, caveats, and
reproduction commands are in [`research/sep13/README.md`](research/sep13/README.md).

## Prospective workflow

Create an environment and install dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Copy [`config/combo_slate.example.json`](config/combo_slate.example.json), set
the slate ID and Kalshi event tickers, then start the Kalshi, Bovada, FanDuel,
and BetRivers collectors:

```bash
cp config/combo_slate.example.json config/combo_slate.json
.venv/bin/python -m scripts.collect_combo_slate \
  --manifest config/combo_slate.json
```

Keep Kalshi running through settlement, then evaluate the captured slate:

```bash
.venv/bin/python -m scripts.build_shadow_pricing \
  --manifest config/combo_slate.json

.venv/bin/python -m scripts.evaluate_prospective_combo_slate \
  --manifest config/combo_slate.json
```

Captures land under `data/live/combo_slates/<slate_id>/`; evaluation results go
to `research/output/<slate_id>/prospective/`. The shadow-pricing command can run
before settlement and writes CSV/Parquet decisions plus a summary under
`research/output/<slate_id>/shadow/`. Each row is one cross-game RFQ joined only
to prices received by that timestamp. Exact two-way game and player lines are
preferred and de-vigged. Complete first-TD markets are normalized across all
outcomes; one-way anytime-TD and alternate player lines carry an extra
uncertainty buffer. When an exact threshold is unavailable, nearby surrounding
alternate lines may be interpolated with a further buffer. Per-book estimates
are combined by median, multiplied across legs, widened for disagreement and
freshness, and converted to a whole-cent YES sale price covering fees and a 1¢
minimum edge. It requires two books per leg by default and never submits a
quote. Re-running it
after settlement adds hypothetical P&L and keeps the frozen `<10¢` rule as a
separate comparison.

The decision file includes the per-book inputs in `leg_pricing_json`, explicit
skip reasons, market and observed trade prices, 10/30/60-second markouts when
the capture covers those horizons, and settlement P&L. The frozen shortlist
remains the historical rule—cross-game YES trades strictly below 10¢—rather
than a calibrated model or order signal.

The independent sportsbook-to-Kalshi player-prop scorer and Streamlit UI remain
documented in [`market_scorer/`](market_scorer/README.md). Railway worker configs
are in `railway.*.toml`, with environment examples in
[`config/railway.env.example`](config/railway.env.example).

## Repo map

- `research/sep13/`: reproducible historical analysis and methodology
- `research/output/`: benchmark results and future slate evaluations
- `nfl_market_edge/`: frozen scoring and shared collection code
- `scripts/`: collectors, slate runner, and evaluators
- `market_scorer/`: separate player-prop scorer and UI
- `config/`: local and Railway configuration examples
