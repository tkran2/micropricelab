"""Unauthenticated public Coinbase market data. No trading/account endpoints."""
import json
from .pipeline import Config

PUBLIC_URL = "wss://advanced-trade-ws.coinbase.com"

class CoinbaseSource:
    def __init__(self, config: Config, url: str = PUBLIC_URL):
        self.config, self.url = config, url
        self.connection = None

    async def __aenter__(self):
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:
            raise RuntimeError("run make setup, then use .venv/bin/python for capture") from exc
        self.connection = await connect(self.url, max_size=self.config.max_frame_bytes,
                                        max_queue=1, compression=None,
                                        open_timeout=10, close_timeout=1,
                                        ping_interval=10, ping_timeout=10)
        try:
            for channel in ("level2", "heartbeats"):
                await self.connection.send(json.dumps({"type": "subscribe", "channel": channel,
                                                       "product_ids": [self.config.product]}))
        except BaseException:
            await self.connection.close()
            raise
        return self

    async def recv(self):
        return await self.connection.recv()

    async def __aexit__(self, *_):
        if self.connection is not None:
            await self.connection.close()
