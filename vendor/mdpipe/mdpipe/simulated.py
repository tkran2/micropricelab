"""Deterministic synthetic exchange; recovery snapshots reflect the true book."""
import asyncio
import json
import random

from .feed import decimal, SCALE

def payload(product, sequence, kind, levels, timestamp="2026-01-01T00:00:00Z"):
    return json.dumps({"channel": "l2_data", "sequence_num": sequence,
                       "timestamp": timestamp,
                       "events": [{"type": kind, "product_id": product,
                                   "updates": [{"side": "bid" if side == "B" else "offer",
                                                "price_level": decimal(price),
                                                "new_quantity": decimal(qty),
                                                "event_time": timestamp}
                                               for side, price, qty in levels]}]},
                      separators=(",", ":"))

class SimulatedExchange:
    def __init__(self, product="BTC-USD", seed=7, interval=.003, recovery_interval=.003):
        self.product, self.interval, self.recovery_interval = product, interval, recovery_interval
        self.rng = random.Random(seed)
        self.bids = {(10000 - i) * SCALE: (i + 1) * SCALE for i in range(20)}
        self.asks = {(10002 + i) * SCALE: (i + 1) * SCALE for i in range(20)}
        self.oracle = {}
        self.version = 0

    def __call__(self, generation):
        return SimulatedConnection(self, generation)

    def expect(self, generation, sequence):
        self.oracle[generation, sequence] = {
            "bid": [max(self.bids), self.bids[max(self.bids)]] if self.bids else None,
            "ask": [min(self.asks), self.asks[min(self.asks)]] if self.asks else None,
            "bids": dict(self.bids), "asks": dict(self.asks),
        }

class SimulatedConnection:
    def __init__(self, exchange, generation):
        self.exchange, self.generation = exchange, generation
        self.sequence = -1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def recv(self):
        delay = self.exchange.interval if self.generation == 1 else self.exchange.recovery_interval
        await asyncio.sleep(delay)
        self.sequence += 1
        if self.sequence == 0:
            levels = [("B", p, q) for p, q in sorted(self.exchange.bids.items())]
            levels += [("A", p, q) for p, q in sorted(self.exchange.asks.items())]
            kind = "snapshot"
        else:
            self.exchange.version += 1
            side = self.exchange.rng.choice(("B", "A"))
            index = self.exchange.rng.randrange(40)
            price = ((10000 - index) if side == "B" else (10002 + index)) * SCALE
            quantity = self.exchange.rng.randrange(0, 12) * SCALE // 10
            book = self.exchange.bids if side == "B" else self.exchange.asks
            if quantity:
                book[price] = quantity
            else:
                book.pop(price, None)
            levels, kind = [(side, price, quantity)], "update"
        self.exchange.expect(self.generation, self.sequence)
        return payload(self.exchange.product, self.sequence, kind, levels)
