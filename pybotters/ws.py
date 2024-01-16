from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import inspect
import logging
import random
import time
import uuid
from dataclasses import dataclass
from secrets import token_hex
from typing import Any, AsyncIterator, Generator, Optional, Union

import aiohttp
from aiohttp.http_websocket import json

from .auth import Auth as _Auth
from .typedefs import WsBytesHandler, WsJsonHandler, WsStrHandler

logger = logging.getLogger(__name__)


def pretty_modulename(e: Exception) -> str:
    modulename = e.__class__.__name__
    module = inspect.getmodule(e)
    if module:
        modulename = f"{module.__name__}.{modulename}"
    return modulename


class WebSocketApp:
    BACKOFF_MIN = 1.92
    BACKOFF_MAX = 60.0
    BACKOFF_FACTOR = 1.618
    BACKOFF_INITIAL = 5.0
    DEFAULT_BACKOFF = (BACKOFF_MIN, BACKOFF_MAX, BACKOFF_FACTOR, BACKOFF_INITIAL)

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        send_str: Optional[Union[str, list[str]]] = None,
        send_bytes: Optional[Union[bytes, list[bytes]]] = None,
        send_json: Optional[Union[dict, list[dict]]] = None,
        hdlr_str: Optional[Union[WsStrHandler, list[WsStrHandler]]] = None,
        hdlr_bytes: Optional[Union[WsBytesHandler, list[WsBytesHandler]]] = None,
        hdlr_json: Optional[Union[WsJsonHandler, list[WsJsonHandler]]] = None,
        backoff: tuple[float, float, float, float] = DEFAULT_BACKOFF,
        **kwargs: Any,
    ) -> None:
        self._session = session
        self._url = url

        self._loop = session._loop
        self._current_ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._event = asyncio.Event()

        if send_str is None:
            send_str = []
        elif isinstance(send_str, str):
            send_str = [send_str]

        if send_bytes is None:
            send_bytes = []
        elif isinstance(send_bytes, bytes):
            send_bytes = [send_bytes]

        if send_json is None:
            send_json = []
        elif isinstance(send_json, dict):
            send_json = [send_json]

        if hdlr_str is None:
            hdlr_str = []
        elif callable(hdlr_str):
            hdlr_str = [hdlr_str]

        if hdlr_bytes is None:
            hdlr_bytes = []
        elif callable(hdlr_bytes):
            hdlr_bytes = [hdlr_bytes]

        if hdlr_json is None:
            hdlr_json = []
        elif callable(hdlr_json):
            hdlr_json = [hdlr_json]

        self._task = asyncio.create_task(
            self._run_forever(
                send_str=send_str,
                send_bytes=send_bytes,
                send_json=send_json,
                hdlr_str=hdlr_str,
                hdlr_bytes=hdlr_bytes,
                hdlr_json=hdlr_json,
                backoff=backoff,
                **kwargs,
            )
        )

    @property
    def url(self) -> str:
        return self._url

    @url.setter
    def url(self, url: str) -> None:
        self._url = url

    @property
    def current_ws(self) -> ClientWebSocketResponse | None:
        return self._current_ws

    async def _run_forever(
        self,
        *,
        send_str: list[str],
        send_bytes: list[bytes],
        send_json: list[dict],
        hdlr_str: list[WsStrHandler],
        hdlr_bytes: list[WsBytesHandler],
        hdlr_json: list[WsJsonHandler],
        backoff: tuple[float, float, float, float],
        **kwargs: Any,
    ) -> None:
        BACKOFF_MIN, BACKOFF_MAX, BACKOFF_FACTOR, BACKOFF_INITIAL = backoff

        backoff_delay = BACKOFF_MIN
        while not self._session.closed:
            try:
                await self._ws_connect(
                    send_str=send_str,
                    send_bytes=send_bytes,
                    send_json=send_json,
                    hdlr_str=hdlr_str,
                    hdlr_bytes=hdlr_bytes,
                    hdlr_json=hdlr_json,
                    **kwargs,
                )
            # From https://github.com/python-websockets/websockets/blob/12.0/src/websockets/legacy/client.py#L600-L624  # noqa: E501
            # Licensed under the BSD-3-Clause
            except Exception as e:
                logger.warning(f"{pretty_modulename(e)}: {e}")
                if backoff_delay == BACKOFF_MIN:
                    initial_delay = random.random() * BACKOFF_INITIAL
                    await asyncio.sleep(initial_delay)
                else:
                    await asyncio.sleep(int(backoff_delay))
                backoff_delay = backoff_delay * BACKOFF_FACTOR
                backoff_delay = min(backoff_delay, BACKOFF_MAX)
            else:
                backoff_delay = BACKOFF_MIN
            # End https://github.com/python-websockets/websockets/blob/12.0/src/websockets/legacy/client.py#L600-L624  # noqa: E501
            finally:
                self._current_ws = None
                self._event.clear()

    async def _ws_connect(
        self,
        *,
        send_str: list[str],
        send_bytes: list[bytes],
        send_json: list[dict],
        hdlr_str: list[WsStrHandler],
        hdlr_bytes: list[WsBytesHandler],
        hdlr_json: list[WsJsonHandler],
        **kwargs: Any,
    ) -> None:
        async with self._session.ws_connect(self._url, **kwargs) as ws:
            self._current_ws = ws
            self._event.set()

            await self._ws_send(ws, send_str, send_bytes, send_json)

            await self._ws_receive(ws, hdlr_str, hdlr_bytes, hdlr_json)

    async def _ws_send(
        self,
        ws: ClientWebSocketResponse,
        send_str: list[str],
        send_bytes: list[bytes],
        send_json: list[dict],
    ) -> None:
        await asyncio.gather(
            *(ws.send_str(x) for x in send_str),
            *(ws.send_bytes(x) for x in send_bytes),
            *(ws.send_json(x) for x in send_json),
        )

    async def _ws_receive(
        self,
        ws: ClientWebSocketResponse,
        hdlr_str: list[WsStrHandler],
        hdlr_bytes: list[WsBytesHandler],
        hdlr_json: list[WsJsonHandler],
    ) -> None:
        async for msg in ws:
            self._loop.call_soon(
                self._onmessage, msg, ws, hdlr_str, hdlr_bytes, hdlr_json
            )

    def _onmessage(
        self,
        msg: aiohttp.WSMessage,
        ws: ClientWebSocketResponse,
        hdlr_str: list[WsStrHandler],
        hdlr_bytes: list[WsBytesHandler],
        hdlr_json: list[WsJsonHandler],
    ) -> None:
        if msg.type == aiohttp.WSMsgType.TEXT:
            for hdlr in hdlr_str:
                self._loop.call_soon(hdlr, msg.data, ws)

        if msg.type == aiohttp.WSMsgType.BINARY:
            for hdlr in hdlr_bytes:
                self._loop.call_soon(hdlr, msg.data, ws)

        if hdlr_json:
            try:
                data = msg.json()
            except json.JSONDecodeError as e:
                logger.warning(f"{pretty_modulename(e)}: {e} {e.doc}")
            else:
                for hdlr in hdlr_json:
                    self._loop.call_soon(hdlr, data, ws)

    async def wait(self) -> None:
        await self._task

    async def _wait_handshake(self) -> "WebSocketApp":
        await self._event.wait()
        return self

    def __await__(self) -> Generator[Any, None, "WebSocketApp"]:
        return self._wait_handshake().__await__()


