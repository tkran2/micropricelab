"""Bounded async ingestion; invalid state is fenced before any further publish."""
import asyncio
from collections import deque
from dataclasses import dataclass
import json
import math
import random
import time

from .feed import FeedError, normalize
from .journal import JournalError, Recorder
from .metrics import Metrics
from .native import NativeBook, NativeError

class Recover(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)

class Complete(Exception):
    pass

@dataclass
class Config:
    product: str = "BTC-USD"
    queue_messages: int = 256
    queue_bytes: int = 16 * 1024 * 1024
    max_frame_bytes: int = 8 * 1024 * 1024
    max_lag_seconds: float = .5
    idle_seconds: float = 10
    snapshot_seconds: float = 15
    max_reconnects: int = 20
    retry_seconds: float = .25
    stop_after: int | None = None
    duration_seconds: float | None = None

    def __post_init__(self):
        from .feed import PRODUCT
        if not PRODUCT.fullmatch(self.product):
            raise ValueError("use a product such as BTC-USD")
        if min(self.queue_messages, self.queue_bytes, self.max_frame_bytes) < 1:
            raise ValueError("queue and frame limits must be positive")
        durations = (self.max_lag_seconds, self.idle_seconds, self.snapshot_seconds, self.retry_seconds)
        if not all(math.isfinite(value) for value in durations):
            raise ValueError("timeouts must be finite")
        if min(self.max_lag_seconds, self.idle_seconds, self.snapshot_seconds) <= 0:
            raise ValueError("timeouts must be positive")
        if self.max_reconnects < 0 or self.retry_seconds < 0:
            raise ValueError("invalid retry policy")
        if self.stop_after is not None and self.stop_after < 1:
            raise ValueError("stop_after must be positive")
        if self.duration_seconds is not None and (not math.isfinite(self.duration_seconds) or self.duration_seconds <= 0):
            raise ValueError("duration must be positive")

@dataclass
class Faults:
    drop_at: int | None = None
    disconnect_at: int | None = None
    slow_ms: float = 0
    # The harness slows generation 1, then permits recovery to make progress.
    slow_generation: int | None = None
    slow_after: int = 0
    seen_updates: int = 0
    dropped: bool = False
    disconnected: bool = False

    def __post_init__(self):
        if not math.isfinite(self.slow_ms) or self.slow_ms < 0 or self.slow_after < 0:
            raise ValueError("invalid slow-consumer injection")
        for point in (self.drop_at, self.disconnect_at):
            if point is not None and point < 1:
                raise ValueError("fault update numbers must be positive")

@dataclass
class Envelope:
    raw: str
    received_ns: int
    received_mono: float
    size: int

class BoundedQueue:
    def __init__(self, count: int, byte_limit: int):
        self.queue = asyncio.Queue(maxsize=count)
        self.byte_limit = byte_limit
        self.bytes = 0
        self.arrivals = deque()

    def put(self, frame: Envelope):
        if self.queue.full() or self.bytes + frame.size > self.byte_limit:
            raise Recover("queue_overflow")
        self.queue.put_nowait(frame)
        self.bytes += frame.size
        self.arrivals.append(frame.received_mono)

    async def get(self):
        frame = await self.queue.get()
        self.bytes -= frame.size
        self.arrivals.popleft()
        return frame

