"""Restricted media requests: exact DNS suffixes, public IPs and pinned connections."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx


class UnsafeURL(ValueError):
    pass


class MediaTooLarge(ValueError):
    pass


def _extra(name: str) -> tuple[str, ...]:
    # Entries are complete DNS suffixes, never keywords or wildcard patterns.
    entries = tuple(x.strip().lower().rstrip(".") for x in os.getenv(name, "").split(",") if x.strip())
    for entry in entries:
        if "." not in entry or any(c in entry for c in "/:*@ "):
            raise ValueError(f"{name} must contain complete domains")
    return entries


PLATFORM_HOSTS = {
    "douyin": (
        "douyin.com", "iesdouyin.com", "douyinvod.com", "douyinpic.com", "douyincdn.com",
        "bytedance.com", "bytedance.net", "bytedanceapi.com", "bytecdn.com", "bytecdn.cn",
        "byteimg.com", "byteimg.cn", "ibytedtos.com", "ibyteimg.com", "pstatp.com",
        "toutiao.com", "toutiaoimg.com", "toutiaovod.com", "zjcdn.com", "zjcdn.cn",
    ) + _extra("EXTRA_DOUYIN_HOSTS"),
    "bilibili": (
        "bilibili.com", "b23.tv", "bilivideo.com", "bilivideo.cn", "hdslb.com", "akamaized.net",
    ) + _extra("EXTRA_BILIBILI_HOSTS"),
}


def platform_of_url(value: str) -> str | None:
    try:
        url = httpx.URL(value)
        if url.scheme not in ("http", "https") or url.userinfo:
            return None
        if url.port not in (None, 80 if url.scheme == "http" else 443):
            return None
        host = url.host.lower().rstrip(".")
        for platform, suffixes in PLATFORM_HOSTS.items():
            if any(host == suffix or host.endswith("." + suffix) for suffix in suffixes):
                return platform
    except (httpx.InvalidURL, ValueError):
        pass
    return None


async def public_addresses(host: str, port: int) -> list[str]:
    try:
        records = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 10
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise UnsafeURL("媒体域名解析失败") from exc
    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise UnsafeURL("媒体域名没有可用地址")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        mapped = getattr(ip, "ipv4_mapped", None)
        if (not ip.is_global or ip.is_multicast or ip.is_reserved
                or (mapped is not None and (not mapped.is_global or mapped.is_multicast or mapped.is_reserved))):
            raise UnsafeURL("媒体地址指向非公网网络")
    return addresses


class PublicMediaTransport(httpx.AsyncHTTPTransport):
    def __init__(self):
        super().__init__()
        # Pools are separated by hostname so a shared CDN IP cannot reuse a
        # TLS connection whose certificate was checked for another hostname.
        self._hosts = {}

    async def aclose(self):
        for transport in self._hosts.values():
            await transport.aclose()
        await super().aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if platform_of_url(str(request.url)) is None:
            raise UnsafeURL("媒体地址不在域名白名单内")
        host = request.url.host.rstrip(".")
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        addresses = await public_addresses(host, port)
        # The socket connects to an already validated IP. Host and TLS SNI keep
        # their original name, including certificate verification.
        headers = request.headers.copy()
        headers["Host"] = host
        extensions = dict(request.extensions, sni_hostname=host)
        if host not in self._hosts:
            self._hosts[host] = httpx.AsyncHTTPTransport()
        transport = self._hosts[host]
        last_error = None
        for address in addresses:
            pinned = httpx.Request(
                request.method, request.url.copy_with(host=address), headers=headers,
                stream=request.stream, extensions=extensions,
            )
            try:
                return await transport.handle_async_request(pinned)
            except httpx.ConnectError as exc:
                last_error = exc
        raise last_error  # type: ignore[misc]


def media_client(**kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=PublicMediaTransport(), trust_env=False, follow_redirects=False, **kwargs
    )


async def open_media(client: httpx.AsyncClient, url: str, *, headers=None) -> httpx.Response:
    """Open a streaming GET. The caller owns the returned response."""
    platform = platform_of_url(url)
    if platform is None:
        raise UnsafeURL("媒体地址不在域名白名单内")
    current = httpx.URL(url)
    for _ in range(6):
        if platform_of_url(str(current)) != platform:
            raise UnsafeURL("媒体跳转目标不在当前平台白名单内")
        response = await client.send(client.build_request("GET", current, headers=headers),
                                     stream=True, follow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("location")
        await response.aclose()
        if not location:
            raise UnsafeURL("媒体跳转缺少目标地址")
        try:
            current = current.join(location)
        except httpx.InvalidURL as exc:
            raise UnsafeURL("媒体跳转地址无效") from exc
    raise UnsafeURL("媒体跳转次数过多")


async def short_response(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """Read one short-link hop without buffering a large landing-page body."""
    response = await client.send(client.build_request("GET", url), stream=True, follow_redirects=False)
    try:
        body = bytearray()
        if response.status_code not in (301, 302, 303, 307, 308):
            async for chunk in limited_chunks(response, 65536):
                body.extend(chunk)
        return httpx.Response(response.status_code, headers=response.headers,
                              content=bytes(body), request=response.request)
    finally:
        await response.aclose()


@asynccontextmanager
async def media_stream(client: httpx.AsyncClient, url: str, *, headers=None):
    response = await open_media(client, url, headers=headers)
    try:
        yield response
    finally:
        await response.aclose()


def check_length(response: httpx.Response, limit: int) -> None:
    try:
        length = int(response.headers.get("content-length", "0"))
    except ValueError:
        length = 0
    if length > limit:
        raise MediaTooLarge("媒体文件超过服务器大小限制")


async def limited_chunks(response: httpx.Response, limit: int, *, raw=False):
    check_length(response, limit)
    size = 0
    iterator = response.aiter_raw(1 << 16) if raw else response.aiter_bytes(1 << 16)
    async for chunk in iterator:
        size += len(chunk)
        if size > limit:
            raise MediaTooLarge("媒体文件超过服务器大小限制")
        yield chunk


async def save_media(response: httpx.Response, destination: Path, limit: int) -> int:
    size = 0
    with destination.open("wb") as output:
        async for chunk in limited_chunks(response, limit):
            output.write(chunk)
            size += len(chunk)
    return size
