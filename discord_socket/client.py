
import logging
import asyncio
import yarl

from typing import Optional, Coroutine
from discord_http import Client

from .intents import Intents
from .websocket import WebSocket

_log = logging.getLogger("discord_http")


class SocketClient:
    def __init__(
        self,
        bot: Client,
        intents: Intents,
        *,
        shard_id: Optional[int] = None,
        shard_count: Optional[int] = None,
        shard_ids: Optional[list[int]] = None,
        max_concurrency: Optional[int] = None,
        api_version: Optional[int] = 8
    ):
        self.bot = bot
        self.intents = intents

        self.api_version = api_version
        self.shard_id = shard_id
        self.shard_count = shard_count
        self.shard_ids = shard_ids
        self.max_concurrency = max_concurrency

        self.__shards: dict[int, WebSocket] = {}

    def get_shard(self, shard_id: int) -> Optional[WebSocket]:
        return self.__shards.get(shard_id, None)

    async def _fetch_gateway(self) -> tuple[int, int, yarl.URL]:
        r = await self.bot.state.query("GET", "/gateway/bot")

        return (
            r.response["shards"],
            r.response["session_start_limit"]["max_concurrency"],
            yarl.URL(r.response["url"])
        )

    async def _launch_shard(self, gateway: yarl.URL, shard_id: int) -> None:
        try:
            shard = WebSocket(
                bot=self.bot,
                intents=self.intents,
                gateway=gateway,
                shard_id=shard_id,
                shard_count=self.shard_count,
                api_version=self.api_version
            )
            shard.connect()
            while not shard._session_id:
                await asyncio.sleep(0.5)

        except Exception as e:
            _log.error("Error launching shard, trying again...", exc_info=e)
            return await self._launch_shard(gateway, shard_id)

        self.__shards[shard_id] = shard

    async def launch_shards(self) -> None:
        if self.shard_count is None:
            (
                self.shard_count,
                self.max_concurrency,
                gateway
            ) = await self._fetch_gateway()
        else:
            gateway = yarl.URL("wss://gateway.discord.gg/")

        shard_ids = self.shard_ids or range(self.shard_count)

        if not self.max_concurrency:
            for shard_id in shard_ids:
                await self._launch_shard(gateway, shard_id)

        else:
            chunks = [
                list(shard_ids[i:i + self.max_concurrency])
                for i in range(0, len(shard_ids), self.max_concurrency)
            ]

            for i, shard_chunk in enumerate(chunks, start=1):
                _booting: list[Coroutine] = [
                    self._launch_shard(gateway, shard_id)
                    for shard_id in shard_chunk
                ]

                _log.debug(f"Launching bucket {i}/{len(chunks)}")
                await asyncio.gather(*_booting)
                _log.debug(f"Bucket {i}/{len(chunks)} shards launched, waiting (5s/bucket)")
                await asyncio.sleep(5)

        _log.debug("All buckets/shards launched")

    def start(self) -> None:
        self.bot.loop.create_task(self.launch_shards())

    async def close(self) -> None:
        async def _close():
            to_close = [
                asyncio.ensure_future(shard.close())
                for shard in self.__shards.values()
            ]

            if to_close:
                await asyncio.wait(to_close)

        _task = asyncio.create_task(_close())
        await _task
