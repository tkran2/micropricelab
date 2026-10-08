import copy
import csv
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import numpy as np
from micropricelab.data import Observation, import_journal, read_observations, write_observations
from micropricelab.study import Config, DAY, SECOND, Ridge, blocked_improvement, evaluate, sample
from mdpipe.journal import Journal, JournalError

BASE = 1704067200 * SECOND

def tape(day=0, seconds=12, session=None, bid_size=9., ask_size=1.):
    session = session or f"day-{day}"
    return [Observation(BASE + day * DAY + i * SECOND // 10,
            BASE + day * DAY + i * SECOND // 10 - 1_000_000,
            session, session, "FIXTURE", "synthetic", 99., 101., bid_size, ask_size)
            for i in range(seconds * 10)]

class ResearchTests(unittest.TestCase):
    def test_microprice_weights_opposite_prices(self):
        rows, _ = sample(tape())
        self.assertAlmostEqual(rows[0]["mid"], 100.)
        self.assertAlmostEqual(rows[0]["micro_minus_mid"], .8)
        self.assertAlmostEqual(rows[0]["imbalance"], .8)

    def test_unavailable_future_quote_not_used_as_feature_or_label(self):
        obs = tape()
        # At 2s the last published quote is still 100, not the 150 arriving 2.1s.
        obs[21] = Observation(obs[21].available_ns, obs[21].source_ns, "day-0", "day-0", "FIXTURE", "synthetic", 149., 151., 1., 1.)
        rows, _ = sample(obs)
        row = next(row for row in rows if row["anchor_ns"] == BASE + SECOND)
        self.assertEqual(row["target_mid"], 100.)
        row = next(row for row in rows if row["anchor_ns"] == BASE + 2 * SECOND)
        self.assertEqual(row["mid"], 100.)

    def test_session_and_reconnect_fences(self):
        obs = tape()
        obs = [Observation(**{**o.__dict__, "segment": "before" if i < 60 else "after"}) for i, o in enumerate(obs)]
        rows, _ = sample(obs)
        boundary = BASE + 6 * SECOND
        self.assertFalse(any(row["anchor_ns"] < boundary <= row["target_ns"] for row in rows))

    def test_gap_and_stale_exclusions(self):
        obs = [o for i, o in enumerate(tape()) if not 50 <= i < 70]
        rows, reasons = sample(obs)
        self.assertEqual(reasons["gap_fences"], 1)
        self.assertFalse(any(row["anchor_ns"] < BASE + 7 * SECOND and row["target_ns"] >= BASE + 5 * SECOND for row in rows))
        rows, reasons = sample(tape()[::4], Config(max_gap_ns=SECOND))
        self.assertGreater(reasons["stale_anchor_label_or_lag"], 0)
        self.assertTrue(all(row["label_quote_age_ns"] <= SECOND // 4 for row in rows))

    def test_midnight_evaluation_boundary_is_purged(self):
        obs = [Observation(**{**o.__dict__, "available_ns": o.available_ns + DAY - 6 * SECOND,
                              "source_ns": o.source_ns + DAY - 6 * SECOND}) for o in tape()]
        rows, _ = sample(obs)
        self.assertTrue(rows)
        self.assertTrue(all(row["anchor_ns"] // DAY == row["target_ns"] // DAY for row in rows))

    def test_training_stats_unchanged_when_future_changes(self):
        a = tape(0) + tape(1)
        b = a[:120] + [Observation(**{**o.__dict__, "bid": 299., "ask": 305., "bid_size": 400.}) for o in a[120:]]
        ra, _ = evaluate(a); rb, _ = evaluate(b)
        for key in ("training_mean", "training_scale", "standardized_coefficients", "condition_thresholds_from_training"):
            self.assertEqual(ra["folds"][0][key], rb["folds"][0][key])
        self.assertLess(ra["folds"][0]["train_last_label_ns"], ra["folds"][0]["evaluation_boundary_ns"])

    def test_change_model_not_price_level_and_constant_features_safe(self):
        rows, _ = sample(tape())
        model = Ridge().fit(rows, 10.)
        self.assertTrue(np.all(np.isfinite(model.predict(rows))))
        self.assertTrue(np.allclose(model.predict(rows), 0))

    def test_no_iid_confidence_interval_for_single_evaluation_day(self):
        result, _ = evaluate(tape(0) + tape(1))
        self.assertIsNone(result["paired_uncertainty"]["ridge"]["interval"])

    def test_bootstrap_is_deterministic_and_paired(self):
        rows = [{"day": str(day), "anchor_ns": BASE + day * DAY + minute * 60 * SECOND,
                 "y": 1., "ridge": .5} for day in range(2) for minute in range(6)]
        r = blocked_improvement(rows, "ridge", Config())
        self.assertEqual(r["interval"], [.5, .5])
        self.assertEqual(r, blocked_improvement(rows, "ridge", Config()))

    def test_observation_validation_and_provenance(self):
        for field, value in (("ask", 98.), ("bid_size", 0.), ("bid", float("nan")), ("available_ns", BASE - SECOND)):
            with self.assertRaises(ValueError):
                Observation(**{**tape()[0].__dict__, field: value}).validate()
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "one.csv"; write_observations(p, tape())
            with self.assertRaises(ValueError): read_observations([p, p])
            q = Path(directory) / "two.csv"
            write_observations(q, [Observation(**{**o.__dict__, "session": "real", "segment": "real", "provenance": "real"}) for o in tape(1)])
            with self.assertRaises(ValueError): read_observations([p, q])
            write_observations(q, [Observation(**{**o.__dict__, "session": "overlap", "segment": "overlap"}) for o in tape()])
            with self.assertRaisesRegex(ValueError, "overlapping"): read_observations([p, q])
            p.write_text(p.read_text() + "\n")
            with self.assertRaises(ValueError): read_observations([p])

    def test_verified_import_uses_publication_clock_and_trust_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "journal.sqlite"
            j = Journal(p, "FIXTURE")
            quote = {"trusted": True, "generation": 1, "bid": [9900000000, 200000000], "ask": [10100000000, 100000000]}
            j.append({"kind": "publication", "quote": quote, "received_ns": BASE + 100, "source_received_ns": BASE})
            j.append({"kind": "command", "result": {"trusted": False}})
            j.append({"kind": "publication", "quote": quote, "received_ns": BASE + 200, "source_received_ns": BASE + 150})
            j.close()
            output = Path(directory) / "data.csv"
            import_journal(p, output, "synthetic")
            obs, _ = read_observations([output])
            self.assertEqual(obs[0].available_ns, BASE + 100)
            self.assertNotEqual(obs[0].segment, obs[1].segment)
            con = sqlite3.connect(p); con.execute("UPDATE events SET payload='{}' WHERE id=2"); con.commit(); con.close()
            with self.assertRaises(JournalError): import_journal(p, output, "synthetic")

    def test_expanding_training_and_reproducibility(self):
        obs = tape(0) + tape(1) + tape(2)
        result, predictions = evaluate(obs)
        self.assertEqual(result["folds"][1]["training_days"], ["2024-01-01", "2024-01-02"])
        self.assertEqual((result, predictions), evaluate(obs))

if __name__ == "__main__": unittest.main()