class WebSocketQueue(asyncio.Queue):
    def onmessage(self, msg: Any, ws: aiohttp.ClientWebSocketResponse):
        self.put_nowait(msg)

    async def __aiter__(self) -> AsyncIterator[Any]:
        while True:
            yield await self.get()


class Heartbeat:
    @staticmethod
    async def bybit(ws: aiohttp.ClientWebSocketResponse):
        while not ws.closed:
            await ws.send_str('{"op":"ping"}')
            await asyncio.sleep(30.0)

    @staticmethod
    async def bitbank(ws: aiohttp.ClientWebSocketResponse):
        while not ws.closed:
            await ws.send_str("2")
            await asyncio.sleep(15.0)

    @staticmethod
    async def binance(ws: aiohttp.ClientWebSocketResponse):
        while not ws.closed:
            await ws.pong()
            await asyncio.sleep(60.0)

    @staticmethod
    async def phemex(ws: aiohttp.ClientWebSocketResponse):
        while not ws.closed:
            await ws.send_str('{"method":"server.ping","params":[],"id":123}')
            await asyncio.sleep(10.0)

    @staticmethod
    async def okx(ws: aiohttp.ClientWebSocketResponse):
        while not ws.closed:
            await ws.send_str("ping")
            await asyncio.sleep(15.0)

    @staticmethod
    async def bitget(ws: aiohttp.ClientWebSocketResponse):
        while not ws.closed:
            await ws.send_str("ping")
            # Refer to official SDK
            # https://github.com/BitgetLimited/v3-bitget-api-sdk/blob/09179123a62cf2a63ea1cfbb289b85e3a40018f8/bitget-python-sdk-api/bitget/ws/bitget_ws_client.py#L58
            await asyncio.sleep(25.0)

    @staticmethod
    async def mexc(ws: aiohttp.ClientWebSocketResponse):
        while not ws.closed:
            await ws.send_str('{"method":"ping"}')
            await asyncio.sleep(10.0)

    @staticmethod
    async def kucoin(ws: aiohttp.ClientWebSocketResponse):
        while not ws.closed:
            await ws.send_str(f'{{"id": "{uuid.uuid4()}", "type": "ping"}}')
            await asyncio.sleep(15)


