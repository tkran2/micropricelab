from pathlib import Path
import json
import time

from .journal import Journal, Recorder
from .native import NativeBook
from .pipeline import Config, Faults, Pipeline
from .replay import replay
from .simulated import SimulatedExchange

def fresh_path(prefix: str, suffix: str):
    return Path("data") / f"{prefix}-{time.time_ns()}{suffix}"

async def exercise(path, config, faults=None, interval=.003, core_path=None):
    exchange = SimulatedExchange(config.product, interval=interval)
    recorder = Recorder(Journal(path, config.product))
    quotes = []
    try:
        async with NativeBook(core_path) as core:
            def verify_quote(quote):
                expected = exchange.oracle[quote["generation"], quote["sequence"]]
                if quote["bid"] != expected["bid"] or quote["ask"] != expected["ask"]:
                    raise AssertionError("publication differs from authoritative synthetic book")
                if not quote["trusted"]:
                    raise AssertionError("untrusted publication")
                quotes.append(quote)
            pipeline = Pipeline(core, recorder, config, faults, verify_quote)
            report = await pipeline.run(exchange)
            if pipeline.gate_open or report["final"]["trusted"]:
                raise AssertionError("publication gate remained open after shutdown")
    finally:
        await recorder.close()
    restored = await replay(path, core_path=core_path)
    if restored["final"] != report["final"] or restored["last_trusted"] != report["last_trusted"]:
        raise AssertionError("recorded replay diverged from capture")
    report["journal"] = str(path)
    report["source"] = "deterministic synthetic Coinbase-format feed"
    report["verified_publications"] = len(quotes)
    report["replay"] = {key: restored[key] for key in ("commands", "integrity", "commands_per_second")}
    return report

async def demo(path=None, core_path=None):
    path = Path(path) if path else fresh_path("demo", ".sqlite")
    return await exercise(path, Config(stop_after=50, retry_seconds=0), core_path=core_path)

async def fault_suite(directory=None, core_path=None):
    directory = Path(directory) if directory else fresh_path("faults", "")
    directory.mkdir(parents=True, exist_ok=False)
    cases = [
        ("baseline", Config(stop_after=50, retry_seconds=0), Faults(), .003, None),
        ("dropped_message", Config(stop_after=50, retry_seconds=0), Faults(drop_at=7), .003, "sequence_gap"),
        ("disconnect", Config(stop_after=50, retry_seconds=0), Faults(disconnect_at=7), .003, "injected_disconnect"),
        ("slow_consumer", Config(stop_after=50, queue_messages=4, retry_seconds=0, max_lag_seconds=1),
         Faults(slow_ms=20, slow_generation=1, slow_after=4), .003, "queue_overflow"),
        ("stale_backlog", Config(stop_after=50, queue_messages=128, retry_seconds=0, max_lag_seconds=.02),
         Faults(slow_ms=50, slow_generation=1, slow_after=4), .003, "stale_processing"),
    ]
    reports = []
    for name, config, faults, interval, expected in cases:
        report = await exercise(directory / f"{name}.sqlite", config, faults, interval, core_path)
        report["scenario"] = name
        if report["error"]:
            raise AssertionError(report["error"])
        if expected and report["metrics"]["invalidations"].get(expected, 0) < 1:
            raise AssertionError(f"{name}: expected {expected} was not detected")
        if expected and report["last_trusted"]["generation"] < 2:
            raise AssertionError(f"{name}: no fresh-generation recovery")
        if report["metrics"]["queue_peak_messages"] > config.queue_messages:
            raise AssertionError("message queue exceeded bound")
        if report["metrics"]["queue_peak_bytes"] > config.queue_bytes:
            raise AssertionError("byte queue exceeded bound")
        reports.append(report)
    output = directory / "report.json"
    output.write_text(json.dumps({"source": "synthetic", "passed": len(reports), "scenarios": reports}, indent=2) + "\n")
    return {"passed": len(reports), "report": str(output), "scenarios": reports}
