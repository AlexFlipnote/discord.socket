import asyncio
import json
import logging
import sys
import zlib
import time
import yarl
import websockets as ws

from datetime import datetime, UTC
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


class Status:
    def __init__(self, shard_id: int):
        self.shard_id = shard_id

        self.sequence: Optional[int] = None
        self.session_id: Optional[str] = None
        self.gateway = DEFAULT_GATEWAY

        self.latency: float = float("inf")
        self._last_ack: float = time.perf_counter()
        self._last_send: float = time.perf_counter()
        self._last_recv: float = time.perf_counter()
        self._last_heartbeat: Optional[float] = None

    @property
    def ping(self) -> float:
        return self._last_recv - self._last_send

    def reset(self) -> None:
        self.sequence = None
        self.session_id = None
        self.gateway = DEFAULT_GATEWAY

    def can_resume(self) -> bool:
        return self.session_id is not None

    def update_sequence(self, sequence: int) -> None:
        self.sequence = sequence

    def update_ready_data(self, data: dict) -> None:
        self.session_id = data["session_id"]
        self.gateway = yarl.URL(data["resume_gateway_url"])

    def get_payload(self) -> dict:
        return {
            "op": int(PayloadType.heartbeat),
            "d": self.sequence
        }

    def update_send(self) -> None:
        self._last_send = time.perf_counter()

    def update_heartbeat(self) -> None:
        self._last_heartbeat = time.perf_counter()

    def tick(self) -> None:
        self._last_recv = time.perf_counter()

    def ack(self) -> None:
        ack_time = time.perf_counter()
        self._last_ack = ack_time
        self.latency = ack_time - self._last_send
        if self.latency > 10:
            _log.warning(f"Shard {self.shard_id} latency is {self.latency:.2f}s behind")