class Pipeline:
    def __init__(self, core: NativeBook, recorder: Recorder, config: Config,
                 faults: Faults | None = None, on_quote=None, on_state=None):
        self.core, self.recorder, self.config = core, recorder, config
        self.faults = faults or Faults()
        self.on_quote = on_quote
        self.on_state = on_state
        self.metrics = Metrics()
        self.generation = 0
        self.gate_open = False
        self.failed = False
        self.last_trusted = None
        self.invalid_since = None
        self.inflight_mono = None

    def close_gate(self, reason="unavailable"):
        was_open = self.gate_open
        self.failed = True
        self.gate_open = False
        if self.invalid_since is None:
            self.invalid_since = time.monotonic()
        if was_open and self.on_state:
            self.on_state({"trusted": False, "generation": self.generation,
                           "reason": reason, "bid": None, "ask": None})

    async def _command(self, command: str, frame: Envelope | None = None):
        started = time.perf_counter_ns()
        result = await self.core.request(command)
        self.metrics.native_roundtrip.add((time.perf_counter_ns() - started) / 1000)
        event = {"kind": "command", "command": command, "result": result,
                 "received_ns": frame.received_ns if frame else time.time_ns(),
                 "raw": frame.raw if frame else None}
        started = time.perf_counter_ns()
        await self.recorder.append(event)
        self.metrics.commit.add((time.perf_counter_ns() - started) / 1000)
        return result

    async def command(self, command: str, frame: Envelope | None = None):
        # Treat native application + journal commit as one cancellation unit.
        # Cancellation after native mutation must not leave an unrecorded command.
        operation = asyncio.create_task(self._command(command, frame))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            await operation
            raise

    async def reader(self, source, queue: BoundedQueue):
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(source.recv(), self.config.idle_seconds)
                except asyncio.TimeoutError as exc:
                    raise Recover("idle_timeout") from exc
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                if not isinstance(raw, str):
                    raise Recover("malformed_frame")
                size = len(raw.encode("utf-8"))
                if size > self.config.max_frame_bytes:
                    raise Recover("frame_limit")
                received_ns, received_mono = time.time_ns(), time.monotonic()
                self.metrics.received += 1
                if self.faults.drop_at is not None or self.faults.disconnect_at is not None:
                    try:
                        probe = json.loads(raw)
                        is_update = probe.get("channel") == "l2_data" and any(
                            event.get("type") == "update" for event in probe.get("events", [])
                        )
                    except (ValueError, AttributeError, TypeError):
                        is_update = False
                    if is_update:
                        self.faults.seen_updates += 1
                        if self.faults.seen_updates == self.faults.disconnect_at and not self.faults.disconnected:
                            self.faults.disconnected = True
                            raise Recover("injected_disconnect")
                        if self.faults.seen_updates == self.faults.drop_at and not self.faults.dropped:
                            self.faults.dropped = True
                            self.metrics.injected_drops += 1
                            await self.recorder.append({"kind": "fault", "fault": "dropped_frame",
                                                        "received_ns": received_ns, "raw": raw})
                            continue
                queue.put(Envelope(raw, received_ns, received_mono, size))
                self.metrics.peak_messages = max(self.metrics.peak_messages, queue.queue.qsize())
                self.metrics.peak_bytes = max(self.metrics.peak_bytes, queue.bytes)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.close_gate(exc.reason if isinstance(exc, Recover) else "disconnect")
            if isinstance(exc, (Recover, JournalError)):
                raise
            raise Recover("disconnect") from exc

    async def consumer(self, queue: BoundedQueue):
        try:
            while True:
                frame = await queue.get()
                self.inflight_mono = frame.received_mono
                if (self.faults.slow_ms and self.metrics.processed >= self.faults.slow_after and
                        self.faults.slow_generation in (None, self.generation)):
                    await asyncio.sleep(self.faults.slow_ms / 1000)
                dwell = time.monotonic() - frame.received_mono
                self.metrics.dwell.add(dwell * 1e6)
                if dwell > self.config.max_lag_seconds:
                    raise Recover("stale_queue")
                packet = normalize(frame.raw, self.config.product)
                result = await self.command(packet.command(self.generation), frame)
                if result["status"] not in ("snapshot", "applied", "duplicate", "tick"):
                    raise Recover(result["reason"] if result["reason"] != "ok" else "invalid_state")
                if time.monotonic() - frame.received_mono > self.config.max_lag_seconds:
                    raise Recover("stale_processing")
                if self.failed:
                    self.inflight_mono = None
                    return
                if packet.kind == "TICK":
                    self.metrics.control_processed += 1
                    self.inflight_mono = None
                    continue
                self.metrics.processed += 1
                if result["status"] == "snapshot":
                    self.metrics.snapshots += 1
                    if self.invalid_since is not None:
                        self.metrics.recovery.add((time.monotonic() - self.invalid_since) * 1e6)
                        self.invalid_since = None
                if result["trusted"]:
                    self.gate_open = True
                    # Duplicate packets do not republish an old quote.
                    if result["status"] != "duplicate":
                        self.last_trusted = result
                        self.metrics.published += 1
                        self.metrics.receive_to_publish.add((time.monotonic() - frame.received_mono) * 1e6)
                        if self.on_quote is not None:
                            self.on_quote(result)
                        # Record what actually passed the gate. An applied update
                        # may have been suppressed by a concurrent disconnect.
                        # The underlying command is durable before this callback;
                        # quote delivery itself is not an exactly-once transaction.
                        await self.recorder.append({"kind": "publication", "quote": result,
                                                    "source_received_ns": frame.received_ns,
                                                    "received_ns": time.time_ns()})
                self.inflight_mono = None
                if self.config.stop_after is not None and self.metrics.processed >= self.config.stop_after:
                    raise Complete()
        except asyncio.CancelledError:
            raise
        except FeedError as exc:
            self.close_gate("malformed_frame")
            await self.recorder.append({"kind": "fault", "fault": "malformed_frame",
                                        "received_ns": frame.received_ns, "raw": frame.raw,
                                        "detail": str(exc)})
            raise Recover("malformed_frame") from exc
        except Exception as exc:
            self.close_gate(exc.reason if isinstance(exc, Recover) else "stopped" if isinstance(exc, Complete) else "processing_failure")
            raise

    async def watchdog(self, queue):
        start = time.monotonic()
        while True:
            await asyncio.sleep(min(.02, self.config.max_lag_seconds / 4))
            now = time.monotonic()
            if not self.gate_open and now - start > self.config.snapshot_seconds:
                self.close_gate("snapshot_timeout")
                raise Recover("snapshot_timeout")
            if self.inflight_mono is not None and now - self.inflight_mono > self.config.max_lag_seconds:
                self.close_gate("stale_processing")
                raise Recover("stale_processing")
            if queue.arrivals and now - queue.arrivals[0] > self.config.max_lag_seconds:
                self.close_gate("stale_queue")
                raise Recover("stale_queue")

    async def run_session(self, source):
        queue = BoundedQueue(self.config.queue_messages, self.config.queue_bytes)
        tasks = [asyncio.create_task(self.reader(source, queue)),
                 asyncio.create_task(self.consumer(queue)),
                 asyncio.create_task(self.watchdog(queue))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            # If reader fails concurrently with planned completion, the gate still closes.
            for task in tasks:
                if task in done:
                    task.result()
            raise Recover("consumer_stopped")
        finally:
            self.close_gate()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.inflight_mono = None

    async def run(self, factory):
        start = time.monotonic()
        attempts = 0
        error = None
        try:
            while True:
                if (self.config.duration_seconds is not None and self.generation > 0 and
                        time.monotonic() - start >= self.config.duration_seconds):
                    break
                self.generation += 1
                self.failed = False
                self.gate_open = False
                await self.command(f"RESET {self.generation}")
                try:
                    async with factory(self.generation) as source:
                        if self.config.duration_seconds is None:
                            await self.run_session(source)
                        else:
                            remaining = self.config.duration_seconds - (time.monotonic() - start)
                            if remaining <= 0:
                                raise Complete()
                            try:
                                await asyncio.wait_for(self.run_session(source), remaining)
                            except asyncio.TimeoutError:
                                raise Complete() from None
                except Complete:
                    break
                except Recover as exc:
                    self.close_gate(exc.reason)
                    self.metrics.invalidations[exc.reason] += 1
                    await self.command(f"INVALIDATE {self.generation} {exc.reason}")
                    if attempts >= self.config.max_reconnects:
                        error = "reconnect budget exhausted"
                        break
                    attempts += 1
                    self.metrics.reconnects += 1
                    delay = min(8, self.config.retry_seconds * (2 ** min(attempts - 1, 5)))
                    await asyncio.sleep(delay * random.uniform(.8, 1.2))
                except (JournalError, NativeError):
                    raise
                except Exception as exc:
                    self.close_gate()
                    self.metrics.invalidations["connect_error"] += 1
                    await self.command(f"INVALIDATE {self.generation} connect_error")
                    if attempts >= self.config.max_reconnects:
                        error = f"connection failed: {type(exc).__name__}"
                        break
                    attempts += 1
                    self.metrics.reconnects += 1
                    await asyncio.sleep(min(8, self.config.retry_seconds * (2 ** min(attempts - 1, 5))))
            if self.metrics.snapshots == 0 and error is None:
                error = "capture ended before receiving a valid snapshot"
        finally:
            self.close_gate()
            # If storage fails, native state is still invalidated; publication stays shut.
            try:
                final = await self.command(f"INVALIDATE {self.generation} stopped")
            except (JournalError, NativeError):
                try:
                    await self.core.request(f"INVALIDATE {self.generation} recording_failure")
                except NativeError:
                    pass
                raise
        return {"product": self.config.product, "elapsed_seconds": round(time.monotonic() - start, 6),
                "metrics": self.metrics.summary(), "last_trusted": self.last_trusted,
                "final": final, "error": error}
