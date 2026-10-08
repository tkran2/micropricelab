# Initial real-data findings — exploratory workflow check

**The data do not establish a stable forecasting advantage or an executable strategy.** The snapshot microprice proxy barely improved MAE on the dense grid, slightly worsened RMSE, and the fitted model failed under wider-spread test conditions.

## Coverage

- live-baseline.sqlite: 505 trusted publications spanning 25.670 seconds of local publication records.
- capture-20261008.sqlite: 9,514 trusted publications spanning 594.030 seconds of local publication records.

Training uses October 7 only: **226 retained 100ms-grid anchors**. Evaluation uses October 8: **4,188 retained anchors** from the new ten-minute capture. These are short observations on two calendar dates, with only one evaluation date. The later capture reported zero sequence gaps and zero reconnects. Research publication-gap fences are a separate conservative filter; silence is not proved feed loss.

## Forecast errors

Errors are USD per one-second midpoint forecast. Labels describe the locally published book, not independently timestamped exchange ground truth.

| Model | MAE | RMSE | Bias |
|---|---:|---:|---:|
| persistence | 4.258388 | 6.669626 | -0.049248 |
| microprice | 4.254996 | 6.670028 | -0.060704 |
| ridge | 7.984656 | 18.492531 | -0.547540 |

Microprice MAE improvement: **$0.003392**, or **0.080%**. The RMSE ordering does not favor microprice. No confidence interval is issued: there is only one evaluation day, and the overlapping labels do not provide independent replication.

With a predeclared one-second grid instead of a 100ms grid, evaluation has **441 anchors**. Persistence/microprice/ridge MAE are **4.005658 / 3.993216 / 8.385410**. Microprice improves both MAE and RMSE in that sensitivity, but the change in magnitude/ordering reinforces the need for more observations. The sparse-grid fitted model also performs poorly. This sensitivity is not used to select a winning model.

## Why the fitted model failed

The earlier capture represents a tiny, mostly unchanged, one-cent-spread period. In training, micro-minus-mid and imbalance are nearly redundant because the spread is constant; ridge shares their weights. During the later capture the spread sometimes widens, changing the relation between those features. A coefficient fitted to very small microprice deviations then extrapolates to much larger values.

- `micro_minus_mid`: training range [-0.00499786, 0.00493228]; 32.33% of evaluation rows lie outside it.
- `imbalance`: training range [-0.99957277, 0.98645507]; 14.95% of evaluation rows lie outside it.
- `past_1s_return`: training range [-2.14000000, 1.61000000]; 57.35% of evaluation rows lie outside it.
- `spread`: training range [0.01000000, 0.01000000]; 19.36% of evaluation rows lie outside it.

The `wide/high_past_move` group has 810 retained observations: persistence MAE is about $4.44, versus about $23.58 for ridge. The `wide/low_past_move` group has **only one observation**, so its apparent model ordering is not meaningful. All condition definitions use earlier-day thresholds. Full counts and errors are in `by_market_condition.csv`.

These diagnostics are consistent with training-support shift and redundant features; they do not prove a unique cause. More dates and broader training coverage are the next experiment. Feature clipping or a changed model should be declared as a new candidate and tested on a subsequent untouched period, rather than retrospectively improving this test.

## Selection and execution limits

Freshness/gap exclusions can also remove healthy quiet periods. The reported errors are conditional on retained recent publications, with possible activity-selection bias. A heartbeat-aware validity policy is a worthwhile future sensitivity, not something silently changed to improve this report.

No buy/sell rule or fill model is tested. A small error improvement says nothing by itself about paying the spread, fees, queue priority, latency, impact or inventory risk. Coinbase spot BTC-USD evidence also cannot be generalized to equities/futures or other venues from these observations.

## Reproduce

```bash
python3 -m micropricelab study data/observations/20261007.csv data/observations/20261008.csv --output reports/reproduced-real
python3 -m micropricelab study data/observations/20261007.csv data/observations/20261008.csv --step-ms 1000 --output reports/reproduced-sensitivity
```

The archive contains original journals, hash-verified observation imports, fingerprints, fold coefficients, individual forecasts, by-day/condition tables, and the plotted errors. Floating-point details can vary slightly with NumPy/BLAS/platform; do not interpret differences near rounding precision as economic evidence.
