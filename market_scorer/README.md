# Market scorer

Everything for historical ranking, markout models, live shadow scoring, and the
small replay UI lives here. Source data stays read-only under `data/`; generated
models and shadow snapshots go under the git-ignored `model_output/`.

Rank or look up historical opportunities:

```bash
python market_scorer/rank.py --top 10
python market_scorer/rank.py --latest-only --player "Nico Collins" \
  --prop-type receiving_yards --threshold 79.5
```

Train/evaluate the frozen DAL-NYG holdout model, or freeze v2 on all preserved
Sunday snapshots:

```bash
python market_scorer/model.py
python market_scorer/train_v2.py
```

Run the read-only hybrid against an upcoming game:

```bash
python market_scorer/live.py --game DET_BUF --date 2026-09-17
```

The hybrid never averages the rule and model. A `strong` or `enter` rule signal
wins; otherwise a positive model prediction can promote the shadow result only
to `watch`. Initial REST snapshots have no Bovada repricing history, so their
rule tier is limited to `watch` or `pass` until a change event is observed.
Matching is exact by normalized player, prop type, and threshold. No orders are
submitted.

Launch the local UI:

```bash
streamlit run market_scorer/app.py
```

## Combo/RFQ scoring scaffold

`combo.py` accepts a JSON snapshot with a combo YES price and selected-side leg
books, then reports component fair value, structural seller edge, the frozen
historical edge prior, quote/tail uncertainty, and `consider`/`watch`/`pass`.
`consider` still means research shortlist, not an order signal.

```bash
python market_scorer/combo.py --input combo_snapshot.json
```

Each `leg_quotes` entry needs `game`, `bid`, `ask`, and `age_seconds`. The scorer
uses component products only when every leg is from a distinct game; otherwise
it requires a combo book midpoint or returns no fair value. Component premium
does not affect selection because it failed the settled-slate validation.

On the current quote-quality subset, the frozen price rule retained 899 combos
at +3.65¢ equal-combo / +2.93¢ volume-weighted net. Operational quote and
independence checks retained 885 at +3.76¢ / +2.93¢—effectively the same signal,
not evidence of model lift.

Evaluate the unchanged scorer on one or more future settled feature files:

```bash
python scripts/evaluate_combo_scorer.py --input path/to/quality_fill_values.parquet
```

On future slates, keep the rule frozen and track component/market-price Brier
scores, realized equal-combo and volume-weighted edge, loss tails, and how much
the quote-quality checks change coverage. Refit only after there are multiple
chronologically separate slates for a real forward holdout.