class Auth:
    @staticmethod
    async def bybit(ws: aiohttp.ClientWebSocketResponse):
        if ("public" in ws._response.url.path) or (
            ws._response.url.path.startswith("/spot/quote")  # for spot v1 only
        ):
            return

        key: str = ws._response._session.__dict__["_apis"][
            AuthHosts.items[ws._response.url.host].name
        ][0]
        secret: bytes = ws._response._session.__dict__["_apis"][
            AuthHosts.items[ws._response.url.host].name
        ][1]

        expires = int((time.time() + 5.0) * 1000)
        path = f"GET/realtime{expires}"
        signature = hmac.new(
            secret, path.encode(), digestmod=hashlib.sha256
        ).hexdigest()

        await ws.send_json(
            {"op": "auth", "args": [key, expires, signature]},
            _itself=True,
        )
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = msg.json()
                if "success" in data:  # for almost all
                    if data["success"]:
                        break
                    else:
                        logger.warning(data)
                else:  # for spot v1 only
                    if "auth" in data:
                        break
                    elif "code" in data:
                        logger.warning(data)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break

    @staticmethod
    async def bitflyer(ws: aiohttp.ClientWebSocketResponse):
        key: str = ws._response._session.__dict__["_apis"][
            AuthHosts.items[ws._response.url.host].name
        ][0]
        secret: bytes = ws._response._session.__dict__["_apis"][
            AuthHosts.items[ws._response.url.host].name
        ][1]

        timestamp = int(time.time() * 1000)
        nonce = token_hex(16)
        sign = hmac.new(
            secret, f"{timestamp}{nonce}".encode(), digestmod=hashlib.sha256
        ).hexdigest()
        await ws.send_json(
            {
                "method": "auth",
                "params": {
                    "api_key": key,
                    "timestamp": timestamp,
                    "nonce": nonce,
                    "signature": sign,
                },
                "id": "auth",
            },
            _itself=True,
        )
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = msg.json()
                if "error" in data:
                    logger.warning(data)
                if "id" in data:
                    if data["id"] == "auth":
                        break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break

    @staticmethod
    async def phemex(ws: aiohttp.ClientWebSocketResponse):
        key: str = ws._response._session.__dict__["_apis"][
            AuthHosts.items[ws._response.url.host].name
        ][0]
        secret: bytes = ws._response._session.__dict__["_apis"][
            AuthHosts.items[ws._response.url.host].name
        ][1]

        expiry = int(time.time() + 60.0)
        signature = hmac.new(
            secret, f"{key}{expiry}".encode(), digestmod=hashlib.sha256
        ).hexdigest()
        msg = {
            "method": "user.auth",
            "params": ["API", key, signature, expiry],
            "id": 123,
        }
        await ws.send_json(msg, _itself=True)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = msg.json()
                if "error" in data:
                    if data["error"] is not None:
                        logger.warning(data)
                if data["result"] == {"status": "success"}:
                    break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break

    @staticmethod
    async def okx(ws: aiohttp.ClientWebSocketResponse):
        if ws._response.url.path.endswith("public"):
            return

        key: str = ws._response._session.__dict__["_apis"][
            AuthHosts.items[ws._response.url.host].name
        ][0]
        secret: bytes = ws._response._session.__dict__["_apis"][
            AuthHosts.items[ws._response.url.host].name
        ][1]
        passphrase: bytes = ws._response._session.__dict__["_apis"][
            AuthHosts.items[ws._response.url.host].name
        ][2]

        timestamp = str(int(time.time()))
        text = f"{timestamp}GET/users/self/verify"
        sign = base64.b64encode(
            hmac.new(secret, text.encode(), digestmod=hashlib.sha256).digest()
        ).decode()
        msg = {
            "op": "login",
            "args": [
                {
                    "apiKey": key,
                    "passphrase": passphrase,
                    "timestamp": timestamp,
                    "sign": sign,
                }
            ],
        }
        await ws.send_json(msg, _itself=True)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = msg.json()
                    if data["event"] == "error":
                        logger.warning(data)
                    if data["event"] == "login":
                        break
                except json.JSONDecodeError:
                    pass
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break

    @staticmethod
    async def bitget(ws: aiohttp.ClientWebSocketResponse):
        key: str = ws._response._session.__dict__["_apis"][
            AuthHosts.items[ws._response.url.host].name
        ][0]
        secret: bytes = ws._response._session.__dict__["_apis"][
            AuthHosts.items[ws._response.url.host].name
        ][1]
        passphrase: bytes = ws._response._session.__dict__["_apis"][
            AuthHosts.items[ws._response.url.host].name
        ][2]

        timestamp = int(round(time.time()))
        sign = base64.b64encode(
            hmac.new(
                secret, f"{timestamp}GET/user/verify".encode(), digestmod=hashlib.sha256
            ).digest()
        ).decode()
        msg = {
            "op": "login",
            "args": [
                {
                    "api_key": key,
                    "passphrase": passphrase,
                    "timestamp": str(timestamp),
                    "sign": sign,
                }
            ],
        }
        await ws.send_json(msg, _itself=True)
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = msg.json()
                except json.JSONDecodeError:
                    pass
                else:
                    if "event" in data:
                        if data["event"] == "login":
                            break
                        elif data["event"] == "error":
                            logger.warning(data)
                            break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break

    @staticmethod
    async def mexc(ws: aiohttp.ClientWebSocketResponse):
        key: str = ws._response._session.__dict__["_apis"][
            AuthHosts.items[ws._response.url.host].name
        ][0]
        secret: bytes = ws._response._session.__dict__["_apis"][
            AuthHosts.items[ws._response.url.host].name
        ][1]

        timestamp = str(int(time.time()))
        sign = hmac.new(
            secret, f"{key}{timestamp}".encode(), digestmod=hashlib.sha256
        ).hexdigest()

        msg = {
            "method": "login",
            "param": {
                "apiKey": key,
                "reqTime": timestamp,
                "signature": sign,
            },
        }
        await ws.send_json(msg, _itself=True)

    @staticmethod
    async def kucoin(ws: aiohttp.ClientWebSocketResponse):
        # Endpointの取得時点で行われるのでここでは不要
        pass


