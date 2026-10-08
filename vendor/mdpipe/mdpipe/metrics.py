from collections import Counter
import math
import random

class Distribution:
    """Bounded deterministic reservoir; tail percentiles are estimates."""
    def __init__(self, capacity: int = 4096):
        self.capacity = capacity
        self.samples: list[float] = []
        self.count = 0
        self.total = 0.0
        self.maximum = 0.0
        self.rng = random.Random(17)

    def add(self, microseconds: float):
        self.count += 1
        self.total += microseconds
        self.maximum = max(self.maximum, microseconds)
        if len(self.samples) < self.capacity:
            self.samples.append(microseconds)
        else:
            index = self.rng.randrange(self.count)
            if index < self.capacity:
                self.samples[index] = microseconds

    def summary(self):
        ordered = sorted(self.samples)
        def percentile(q):
            return round(ordered[max(0, math.ceil(q * len(ordered)) - 1)], 3) if ordered else 0
        return {"count": self.count, "sample_count": len(ordered),
                "mean": round(self.total / self.count, 3) if self.count else 0,
                "p50": percentile(.5), "p99": percentile(.99),
                "max": round(self.maximum, 3)}

class Metrics:
    def __init__(self):
        self.received = self.processed = self.published = self.reconnects = 0
        self.injected_drops = self.snapshots = 0
        self.control_processed = 0
        self.peak_messages = self.peak_bytes = 0
        self.invalidations = Counter()
        self.dwell = Distribution()
        self.native_roundtrip = Distribution()
        self.commit = Distribution()
        self.receive_to_publish = Distribution()
        self.recovery = Distribution()

    def summary(self):
        return {"received": self.received, "processed": self.processed,
                "control_processed": self.control_processed,
                "published": self.published, "snapshots": self.snapshots,
                "reconnects": self.reconnects, "injected_drops": self.injected_drops,
                "invalidations": dict(self.invalidations),
                "queue_peak_messages": self.peak_messages, "queue_peak_bytes": self.peak_bytes,
                "queue_dwell_us": self.dwell.summary(),
                "native_roundtrip_us": self.native_roundtrip.summary(),
                "journal_commit_us": self.commit.summary(),
                "receive_to_publish_us": self.receive_to_publish.summary(),
                "recovery_us": self.recovery.summary()}
