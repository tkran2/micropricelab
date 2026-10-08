import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import uuid
import numpy as np
from .data import JournalError, Observation, ROOT, import_journal, write_observations
from .study import Config, DAY, SECOND, run_study

def demo(output):
    out = Path(output); paths = []
    rng = np.random.default_rng(2027)
    for day in range(4):
        session = f"synthetic-day-{day}"
        start = 1704067200 * SECOND + day * DAY
        obs, mid, previous_imbalance = [], 100., 0.
        for step in range(2400):
            imbalance = float(rng.uniform(-.9, .9))
            spread = .02 if step % 700 < 500 else .08
            mid += .003 * previous_imbalance + float(rng.normal(0, .0015))
            # Deliberately planted synthetic predictability; never evidence of real alpha.
            obs.append(Observation(start + step * 50_000_000, start + step * 50_000_000 - 100_000,
                session, session, "SYNTHETIC-USD", "synthetic", mid - spread / 2, mid + spread / 2,
                10 * (1 + imbalance), 10 * (1 - imbalance)))
            previous_imbalance = imbalance
        path = out / "data" / f"day-{day}.csv"
        write_observations(path, obs, {"source": "planted-signal fixture", "seed": 2027, "not_market_data": True})
        paths.append(path)
    return run_study(paths, out / "report")

def capture(args):
    if not all(math.isfinite(x) and x > 0 for x in (args.hours, args.session_seconds, args.max_total_mb)):
        raise ValueError("positive capture settings required")
    out = Path(args.output).resolve(); out.mkdir(parents=True, exist_ok=True)
    vendor = ROOT / "vendor" / "mdpipe"
    subprocess.run(["make", "-C", str(vendor), "build/book-core"], check=True)
    deadline = time.monotonic() + args.hours * 3600
    captures = []
    while time.monotonic() < deadline:
        used = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
        remaining_mb = args.max_total_mb - used / 1024**2
        if remaining_mb < 32:
            raise ValueError("total capture disk budget reached; do not delete previous sessions")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        journal = out / f"{stamp}-{uuid.uuid4().hex[:8]}.sqlite"
        duration = min(args.session_seconds, max(.1, deadline - time.monotonic()))
        status = subprocess.run([sys.executable, "-m", "mdpipe", "capture", "--product", args.product,
            "--seconds", str(duration), "--max-storage-mb", str(int(min(512, remaining_mb))),
            "--output", str(journal), "--quiet"], cwd=vendor).returncode
        report = import_journal(journal, out / "observations" / (journal.stem + ".csv"), "real")
        captures.append({**report, "collector_exit_status": status})
        (out / "manifest.json").write_text(json.dumps({"source": "live Coinbase public websocket", "captures": captures}, indent=2) + "\n")
        if status:
            raise ValueError("collector stopped with an error; verified committed data retained, inspect summary")
    return {"sessions": len(captures), "manifest": str(out / "manifest.json")}

def main():
    parser = argparse.ArgumentParser(description="Causal multi-day BBO forecasting study")
    subs = parser.add_subparsers(dest="command", required=True)
    d = subs.add_parser("demo"); d.add_argument("--output", default="reports/synthetic-demo")
    imp = subs.add_parser("import-journal"); imp.add_argument("journal"); imp.add_argument("--provenance", choices=("real", "synthetic"), required=True); imp.add_argument("--output", required=True)
    s = subs.add_parser("study"); s.add_argument("files", nargs="+"); s.add_argument("--output", default="reports/study"); s.add_argument("--train-days", type=int, default=1); s.add_argument("--step-ms", type=int, choices=(100, 1000), default=100)
    cap = subs.add_parser("capture"); cap.add_argument("--hours", type=float, default=.5); cap.add_argument("--session-seconds", type=float, default=900); cap.add_argument("--product", default="BTC-USD"); cap.add_argument("--max-total-mb", type=float, default=4096); cap.add_argument("--output", default="data/captures")
    args = parser.parse_args()
    try:
        if args.command == "demo": result = demo(args.output)
        elif args.command == "import-journal": result = import_journal(args.journal, args.output, args.provenance)
        elif args.command == "capture": result = capture(args)
        else: result = run_study(args.files, args.output, Config(train_days=args.train_days, step_ns=args.step_ms * 1_000_000))
        print(json.dumps({key: result[key] for key in ("overall", "provenance", "sessions", "observations", "manifest") if key in result}, indent=2))
        return 0
    except (ValueError, OSError, JournalError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr); return 1

if __name__ == "__main__":
    sys.exit(main())
