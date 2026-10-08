import asyncio
from datetime import datetime
import time

from .feed import normalize
from .journal import JournalError, records
from .native import NativeBook

async def replay(path, speed: float = 0, on_quote=None, core_path=None):
    """speed=0 is maximum speed; speed=10 compresses receive-time gaps tenfold."""
    if speed < 0:
        raise ValueError("replay speed cannot be negative")
    start = time.perf_counter()
    processed = 0
    product = None
    first_received = None
    origin = time.monotonic()
    last = None
    last_trusted = None
    async with NativeBook(core_path) as core:
        for _, event in records(path):
            if event["kind"] == "metadata":
                product = event["product"]
                if event.get("scale") != 100000000:
                    raise JournalError("unexpected fixed-point scale")
                continue
            if event["kind"] == "fault":
                continue
            if event["kind"] == "publication":
                if not last or not last["trusted"] or event.get("quote") != last:
                    raise JournalError("publication does not match trusted reconstructed state")
                last_trusted = last
                if on_quote:
                    on_quote(last)
                continue
            if event["kind"] != "command":
                raise JournalError("unknown journal event")
            received = event.get("received_ns")
            if type(received) is not int:
                raise JournalError("missing receive timestamp")
            if first_received is None:
                first_received = received
            if speed:
                due = origin + max(0, received - first_received) / 1e9 / speed
                delay = due - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            command = event.get("command")
            if not isinstance(command, str):
                raise JournalError("missing normalized command")
            if event.get("raw") is not None:
                packet = normalize(event["raw"], product)
                if packet is None or packet.command(int(command.split()[1])) != command:
                    raise JournalError("raw feed does not match normalized command")
            last = await core.request(command)
            if last != event.get("result"):
                raise JournalError(f"replay diverged after command {processed + 1}")
            processed += 1
        final_book = await core.request("DUMP")
    elapsed = time.perf_counter() - start
    return {"product": product, "commands": processed, "seconds": round(elapsed, 6),
            "commands_per_second": round(processed / elapsed, 2) if elapsed else 0,
            "final": last, "last_trusted": last_trusted, "book": final_book,
            "integrity": "verified", "speed": speed}
