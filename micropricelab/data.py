"""Verified journal import and explicit-provenance observation files."""
from __future__ import annotations
import csv
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "mdpipe"))
from mdpipe.journal import JournalError, records  # user-owned, vendored capture implementation

@dataclass(frozen=True)
class Observation:
    available_ns: int
    source_ns: int
    session: str
    segment: str
    product: str
    provenance: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float

    def validate(self):
        numbers = (self.bid, self.ask, self.bid_size, self.ask_size)
        if not all(math.isfinite(x) and 0 < x <= (2**63 - 1) / 100000000 for x in numbers):
            raise ValueError("prices and sizes must fit positive signed-64-bit 1e-8 fixed point")
        if not 0 <= self.source_ns <= self.available_ns < 2**63:
            raise ValueError("invalid UTC nanosecond timestamps")
        if self.ask <= self.bid or self.available_ns < self.source_ns:
            raise ValueError("crossed/locked quote or publication before source arrival")
        if self.provenance not in ("real", "synthetic") or not self.segment:
            raise ValueError("explicit provenance and segment are required")

FIELDS = tuple(Observation.__dataclass_fields__)

def write_observations(path, observations, metadata=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=FIELDS)
        writer.writeheader()
        for obs in observations:
            obs.validate()
            writer.writerow(obs.__dict__)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(".meta.json").write_text(json.dumps({
        **(metadata or {}), "csv_sha256": digest,
        "availability_clock": "local UTC publication time, not exchange event time",
    }, indent=2) + "\n")
    return digest

def read_observations(paths):
    result = []
    inputs = []
    for path in paths:
        path = Path(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        sidecar = path.with_suffix(".meta.json")
        if sidecar.exists():
            meta = json.loads(sidecar.read_text())
            if meta.get("csv_sha256") != digest:
                raise ValueError(f"CSV fingerprint mismatch: {path}")
        with path.open(newline="") as inp:
            if tuple((reader := csv.DictReader(inp)).fieldnames or ()) != FIELDS:
                raise ValueError(f"unsupported observation schema: {path}")
            for row in reader:
                for name in ("available_ns", "source_ns"):
                    row[name] = int(row[name])
                for name in ("bid", "ask", "bid_size", "ask_size"):
                    row[name] = float(row[name])
                obs = Observation(**row)
                obs.validate()
                result.append(obs)
        inputs.append({"file": path.name, "sha256": digest})
    if not result:
        raise ValueError("no observations")
    if len({o.provenance for o in result}) != 1 or len({o.product for o in result}) != 1:
        raise ValueError("never pool real/synthetic data or different products")
    # A repeated file must not accidentally double the sample size.
    if len({(o.session, o.segment, o.available_ns) for o in result}) != len(result):
        raise ValueError("duplicate observations or repeated input file")
    spans = {}
    for obs in result:
        low, high = spans.get(obs.session, (obs.available_ns, obs.available_ns))
        spans[obs.session] = min(low, obs.available_ns), max(high, obs.available_ns)
    ordered_spans = sorted(spans.values())
    if any(left[1] >= right[0] for left, right in zip(ordered_spans, ordered_spans[1:])):
        raise ValueError("overlapping capture sessions would double-count a market period")
    result.sort(key=lambda o: o.available_ns)
    return result, inputs

def import_journal(path, output, provenance):
    if provenance not in ("real", "synthetic"):
        raise ValueError("declare real or synthetic explicitly")
    path = Path(path)
    observations, fence, previous_time, metadata, chain = [], 0, -1, None, hashlib.sha256()
    session = ""
    for row_id, event in records(path):
        chain.update(json.dumps(event, sort_keys=True, separators=(",", ":")).encode())
        kind = event.get("kind")
        if kind == "metadata":
            metadata = event
            session = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()[:20]
        elif kind == "command" and not event.get("result", {}).get("trusted", False):
            fence += 1
        elif kind in ("fault", "disconnect", "reconnect"):
            fence += 1
        elif kind == "publication":
            quote = event["quote"]
            if not quote.get("trusted") or not quote.get("bid") or not quote.get("ask"):
                fence += 1
                continue
            now = int(event["received_ns"])
            if now <= previous_time:
                raise ValueError("publication clock is non-monotonic; reject this session")
            previous_time = now
            scale = metadata["scale"]
            obs = Observation(now, int(event["source_received_ns"]), session,
                              f"{session}:{quote['generation']}:{fence}",
                              metadata["product"], provenance,
                              quote["bid"][0] / scale, quote["ask"][0] / scale,
                              quote["bid"][1] / scale, quote["ask"][1] / scale)
            obs.validate()
            observations.append(obs)
    if not observations:
        raise ValueError("journal has no trusted usable publications")
    report = {"source_journal": path.name, "verified_rows": row_id,
              "canonical_events_sha256": chain.hexdigest(), "provenance": provenance,
              "observations": len(observations), "trust_fences": fence,
              "first_available_ns": observations[0].available_ns,
              "last_available_ns": observations[-1].available_ns,
              "source": "Coinbase Advanced Trade public level2" if provenance == "real" else "synthetic fixture"}
    write_observations(output, observations, report)
    return report