class WebSocket:
    def __init__(
        self,
        bot: Client,
        intents: Intents,
        shard_id: int,
        *,
        shard_count: Optional[int] = None,
        raw_events: bool = False,
        api_version: Optional[int] = 8
    ):
        self.bot = bot

        self.intents = intents

        self.api_version = api_version
        self.shard_id = shard_id
        self.shard_count = shard_count
        self.raw_events = raw_events

        self.ws: Optional[ws.WebSocketClientProtocol] = None

        self.parser = Parser(bot)
        self.status = Status(shard_id)

        self._connection = None

        self._buffer: bytearray = bytearray()
        self._zlib: zlib._Decompress = zlib.decompressobj()

        self._heartbeat_interval: float = 41_250 / 1000  # 41.25 seconds
        self._close_code: Optional[int] = None
        self._last_activity: datetime = datetime.now(UTC)

    @property
    def url(self) -> str:
        """ Returns the websocket url for the client """
        if not isinstance(self.api_version, int):
            raise TypeError("api_version must be of type int")

        return self.status.gateway.with_query(
            v=self.api_version,
            encoding="json",
            compress="zlib-stream"
        ).human_repr()

    def _reset_buffer(self) -> None:
        self._buffer = bytearray()
        self._zlib = zlib.decompressobj()

    def _reset_instance(self) -> None:
        self._reset_buffer()
        self.status.reset()

    def _can_handle_close(self) -> bool:
        code = self._close_code or self.ws.close_code
        return code not in (1000, 4004, 4010, 4011, 4012, 4013, 4014)

    def _was_normal_close(self) -> bool:
        code = self._close_code or self.ws.close_code
        return code == 1000

    async def send_message(self, message: Union[dict, PayloadType]) -> None:
        """ Sends a message to the websocket """
        if isinstance(message, PayloadType):
            message = self.payload(message)

        if not isinstance(message, dict):
            raise TypeError("message must be of type dict")

        _log.debug(f"Sending message: {message}")
        await self.ws.send(json.dumps(message))

        self._last_activity = datetime.now(UTC)
        self.status.update_send()

    async def close(self, code: Optional[int] = 1000) -> None:
        """ Closes the websocket for good, or forcefully """
        code = code or 1000
        self._close_code = code
        await self.ws.close(code=code)

    async def received_message(self, msg: Any) -> None:
        self._last_activity = datetime.now(UTC)

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
            self.status.update_sequence(seq)

        self.status.tick()

        if op != PayloadType.dispatch:
            match op:
                case PayloadType.reconnect:
                    _log.debug(f"Shard {self.shard_id} got requrested to reconnect")
                    await self.close(code=1013)  # 1013 = Try again later
                    return

                case PayloadType.heartbeat_ack:
                    self.status.ack()
                    _log.debug(f"Shard {self.shard_id} heartbeat ACK")
                    return

                case PayloadType.heartbeat:
                    _log.debug(f"Shard {self.shard_id} heartbeat from event-case")
                    await self.send_message(PayloadType.heartbeat)
                    return

                case PayloadType.hello:
                    self._heartbeat_interval = (
                        int(data["heartbeat_interval"]) / 1000
                    )

                    if self.status.can_resume():
                        _log.debug(f"Shard {self.shard_id} resuming session")
                        await self.send_message(PayloadType.resume)
                    else:
                        _log.debug(f"Shard {self.shard_id} identifying...")
                        await self.send_message(PayloadType.identify)

                    return

                case PayloadType.invalidate_session:
                    self._reset_instance()

                    if data is True:
                        _log.error(f"Shard {self.shard_id} session invalidated, not attempting reboot...")
                        # TODO: Add a way to kill shard maybe?

                    elif data is False:
                        _log.warning(f"Shard {self.shard_id} session invalidated, resetting instance")

                    await self.close()

                case _:
                    return

        match event:
            case "READY":
                self.status.update_sequence(msg["s"])
                self.status.update_ready_data(data)  # type: ignore
                _log.info(f"Shard {self.shard_id} ready")

            case "RESUMED":
                _log.info(f"Shard {self.shard_id} resumed")

            case _:
                pass

    async def _socket_manager(self) -> None:
        try:
            keep_waiting: bool = True
            self._reset_buffer()

            async with ws.connect(self.url) as socket:
                self.ws = socket

                try:
                    while keep_waiting:
                        if (
                            not self.status._last_heartbeat or
                            time.perf_counter() - self.status._last_heartbeat > self._heartbeat_interval
                        ):
                            _log.debug(f"Shard {self.shard_id} heartbeat from if-case")
                            await self.send_message(PayloadType.heartbeat)

                        try:
                            evt = await asyncio.wait_for(
                                self.ws.recv(),
                                timeout=self._heartbeat_interval
                            )

                        except asyncio.TimeoutError:
                            # No event received, send in case..
                            _log.debug(f"Shard {self.shard_id} heartbeat from except-case")
                            await self.send_message(PayloadType.heartbeat)

                        except asyncio.CancelledError:
                            await self.ws.ping()

                        else:
                            await self.received_message(evt)

                except Exception as e:
                    keep_waiting = False

                    if self._can_handle_close():
                        self._reset_buffer()
                        _log.warning(f"Shard {self.shard_id} closed, attempting reconnect")

                    else:  # Something went wrong, reset the instance
                        self._reset_instance()
                        if self._was_normal_close():
                            # Possibly Discord closed the connection due to load balancing
                            _log.warning(f"Shard {self.shard_id} closed, attempting new connection")
                        else:
                            _log.error(f"Shard {self.shard_id} crashed", exc_info=e)

                    self.connect()

        except Exception as e:
            self._reset_instance()
            _log.error(f"Shard {self.shard_id} crashed completly", exc_info=e)

    async def on_event(self, name: str, event: Any) -> None:
        new_name = name.lower()
        data: dict = event.get("d", {})

        if not data:
            return None

        if self.raw_events:
            self.bot.dispatch("raw_socket_received", event)
            return

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
                self.status.update_heartbeat()
                return self.status.get_payload()

            case PayloadType.hello:
                return {
                    "op": int(op),
                    "d": {
                        "heartbeat_interval": int(self._heartbeat_interval * 1000)
                    }
                }

            case PayloadType.resume:
                return {
                    "op": int(op),
                    "d": {
                        "seq": self.status.sequence,
                        "session_id": self.status.session_id,
                        "token": self.bot.token,
                    }
                }

            case _:
                payload = {
                    "op": int(op),
                    "d": {
                        "token": self.bot.token,
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

                if self.shard_count is not None:
                    payload["d"]["shard"] = [self.shard_id, self.shard_count]

                return payload
