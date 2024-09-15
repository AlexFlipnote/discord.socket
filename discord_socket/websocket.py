import asyncio
import json
import logging
import sys
import zlib
import time
import yarl
import websockets as ws

from typing import Optional, Union, Any
from discord_http import Client, utils

from .intents import Intents
from .parser import Parser

DEFAULT_GATEWAY = yarl.URL("wss://gateway.discord.gg/")
_log = logging.getLogger("discord_http")

__all__ = (
    "WebSocket",
    "PayloadType",
)


class PayloadType(utils.Enum):
    dispatch = 0
    heartbeat = 1
    identify = 2
    presence = 3
    voice_state = 4
    voice_ping = 5
    resume = 6
    reconnect = 7
    request_guild_members = 8
    invalidate_session = 9
    hello = 10
    heartbeat_ack = 11
    guild_sync = 12


class WebSocket:
    def __init__(
        self,
        bot: Client,
        intents: Intents,
        *,
        api_version: Optional[int] = 8
    ):
        self.bot = bot

        self.token = bot.token
        self.intents = intents
        self.api_version = api_version
        self.loop = asyncio.get_event_loop()

        self.ws: Optional[ws.WebSocketClientProtocol] = None
        self.parser = Parser(bot)

        self._connection = None

        self._buffer: bytearray = bytearray()
        self._zlib: zlib._Decompress = zlib.decompressobj()

        self._sequence: Optional[str] = None
        self._session_id: Optional[str] = None
        self._resume_url: Optional[str] = None
        self._heartbeat_interval: float = 41_250 / 1000  # 41.25 seconds
        self._last_ping: Optional[int] = None
        self._close_code: Optional[int] = None

        self.gateway = DEFAULT_GATEWAY

    @property
    def url(self) -> str:
        """ Returns the websocket url for the client """
        if not isinstance(self.api_version, int):
            raise TypeError("api_version must be of type int")

        return self.gateway.with_query(
            v=self.api_version,
            encoding="json",
            compress="zlib-stream"
        ).human_repr()

    async def send_message(self, message: Union[dict, PayloadType]) -> None:
        """ Sends a message to the websocket """
        if isinstance(message, PayloadType):
            message = self.payload(message)

        if not isinstance(message, dict):
            raise TypeError("message must be of type dict")

        _log.debug(f"Sending message: {message}")
        await self.ws.send(json.dumps(message))

    async def close(self, code: Optional[int] = 1000) -> None:
        """ Closes the websocket """
        code = code or 1000

        self._close_code = code
        await self.ws.close(code=code)

    def _can_handle_close(self) -> bool:
        code = self._close_code or self.ws.close_code
        return code not in (1000, 4004, 4010, 4011, 4012, 4013, 4014)

    async def reconnect(self) -> None:
        """ Reconnects the websocket """
        await self.ws.close()
        self.connect()

    async def received_message(self, msg: Any) -> None:
        if type(msg) is bytes:
            self._buffer.extend(msg)

            if len(msg) < 4 or msg[-4:] != b"\x00\x00\xff\xff":
                return None

            msg = self._zlib.decompress(self._buffer)
            msg = msg.decode("utf-8")
            self._buffer = bytearray()

        msg = json.loads(msg)

        event = msg.get("t", None)

        if event:
            await self.on_event(event, msg)

        op = msg.get("op", None)
        data = msg.get("d", None)
        seq = msg.get("s", None)

        if seq is not None:
            self._sequence = seq

        self._last_ping = int(time.time())

        if op != PayloadType.dispatch:
            match op:
                case PayloadType.reconnect:
                    await self.reconnect()
                    return

                case PayloadType.heartbeat_ack:
                    _log.debug("Heartbeat ack")
                    return

                case PayloadType.heartbeat:
                    await self.send_message(PayloadType.heartbeat)
                    return

                case PayloadType.hello:
                    self._heartbeat_interval = (
                        int(data["heartbeat_interval"]) / 1000
                    )
                    await self.send_message(PayloadType.identify)
                    return

                case PayloadType.invalidate_session:
                    if data is True:
                        self._sequence = None
                        self._session_id = None
                        self.gateway = DEFAULT_GATEWAY
                        await self.close()
                        raise Exception("Session invalidated")

                case _:
                    return

        match event:
            case "READY":
                self._sequence = msg["s"]
                self._session_id = data["session_id"]
                self._resume_url = data["resume_gateway_url"]
                _log.debug(f"Ready {self._session_id}")

            case "RESUMED":
                _log.debug("Resumed session")

            case _:
                pass

    async def _socket_manager(self) -> None:
        try:
            keep_waiting: bool = True

            async with ws.connect(self.url) as socket:
                self.ws = socket

                try:
                    while keep_waiting:
                        if (
                            not self._last_ping or
                            int(time.time()) - self._last_ping > 45
                        ):
                            await self.send_message(PayloadType.heartbeat)

                        try:
                            evt = await asyncio.wait_for(
                                self.ws.recv(),
                                timeout=self._heartbeat_interval
                            )

                        except asyncio.TimeoutError:
                            # No heartbeat received, send in case..
                            await self.send_message(PayloadType.heartbeat)

                        except asyncio.CancelledError:
                            await self.ws.ping()

                        else:
                            await self.received_message(evt)

                except Exception as e:
                    keep_waiting = False

                    if self._can_handle_close():
                        print(utils.traceback_maker(e))
                        _log.debug("Websocket closed, attempting reconnect")
                        await self.reconnect()

                    else:
                        _log.error("Error in websocket", exc_info=e)

        except Exception as e:
            _log.error("Error in websocket", exc_info=e)

    async def on_event(self, name: str, event: Any) -> None:
        new_name = name.lower()
        data: dict = event.get("d", {})

        if not data:
            return None

        match name:
            case "MESSAGE_CREATE" | "MESSAGE_UPDATE":
                event = self.parser.message_create(data)
                self.bot.dispatch(new_name, event)

            case "MESSAGE_DELETE":
                event = self.parser.message_delete(data)
                self.bot.dispatch(new_name, event)

            case _:
                self.bot.dispatch(new_name, event)

    def connect(self) -> None:
        """ Connect the websocket """
        self._connection = asyncio.ensure_future(
            self._socket_manager()
        )

    def payload(self, op: PayloadType) -> dict:
        """ Returns a payload for the websocket """
        if not isinstance(op, PayloadType):
            raise TypeError("op must be of type PayloadType")

        match op:

            case PayloadType.heartbeat:
                self._last_ping = int(time.time())
                return {
                    "op": op.value,
                    "d": self._heartbeat_interval
                }

            case PayloadType.hello:
                return {
                    "op": op.value,
                    "d": {
                        "heartbeat_interval": int(self._heartbeat_interval * 1000)
                    }
                }

            case PayloadType.resume:
                return {
                    "op": op.value,
                    "d": {
                        "seq": self._sequence,
                        "session_id": self._session_id,
                        "token": self.token,
                    }
                }

            case _:
                return {
                    "op": op.value,
                    "d": {
                        "token": self.token,
                        "intents": self.intents.value,
                        "properties": {
                            "os": sys.platform,
                            "browser": "discord.http",
                            "device": "discord.http"
                        },
                        "compress": True,
                        "large_threshold": 250,
                    }
                }
