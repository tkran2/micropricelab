import argparse
import asyncio
import json
from pathlib import Path
import sys

from .feed import decimal
from .harness import demo, fault_suite, fresh_path
from .journal import Journal, JournalError, Recorder
from .native import NativeBook, NativeError
from .pipeline import Config, Faults, Pipeline
from .replay import replay
from .research import features
from .source import CoinbaseSource

def quote_output(quote):
    def price(side):
        return {"price": decimal(side[0]), "quantity": decimal(side[1])} if side else None
    print(json.dumps({"trusted": True, "generation": quote["generation"], "sequence": quote["sequence"],
                      "bid": price(quote["bid"]), "ask": price(quote["ask"]),
                      "digest": quote["digest"]}), flush=True)

def state_output(state):
    print(json.dumps(state), flush=True)

async def capture(args):
    try:
        from websockets.asyncio.client import connect  # noqa: F401
    except ImportError as exc:
        raise ValueError("run make setup, then use .venv/bin/python for capture") from exc
    config = Config(product=args.product, queue_messages=args.queue_messages,
                    queue_bytes=args.queue_bytes, max_lag_seconds=args.max_lag_ms / 1000,
                    duration_seconds=args.seconds, max_reconnects=args.max_reconnects)
    path = Path(args.output) if args.output else fresh_path("capture", ".sqlite")
    recorder = Recorder(Journal(path, args.product, args.max_storage_mb * 1024 * 1024))
    faults = Faults(drop_at=args.drop_at, disconnect_at=args.disconnect_at, slow_ms=args.slow_ms)
    try:
        async with NativeBook(args.core) as core:
            pipeline = Pipeline(core, recorder, config, faults,
                                None if args.quiet else quote_output,
                                None if args.quiet else state_output)
            report = await pipeline.run(lambda _: CoinbaseSource(config))
    finally:
        await recorder.close()
    report["journal"] = str(path)
    report["source"] = "Coinbase Advanced Trade public level2"
    summary_path = path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 1 if report["error"] else 0

def parser():
    p = argparse.ArgumentParser(description="C++ book reconstruction + fault-tolerant async market-data journal")
    p.add_argument("--core", help="path to the C++ book-core executable")
    commands = p.add_subparsers(dest="action", required=True)
    d = commands.add_parser("demo", help="offline synthetic capture and verified replay")
    d.add_argument("--output")
    f = commands.add_parser("faults", help="run five offline fault scenarios")
    f.add_argument("--output")
    r = commands.add_parser("replay", help="verify journal integrity and replay into C++ core")
    r.add_argument("journal")
    r.add_argument("--speed", type=float, default=0, help="0=max speed, 10=10x receive-time playback")
    r.add_argument("--quotes", action="store_true")
    r.add_argument("--features", help="export trusted top-of-book research features to a new CSV")
    c = commands.add_parser("capture", help="record public Coinbase level2 market data")
    c.add_argument("--product", default="BTC-USD")
    c.add_argument("--seconds", type=float, default=60)
    c.add_argument("--output")
    c.add_argument("--queue-messages", type=int, default=256)
    c.add_argument("--queue-bytes", type=int, default=16 * 1024 * 1024)
    c.add_argument("--max-lag-ms", type=float, default=500)
    c.add_argument("--max-storage-mb", type=int, default=512)
    c.add_argument("--max-reconnects", type=int, default=20)
    c.add_argument("--drop-at", type=int, help="deliberately drop the Nth update")
    c.add_argument("--disconnect-at", type=int, help="deliberately disconnect at the Nth update")
    c.add_argument("--slow-ms", type=float, default=0, help="delay every consumer frame")
    c.add_argument("--quiet", action="store_true")
    return p

async def run(args):
    if args.action == "capture":
        return await capture(args)
    if args.action == "demo":
        result = await demo(args.output, args.core)
    elif args.action == "faults":
        result = await fault_suite(args.output, args.core)
    else:
        result = await replay(args.journal, args.speed, quote_output if args.quotes else None, args.core)
        if args.features:
            result["features"] = features(args.journal, args.features)
    print(json.dumps(result, indent=2))
    return 0

def main():
    args = parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Stopped; only committed journal rows are replayable.", file=sys.stderr)
        return 130
    except (JournalError, NativeError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
