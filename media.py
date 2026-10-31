"""SDK-only QQ avatar URL download with DNS validation and pinned TCP connections.

No proxy, redirect, user URL or local path is accepted. TLS SNI/certificate checks
still use the original allowed hostname; only the TCP endpoint is a validated IP.
"""

from collections import OrderedDict
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import asyncio
import ipaddress
import socket
import time

import httpcore

from .client import BridgeError, image_kind
from .config import AvatarSettings, format_bytes


def avatar_url(url: str, target: str) -> str:
    try:
        if not isinstance(target, str) or not 5 <= len(target) <= 20 or not target.isascii() or not target.isdigit():
            raise ValueError
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if (
            len(url) > 2048
            or any(char.isspace() for char in url)
            or "\\" in url
            or parsed.scheme != "https"
            or parsed.netloc != "q1.qlogo.cn"
            or parsed.path != "/g"
            or parsed.fragment
            or set(query) != {"b", "nk", "s"}
            or query["b"] != ["qq"]
            or query["nk"] != [target]
            or len(query["s"]) != 1
            or query["s"][0] not in {"40", "100", "140", "640"}
        ):
            raise ValueError
        return f"https://q1.qlogo.cn/g?b=qq&nk={target}&s={query['s'][0]}"
    except (ValueError, TypeError):
        raise BridgeError("SDK 头像地址不符合已支持的官方 HTTPS 头像来源，已拒绝下载。") from None


class PublicBackend(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != "q1.qlogo.cn" or port != 443:
            raise BridgeError("头像连接目标不在允许范围内。")
        records = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=timeout or 8,
        )
        addresses = [record[4][0] for record in records]
        if not addresses or any(
            not ipaddress.ip_address(address).is_global
            or ipaddress.ip_address(address).is_multicast
            or ipaddress.ip_address(address).is_reserved
            for address in addresses
        ):
            raise BridgeError("头像 DNS 解析包含非公网地址，已拒绝。")
        # Pass the literal IP, not the hostname, to prevent a second DNS lookup/rebinding.
        return await self.backend.connect_tcp(addresses[0], port, timeout, local_address, socket_options)

    async def connect_unix_socket(self, *args, **kwargs):
        raise BridgeError("头像下载禁止本地套接字。")


class AvatarDownloader:
    def __init__(
        self, settings: AvatarSettings, max_bytes: int, pool: Optional[httpcore.AsyncConnectionPool] = None, clock=None
    ):
        self.settings = settings
        self.max_bytes = max_bytes
        self.pool = (
            pool
            if pool is not None
            else httpcore.AsyncConnectionPool(
                max_connections=settings.concurrency,
                max_keepalive_connections=0,
                retries=0,
                network_backend=PublicBackend(),
            )
        )
        self._slots = asyncio.Semaphore(settings.concurrency)
        self._cache = OrderedDict()
        self._bytes = 0
        self._clock = clock or time.monotonic
        self._closed = False

    def purge_expired(self):
        now = self._clock()
        # Atomic in this event loop, including accounting; no await/get race.
        for key, (expires, data) in list(self._cache.items()):
            if expires <= now:
                self._bytes -= len(data)
                del self._cache[key]

    async def close(self):
        self._closed = True
        self._cache.clear()
        self._bytes = 0
        await self.pool.aclose()

    async def get(self, url: str, target: str) -> bytes:
        canonical = avatar_url(url, target)
        try:
            return await asyncio.wait_for(self._get(canonical), self.settings.timeout)
        except (asyncio.TimeoutError, httpcore.TimeoutException):
            raise BridgeError("头像获取超时，请重试或直接附图。") from None
        except (httpcore.NetworkError, httpcore.ProtocolError, OSError):
            raise BridgeError("官方头像下载失败，请直接附图。") from None

    async def _get(self, url: str) -> bytes:
        async with self._slots:
            if self._closed:
                raise BridgeError("头像下载器已关闭。")
            self.purge_expired()
            if url in self._cache:
                self._cache.move_to_end(url)
                return self._cache[url][1]
            async with self.pool.stream(
                "GET",
                url,
                headers=[(b"Accept-Encoding", b"identity")],
                extensions={
                    "timeout": {
                        "connect": self.settings.timeout,
                        "read": self.settings.timeout,
                        "write": self.settings.timeout,
                        "pool": self.settings.timeout,
                    },
                },
            ) as response:
                headers = {key.lower(): value for key, value in response.headers}
                if response.status != 200:
                    raise BridgeError("头像服务响应失败或重定向，已拒绝；请直接附图。")
                if headers.get(b"content-encoding", b"identity").lower() != b"identity":
                    raise BridgeError("头像服务返回压缩传输，已拒绝。")
                length = headers.get(b"content-length", b"")
                if length and (not length.isdigit() or int(length) > self.max_bytes):
                    raise BridgeError(f"头像大小超过输入限制（上限 {format_bytes(self.max_bytes)}）。")
                result = bytearray()
                async for chunk in response.aiter_stream():
                    if len(result) + len(chunk) > self.max_bytes:
                        raise BridgeError(f"头像大小超过输入限制（上限 {format_bytes(self.max_bytes)}）。")
                    result.extend(chunk)
            data = bytes(result)
            image_kind(data)
            if self._closed:
                raise BridgeError("头像下载器已关闭。")
            if self.settings.cache_entries and len(data) <= self.settings.cache_bytes:
                if url in self._cache:
                    _, replaced = self._cache.pop(url)
                    self._bytes -= len(replaced)
                while self._cache and (
                    len(self._cache) >= self.settings.cache_entries
                    or self._bytes + len(data) > self.settings.cache_bytes
                ):
                    _, (_, removed) = self._cache.popitem(last=False)
                    self._bytes -= len(removed)
                self._cache[url] = (self._clock() + self.settings.cache_seconds, data)
                self._bytes += len(data)
            return data
