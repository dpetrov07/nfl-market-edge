# Sep 13, 2026 combo benchmark

This directory contains the historical pipeline retained to reproduce the
findings behind the frozen cross-game `<10¢` scorer. It is reference research,
not the day-to-day collection workflow.

## Retained findings

The settled sample contains 29,566 traded 2/3-leg combo markets, 182,951 fills,
and 54.51 million contracts. Seller return is the observed YES price minus
settlement, less the existing `7% * price * (1 - price)` fee estimate.

- The frozen cross-game `<10¢` group contains 4,864 combos and 22,016 fills.
  Equal-combo net was +1.77¢; observed-volume-weighted net was +1.19¢.
- 4,705 candidate positions gained 4.96¢ on average while 159 lost 92.70¢ on
  average. The worst result was -98.88¢.
- Component midpoint products were well calibrated in aggregate but slightly
  worse than combo price on Brier score. Component premium did not improve the
  price-only cross-validated model.

Canonical numeric results are in
[`../output/sunday_combo_2026-09-13/summary.json`](../output/sunday_combo_2026-09-13/summary.json)
and
[`../output/sunday_combo_2026-09-13/fair_value/summary.json`](../output/sunday_combo_2026-09-13/fair_value/summary.json).

## Reproduce

The local input snapshot is `data/sunday_2026-09-13/`. The important inputs are
the Kalshi raw/processed books and the `combos/` Parquet tables. The raw capture
is intentionally git-ignored because of its size; do not delete it if this
machine remains the reproduction source.

Run the stages from the repo root:

```bash
# Only needed when rebuilding processed Kalshi books from the raw capture.
.venv/bin/python -m research.sep13.preprocess

# Only needed when rebuilding the combo tables from Kalshi history.
.venv/bin/python -m research.sep13.discover_combos

.venv/bin/python -m research.sep13.build_economics
.venv/bin/python -m research.sep13.analyze_edges
.venv/bin/python -m research.sep13.analyze_fair_value
.venv/bin/python -m scripts.evaluate_combo_scorer
```

The rebuild overwrites derived files under `data/sunday_2026-09-13/combos/`
and `research/output/sunday_combo_2026-09-13/`. Discovery requires current
Kalshi API access; the later stages run from the preserved local Parquet inputs.

## Retained visuals

![Cross-game price buckets](../output/sunday_combo_2026-09-13/price-neighborhood.svg)

![Structure breakouts](../output/sunday_combo_2026-09-13/structure-breakouts.svg)

![Candidate tail risk](../output/sunday_combo_2026-09-13/candidate-risk-exposure.svg)

![Component calibration](../output/sunday_combo_2026-09-13/fair_value/component-calibration.svg)

![Component premium](../output/sunday_combo_2026-09-13/fair_value/premium-signal.svg)
