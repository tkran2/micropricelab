"""Predictions, causal sampling, expanding-day evaluation, and blocked uncertainty."""
from __future__ import annotations
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import csv
import html
import json
from pathlib import Path
import numpy as np
from .data import read_observations

SECOND = 1_000_000_000
DAY = 86_400 * SECOND
FEATURES = ("micro_minus_mid", "imbalance", "past_1s_return", "spread")

@dataclass(frozen=True)
class Config:
    horizon_ns: int = SECOND
    step_ns: int = SECOND // 10
    max_age_ns: int = SECOND // 4
    max_gap_ns: int = SECOND // 2
    train_days: int = 1
    ridge_alpha: float = 10.0
    block_ns: int = 60 * SECOND
    bootstrap_repeats: int = 1000
    seed: int = 8731

    def validate(self):
        if min(self.horizon_ns, self.step_ns, self.max_age_ns, self.max_gap_ns,
               self.train_days, self.block_ns, self.bootstrap_repeats) <= 0:
            raise ValueError("positive timing and count settings are required")
        if not np.isfinite(self.ridge_alpha) or self.ridge_alpha <= 0:
            raise ValueError("positive finite ridge penalty required")

def utc_day(timestamp):
    return datetime.fromtimestamp(timestamp / SECOND, timezone.utc).date().isoformat()

