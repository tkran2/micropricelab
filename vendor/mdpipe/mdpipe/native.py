"""One persistent C++ process; requests are serialized and cancellation safe."""
import asyncio
import json
import os
from pathlib import Path

DEFAULT_CORE = Path(__file__).resolve().parents[1] / "build" / "book-core"

class NativeError(RuntimeError):
    pass

class NativeBook:
    def __init__(self, executable: str | Path | None = None):
        self.executable = str(executable or os.environ.get("MDPIPE_CORE", DEFAULT_CORE))
        self.process = None
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.executable, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=32 * 1024 * 1024,
            )
        except OSError as exc:
            raise NativeError("C++ core unavailable; run make first") from exc
        return self

    async def _exchange(self, command: str) -> dict:
        if self.process is None or self.process.returncode is not None:
            raise NativeError("C++ core is not running")
        self.process.stdin.write((command + "\n").encode("ascii"))
        await self.process.stdin.drain()
        line = await self.process.stdout.readline()
        if not line:
            raise NativeError("C++ core exited before acknowledging command")
        try:
            return json.loads(line)
        except ValueError as exc:
            raise NativeError("invalid acknowledgement from C++ core") from exc

    async def request(self, command: str) -> dict:
        if "\n" in command or "\r" in command:
            raise NativeError("one request per line")
        async with self.lock:
            operation = asyncio.create_task(self._exchange(command))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                # Consume the outstanding reply before allowing the next request.
                # Otherwise RESET could accidentally receive the previous UPD ack.
                await operation
                raise

    async def __aexit__(self, *_):
        if self.process is None:
            return
        if self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
