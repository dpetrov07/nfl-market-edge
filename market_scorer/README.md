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