def sample(observations, config=Config()):
    """Causal as-of lookup on a regular grid, fenced by session, UTC day and gap."""
    config.validate()
    groups, reasons = defaultdict(list), defaultdict(int)
    for obs in observations:
        groups[(obs.segment, obs.available_ns // DAY)].append(obs)
    rows = []
    for (segment, _), group in sorted(groups.items(), key=lambda item: item[1][0].available_ns):
        group.sort(key=lambda o: o.available_ns)
        times = np.array([o.available_ns for o in group], dtype=np.int64)
        if np.any(np.diff(times) <= 0):
            raise ValueError("non-monotonic segment")
        # Each large inter-publication gap ends a usable span. A label may not bridge it.
        cuts = np.r_[0, np.flatnonzero(np.diff(times) > config.max_gap_ns) + 1, len(group)]
        reasons["gap_fences"] += len(cuts) - 2
        for start, end in zip(cuts[:-1], cuts[1:]):
            local = group[start:end]
            ts = times[start:end]
            if len(ts) < 2:
                reasons["short_spans"] += 1
                continue
            first = ((int(ts[0]) + SECOND + config.step_ns - 1) // config.step_ns) * config.step_ns
            anchors = np.arange(first, int(ts[-1]) + 1, config.step_ns, dtype=np.int64)
            targets = anchors + config.horizon_ns
            i = np.searchsorted(ts, anchors, side="right") - 1
            j = np.searchsorted(ts, targets, side="right") - 1
            k = np.searchsorted(ts, anchors - SECOND, side="right") - 1
            valid_boundary = targets <= ts[-1]
            reasons["horizon_outside_span"] += int((~valid_boundary).sum())
            safe_i, safe_j, safe_k = np.maximum(i, 0), np.maximum(j, 0), np.maximum(k, 0)
            age_ok = ((anchors - ts[safe_i] <= config.max_age_ns)
                      & (targets - ts[safe_j] <= config.max_age_ns)
                      & (anchors - SECOND - ts[safe_k] <= config.max_age_ns)
                      & (i >= 0) & (j >= 0) & (k >= 0))
            reasons["stale_anchor_label_or_lag"] += int((valid_boundary & ~age_ok).sum())
            bid = np.array([o.bid for o in local]); ask = np.array([o.ask for o in local])
            bsz = np.array([o.bid_size for o in local]); asz = np.array([o.ask_size for o in local])
            mids = (bid + ask) / 2
            spreads = np.round(ask - bid, 8)  # source uses 1e-8 fixed point; remove subtraction noise
            # Snapshot size-weighted microprice, not an estimated Markov-chain microprice.
            micro = (ask * bsz + bid * asz) / (bsz + asz)
            imbalance = (bsz - asz) / (bsz + asz)
            for a, target, ii, jj, kk in zip(anchors[valid_boundary & age_ok],
                    targets[valid_boundary & age_ok], i[valid_boundary & age_ok],
                    j[valid_boundary & age_ok], k[valid_boundary & age_ok]):
                past_return = float(np.round(mids[ii] - mids[kk], 8))
                rows.append({"anchor_ns": int(a), "target_ns": int(target),
                             "day": utc_day(int(a)), "segment": segment,
                             "mid": float(mids[ii]), "target_mid": float(mids[jj]),
                             "y": float(np.round(mids[jj] - mids[ii], 8)),
                             "micro_minus_mid": float(micro[ii] - mids[ii]),
                             "imbalance": float(imbalance[ii]),
                             "past_1s_return": float(past_return),
                             "spread": float(spreads[ii]),
                             "past_abs_return": float(abs(past_return)),
                             "anchor_quote_age_ns": int(a - ts[ii]),
                             "label_quote_age_ns": int(target - ts[jj])})
    rows.sort(key=lambda row: row["anchor_ns"])
    reasons["retained_samples"] = len(rows)
    return rows, dict(reasons)

def matrix(rows):
    return np.array([[row[key] for key in FEATURES] for row in rows], dtype=float)

class Ridge:
    def fit(self, rows, alpha):
        x = matrix(rows); y = np.array([row["y"] for row in rows])
        self.mean = x.mean(axis=0); self.scale = x.std(axis=0)
        self.scale[self.scale < 1e-12] = 1.0
        self.intercept = float(y.mean())
        z = (x - self.mean) / self.scale
        self.coef = np.linalg.solve(z.T @ z + alpha * np.eye(x.shape[1]),
                                    z.T @ (y - self.intercept))
        return self

    def predict(self, rows):
        return self.intercept + ((matrix(rows) - self.mean) / self.scale) @ self.coef

    def audit(self):
        return {"features": FEATURES, "training_mean": self.mean.tolist(),
                "training_scale": self.scale.tolist(), "standardized_coefficients": self.coef.tolist(),
                "intercept": self.intercept}

def errors(rows, model):
    e = np.array([row[model] - row["y"] for row in rows])
    return {"samples": len(rows), "mae": float(np.abs(e).mean()),
            "rmse": float(np.sqrt((e * e).mean())), "bias": float(e.mean())}

def blocked_improvement(predictions, model, config):
    days = defaultdict(lambda: defaultdict(list))
    for row in predictions:
        days[row["day"]][row["anchor_ns"] // config.block_ns].append(
            abs(row["y"]) - abs(row[model] - row["y"]))
    # No iid-row confidence interval for overlapping labels. Require multiple test days.
    if len(days) < 2 or any(len(blocks) < 5 for blocks in days.values()):
        return {"interval": None, "reason": "need at least 2 evaluation days, each with 5 time blocks; current data are exploratory"}
    stats = [np.array([(sum(block), len(block)) for block in blocks.values()], float)
             for _, blocks in sorted(days.items())]
    point = float(np.mean([arr[:, 0].sum() / arr[:, 1].sum() for arr in stats]))
    rng = np.random.default_rng(config.seed)
    values = []
    for _ in range(config.bootstrap_repeats):
        draw = []
        for index in rng.integers(0, len(stats), len(stats)):
            arr = stats[index]
            block_draw = arr[rng.integers(0, len(arr), len(arr))]
            draw.append(block_draw[:, 0].sum() / block_draw[:, 1].sum())
        values.append(float(np.mean(draw)))
    return {"equal_day_weighted_mae_improvement": point,
            "interval": np.quantile(values, [0.025, 0.975]).tolist(),
            "method": "hierarchical paired bootstrap: evaluation days, then fixed UTC time blocks",
            "block_seconds": config.block_ns / SECOND,
            "caution": "descriptive interval, not proof of independent days or a profitable strategy"}

def evaluate(observations, inputs=None, config=Config()):
    rows, exclusions = sample(observations, config)
    days = sorted({row["day"] for row in rows})
    if len(days) <= config.train_days:
        raise ValueError("need usable observations on more days than the initial training window")
    predictions, folds, metrics, conditions = [], [], [], []
    for day in days[config.train_days:]:
        cutoff = min(row["anchor_ns"] for row in rows if row["day"] == day) // DAY * DAY
        train = [row for row in rows if row["day"] < day and row["target_ns"] < cutoff]
        test = [row for row in rows if row["day"] == day and row["target_ns"] < cutoff + DAY]
        if len(train) < 20 or not test:
            raise ValueError(f"insufficient training/test samples on {day}")
        model = Ridge().fit(train, config.ridge_alpha)
        spread_q = float(np.quantile([r["spread"] for r in train], .75))
        move_q = float(np.quantile([r["past_abs_return"] for r in train], .75))
        ranges = {key: (min(r[key] for r in train), max(r[key] for r in train)) for key in FEATURES}
        fitted = model.predict(test)
        for row, prediction in zip(test, fitted):
            row = dict(row)
            row.update(persistence=0., microprice=row["micro_minus_mid"], ridge=float(prediction))
            row["condition"] = ("wide" if row["spread"] > spread_q else "normal_spread") + "/" + (
                "high_past_move" if row["past_abs_return"] > move_q else "low_past_move")
            predictions.append(row)
        daily = [r for r in predictions if r["day"] == day]
        for name in ("persistence", "microprice", "ridge"):
            metrics.append({"day": day, "model": name, **errors(daily, name)})
            for condition in sorted({r["condition"] for r in daily}):
                subset = [r for r in daily if r["condition"] == condition]
                conditions.append({"day": day, "condition": condition, "model": name, **errors(subset, name)})
        folds.append({"evaluation_day": day, "training_days": sorted({r["day"] for r in train}),
                      "train_samples": len(train), "test_samples": len(test),
                      "train_last_label_ns": max(r["target_ns"] for r in train),
                      "evaluation_boundary_ns": cutoff,
                      "condition_thresholds_from_training": {"spread_q75": spread_q, "past_abs_return_q75": move_q},
                      "feature_support": {key: {
                          "train_min": low, "train_max": high,
                          "test_fraction_outside_training_range": sum(
                              r[key] < low or r[key] > high
                              for r in test) / len(test)} for key, (low, high) in ranges.items()},
                      **model.audit()})
    result = {"question": "Does size-weighted BBO microprice improve one-second observed-midpoint forecasts?",
              "provenance": observations[0].provenance, "product": observations[0].product,
              "config": asdict(config), "inputs": inputs or [], "exclusions": exclusions,
              "folds": folds, "by_day": metrics, "by_market_condition": conditions,
              "overall": [{"model": name, **errors(predictions, name)} for name in ("persistence", "microprice", "ridge")],
              "paired_uncertainty": {name: blocked_improvement(predictions, name, config) for name in ("microprice", "ridge")},
              "warnings": ["Observed local published BBO, not an exchange-timestamp ground truth.",
                           "Crypto venue results do not establish an equity or futures edge.",
                           "Overlapping one-second labels are dependent; sample count is not independent evidence.",
                           "Freshness and publication-gap filters can exclude healthy quiet periods; errors are conditional on retained observations and may have activity-selection bias.",
                           "Forecasting error excludes spreads, fees, queues, fills, latency, impact and inventory risk.",
                           "A short or thin training capture is a workflow check, not evidence of stable predictive skill."]}
    return result, predictions

def save_report(output, result, predictions):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    for filename, data in (("by_day.csv", result["by_day"]), ("by_market_condition.csv", result["by_market_condition"]),
                           ("predictions.csv", predictions)):
        with (output / filename).open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(data[0]))
            writer.writeheader(); writer.writerows(data)
    rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(row[key]))}</td>" for key in
                  ("day", "model", "samples", "mae", "rmse", "bias")) + "</tr>" for row in result["by_day"])
    (output / "index.html").write_text(f'''<!doctype html><html lang="en"><meta charset="utf-8">
<title>MicropriceLab report</title><style>body{{font:16px system-ui;max-width:1050px;margin:40px auto;padding:0 24px;color:#15253b}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #ddd}}table{{border-collapse:collapse;width:100%}}.tag{{background:#e9f0ff;padding:8px;border-radius:5px}}li{{margin:10px 0}}</style>
<h1>MicropriceLab</h1><p class="tag">{html.escape(result['provenance'].upper())} · {html.escape(result['product'])} · one-second horizon</p>
<p>Prices and errors are in quote-currency units. Training always precedes each evaluation day. The fitted model predicts price changes.</p>
<table><thead><tr><th>Evaluation day</th><th>Model</th><th>Samples</th><th>MAE</th><th>RMSE</th><th>Bias</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Interpretation limits</h2><ul>{''.join('<li>'+html.escape(w)+'</li>' for w in result['warnings'])}</ul>
<p>Read report.json for fold coefficients, train-only thresholds, exclusions and paired uncertainty; predictions.csv contains the individual forecasts.</p></html>''')

def run_study(paths, output, config=Config()):
    observations, inputs = read_observations(paths)
    result, predictions = evaluate(observations, inputs, config)
    save_report(output, result, predictions)
    return result
