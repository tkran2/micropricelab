"""Coinbase Advanced Trade level2 normalization. Decimal strings stay exact."""
from dataclasses import dataclass
import json
import re

SCALE = 100_000_000
MAX_INT = (1 << 63) - 1
MAX_SEQUENCE = (1 << 64) - 1
MAX_UPDATES = 250_000
DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")
PRODUCT = re.compile(r"[A-Z0-9]{1,16}-[A-Z0-9]{1,16}\Z")

class FeedError(ValueError):
    pass

def units(value: object) -> int:
    if not isinstance(value, str) or len(value) > 40 or not DECIMAL.fullmatch(value):
        raise FeedError("price and quantity must be unsigned decimal strings")
    whole, _, fraction = value.partition(".")
    # Trailing zeroes beyond 8 decimal places are harmless. Never round silently.
    if len(fraction) > 8 and any(c != "0" for c in fraction[8:]):
        raise FeedError("more than 8 significant decimal places")
    result = int(whole) * SCALE + int((fraction[:8] + "00000000")[:8])
    if result > MAX_INT:
        raise FeedError("fixed-point overflow")
    return result

def decimal(value: int) -> str:
    whole, fraction = divmod(value, SCALE)
    return f"{whole}.{fraction:08d}"

@dataclass(frozen=True)
class Packet:
    kind: str
    sequence: int
    levels: tuple[tuple[str, int, int], ...]
    exchange_time: str | None

    def command(self, generation: int) -> str:
        if self.kind == "TICK":
            return f"TICK {generation} {self.sequence}"
        parts = [self.kind, str(generation), str(self.sequence), str(len(self.levels))]
        for side, price, quantity in self.levels:
            parts.extend((side, str(price), str(quantity)))
        return " ".join(parts)

def normalize(raw: str, product: str) -> Packet:
    try:
        message = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise FeedError("invalid JSON") from exc
    if not isinstance(message, dict):
        raise FeedError("expected JSON object")
    if message.get("type") == "error" or message.get("channel") == "error":
        raise FeedError("exchange reported an error")
    channel = message.get("channel")
    if not isinstance(channel, str) or not channel:
        raise FeedError("missing channel")
    sequence = message.get("sequence_num")
    if type(sequence) is not int or not 0 <= sequence <= MAX_SEQUENCE:
        raise FeedError("missing or invalid envelope sequence_num")
    timestamp = message.get("timestamp")
    if timestamp is not None and not isinstance(timestamp, str):
        raise FeedError("invalid timestamp")
    if channel != "l2_data":
        # Subscription replies and heartbeats share the connection sequence with
        # l2_data. Advance continuity without changing levels or emitting a quote.
        return Packet("TICK", sequence, (), timestamp)
    events = message.get("events")
    if not isinstance(events, list) or not events:
        raise FeedError("missing l2 events")
    kinds: set[str] = set()
    levels: list[tuple[str, int, int]] = []
    seen: set[tuple[str, int]] = set()
    for event in events:
        if not isinstance(event, dict) or event.get("product_id") != product:
            raise FeedError("unexpected product on single-product connection")
        kind = event.get("type")
        if kind not in ("snapshot", "update"):
            raise FeedError("unknown l2 event type")
        kinds.add(kind)
        updates = event.get("updates")
        if not isinstance(updates, list):
            raise FeedError("missing updates")
        for update in updates:
            if not isinstance(update, dict) or update.get("side") not in ("bid", "offer"):
                raise FeedError("unknown side")
            side = "B" if update["side"] == "bid" else "A"
            price = units(update.get("price_level"))
            quantity = units(update.get("new_quantity"))
            if price == 0:
                raise FeedError("price must be positive")
            if kind == "snapshot":
                key = (side, price)
                if key in seen:
                    raise FeedError("duplicate snapshot level")
                seen.add(key)
            levels.append((side, price, quantity))
            if len(levels) > MAX_UPDATES:
                raise FeedError("too many updates")
    if len(kinds) != 1:
        raise FeedError("mixed snapshot and update packet")
    return Packet("SNAP" if "snapshot" in kinds else "UPD", sequence, tuple(levels), timestamp)