@dataclass
class Item:
    name: str
    func: Any


class HeartbeatHosts:
    items = {
        "stream.bitbank.cc": Heartbeat.bitbank,
        "stream.bybit.com": Heartbeat.bybit,
        "stream.bytick.com": Heartbeat.bybit,
        "stream-testnet.bybit.com": Heartbeat.bybit,
        "stream.binance.com": Heartbeat.binance,
        "fstream.binance.com": Heartbeat.binance,
        "dstream.binance.com": Heartbeat.binance,
        "vstream.binance.com": Heartbeat.binance,
        "stream.binancefuture.com": Heartbeat.binance,
        "dstream.binancefuture.com": Heartbeat.binance,
        "testnet.binanceops.com": Heartbeat.binance,
        "testnetws.binanceops.com": Heartbeat.binance,
        "phemex.com": Heartbeat.phemex,
        "api.phemex.com": Heartbeat.phemex,
        "vapi.phemex.com": Heartbeat.phemex,
        "testnet.phemex.com": Heartbeat.phemex,
        "testnet-api.phemex.com": Heartbeat.phemex,
        "ws.okx.com": Heartbeat.okx,
        "wsaws.okx.com": Heartbeat.okx,
        "wspap.okx.com": Heartbeat.okx,
        "ws.bitget.com": Heartbeat.bitget,
        "contract.mexc.com": Heartbeat.mexc,
        "ws-api.kucoin.com": Heartbeat.kucoin,
    }


class AuthHosts:
    items = {
        "stream.bybit.com": Item("bybit", Auth.bybit),
        "stream.bytick.com": Item("bybit", Auth.bybit),
        "stream-testnet.bybit.com": Item("bybit_testnet", Auth.bybit),
        "ws.lightstream.bitflyer.com": Item("bitflyer", Auth.bitflyer),
        "phemex.com": Item("phemex", Auth.phemex),
        "api.phemex.com": Item("phemex", Auth.phemex),
        "vapi.phemex.com": Item("phemex", Auth.phemex),
        "testnet.phemex.com": Item("phemex_testnet", Auth.phemex),
        "testnet-api.phemex.com": Item("phemex_testnet", Auth.phemex),
        "ws.okx.com": Item("okx", Auth.okx),
        "wsaws.okx.com": Item("okx", Auth.okx),
        "wspap.okx.com": Item("okx_demo", Auth.okx),
        "ws.bitget.com": Item("bitget", Auth.bitget),
        "contract.mexc.com": Item("mexc", Auth.mexc),
    }


class ClientWebSocketResponse(aiohttp.ClientWebSocketResponse):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if self._response.url.host in HeartbeatHosts.items:
            self.__dict__["_pingtask"] = asyncio.create_task(
                HeartbeatHosts.items[self._response.url.host](self)
            )
        if self._response.__dict__["_auth"] is _Auth:
            if self._response.url.host in AuthHosts.items:
                if (
                    AuthHosts.items[self._response.url.host].name
                    in self._response._session.__dict__["_apis"]
                ):
                    self.__dict__["_authtask"] = asyncio.create_task(
                        AuthHosts.items[self._response.url.host].func(self)
                    )
        self._lock = asyncio.Lock()

    async def _wait_authtask(self):
        if "_authtask" in self.__dict__:
            await self.__dict__["_authtask"]

    async def send_str(self, *args, **kwargs) -> None:
        if self._response.url.host not in RequestLimitHosts.items:
            await super().send_str(*args, **kwargs)
        else:
            super_send_str = super().send_str(*args, **kwargs)
            await RequestLimitHosts.items[self._response.url.host](self, super_send_str)

    async def send_json(self, *args, **kwargs) -> None:
        _itself = kwargs.pop("_itself", False)
        if not _itself:
            await self._wait_authtask()

        if (
            (kwargs.pop("auth", _Auth) is _Auth)
            and (self._response.url.host in MessageSignHosts.items)
            and (
                MessageSignHosts.items[self._response.url.host].name
                in self._response._session.__dict__["_apis"]
            )
        ):
            data = kwargs.get("data", args[0] if len(args) > 0 else None)
            if data:
                MessageSignHosts.items[self._response.url.host].func(self, data)

        return await super().send_json(*args, **kwargs)


class RequestLimit:
    @staticmethod
    async def gmocoin(ws: ClientWebSocketResponse, send_str):
        async with ws._lock:
            await send_str
            r = await ws._response._session.get(
                "https://api.coin.z.com/public/v1/status", auth=_Auth
            )
            data = await r.json()
            before = datetime.datetime.fromisoformat(data["responsetime"][:-1])
            while True:
                await asyncio.sleep(1.0)
                r = await ws._response._session.get(
                    "https://api.coin.z.com/public/v1/status", auth=_Auth
                )
                data = await r.json()
                after = datetime.datetime.fromisoformat(data["responsetime"][:-1])
                delta = after - before
                if delta.total_seconds() >= 1.0:
                    break

    @staticmethod
    async def binance(ws: ClientWebSocketResponse, send_str):
        async with ws._lock:
            await send_str
            r = await ws._response._session.get(
                "https://api.binance.com/api/v3/time", auth=None
            )
            data = await r.json()
            before = datetime.datetime.fromtimestamp(data["serverTime"] / 1000)
            while True:
                await asyncio.sleep(0.25)  # limit of 5 incoming messages per second
                r = await ws._response._session.get(
                    "https://api.binance.com/api/v3/time", auth=None
                )
                data = await r.json()
                after = datetime.datetime.fromtimestamp(data["serverTime"] / 1000)
                delta = after - before
                if delta.total_seconds() > 0.25:
                    break


class RequestLimitHosts:
    items = {
        "api.coin.z.com": RequestLimit.gmocoin,
        "stream.binance.com": RequestLimit.binance,
    }


class MessageSign:
    @staticmethod
    def binance(ws: aiohttp.ClientWebSocketResponse, data: dict[str, Any]):
        key: str = ws._response._session.__dict__["_apis"][
            MessageSignHosts.items[ws._response.url.host].name
        ][0]
        secret: bytes = ws._response._session.__dict__["_apis"][
            MessageSignHosts.items[ws._response.url.host].name
        ][1]

        if not ws._response.url.path.startswith("/ws-api"):
            return

        params = data.get("params")

        if not isinstance(params, dict):
            return

        timestamp = int(time.time() * 1000)

        params["apiKey"] = key
        params["timestamp"] = timestamp

        payload = "&".join(
            [f"{param}={value}" for param, value in sorted(params.items())]
        )
        signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()

        params["signature"] = signature


class MessageSignHosts:
    items = {
        "ws-api.binance.com": Item("binance", MessageSign.binance),
        "testnet.binance.vision": Item("binance_testnet", MessageSign.binance),
    }
