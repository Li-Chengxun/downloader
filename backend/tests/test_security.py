import asyncio
import ipaddress
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import media_http
import resource_limits as limits
import sessions
import parser as douyin
import bilibili
from prepared_files import PreparedFiles, PreparedCapacityExceeded


class HostPolicyTests(unittest.TestCase):
    def test_official_cdn_and_subdomains(self):
        for url, expected in (
            ("https://v26-web.douyinvod.com/media", "douyin"),
            ("https://p3-pc-sign.douyinpic.com/photo", "douyin"),
            ("https://upos-sz-mirrorcos.bilivideo.com/media", "bilibili"),
            ("http://i0.hdslb.com/cover", "bilibili"),
        ):
            with self.subTest(url=url):
                self.assertEqual(media_http.platform_of_url(url), expected)

    def test_keyword_port_scheme_and_credentials_bypasses(self):
        for url in (
            "https://douyin.attacker.invalid/video", "https://bilivideo.attacker.invalid/v",
            "https://bilibili.com.attacker.invalid/v", "https://evilbilibili.com/v",
            "http://localhost/douyin", "http://127.0.0.1/bilivideo",
            "https://www.bilibili.com:8443/x", "ftp://www.bilibili.com/x",
            "https://user:password@www.bilibili.com/x",
        ):
            with self.subTest(url=url):
                self.assertIsNone(media_http.platform_of_url(url))


class NetworkPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_short_links_reject_external_redirects(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(douyin.ParseError):
                await douyin.resolve_canonical_url("https://v.douyin.com/abc/", client=client)
            with self.assertRaises(bilibili.ParseError):
                await bilibili._resolve_short_with_client(client, "https://b23.tv/abc")
        self.assertEqual(len(calls), 2)

    async def test_platform_short_links_preserve_video_and_episode_targets(self):
        def handler(request):
            target = ("https://www.douyin.com/video/1234567890123456789"
                      if request.url.host == "v.douyin.com" else "https://www.bilibili.com/bangumi/play/ep123")
            return httpx.Response(302, headers={"location": target})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(await douyin.resolve_canonical_url("https://v.douyin.com/abc/", client),
                             "https://www.douyin.com/video/1234567890123456789")
            self.assertEqual(await bilibili._resolve_short_with_client(client, "https://b23.tv/abc"),
                             "https://www.bilibili.com/bangumi/play/ep123")
    async def test_mixed_public_private_dns_is_rejected(self):
        loop = asyncio.get_running_loop()
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
                   for address in ("1.1.1.1", "10.0.0.1")]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=records)):
            with self.assertRaises(media_http.UnsafeURL):
                await media_http.public_addresses("i0.hdslb.com", 443)

    async def test_local_reserved_and_mapped_dns_is_rejected(self):
        loop = asyncio.get_running_loop()
        for address in ("127.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "fc00::1",
                        "::ffff:127.0.0.1", "100.64.0.1"):
            with self.subTest(address=address):
                ip = ipaddress.ip_address(address)
                family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
                with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[
                    (family, socket.SOCK_STREAM, 6, "", (address, 443))
                ])):
                    with self.assertRaises(media_http.UnsafeURL):
                        await media_http.public_addresses("i0.hdslb.com", 443)

    async def test_socket_is_pinned_with_original_host_and_tls_name(self):
        captured = []
        async def send(request):
            captured.append(request)
            return httpx.Response(200, content=b"image")
        transport = media_http.PublicMediaTransport()
        # Intercept the actual transport entry point; DNS is called once before it.
        with patch("media_http.public_addresses", AsyncMock(return_value=["1.1.1.1"])) as dns, \
             patch.object(httpx.AsyncHTTPTransport, "handle_async_request", side_effect=send):
            await transport.handle_async_request(httpx.Request("GET", "https://i0.hdslb.com/a?sig=xyz"))
        try:
            self.assertEqual(captured[0].url.host, "1.1.1.1")
            self.assertEqual(captured[0].headers["host"], "i0.hdslb.com")
            self.assertEqual(captured[0].extensions["sni_hostname"], "i0.hdslb.com")
            self.assertEqual(captured[0].url.query, b"sig=xyz")
            dns.assert_awaited_once()
        finally:
            await transport.aclose()

    async def test_redirect_is_checked_before_second_request(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(media_http.UnsafeURL):
                await media_http.open_media(client, "https://i0.hdslb.com/a")
        self.assertEqual(len(calls), 1)

    async def test_relative_redirect_and_redirect_limit(self):
        def handler(request):
            if request.url.path == "/a":
                return httpx.Response(302, headers={"location": "/b"})
            return httpx.Response(200, content=b"ok")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async with media_http.media_stream(client, "https://i0.hdslb.com/a") as response:
                self.assertEqual(await response.aread(), b"ok")
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(302, headers={"location": "/loop"})
        )) as client:
            with self.assertRaises(media_http.UnsafeURL):
                await media_http.open_media(client, "https://i0.hdslb.com/a")

    async def test_size_limit_without_content_length(self):
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"abc"
                yield b"def"
        response = httpx.Response(200, stream=Body())
        with self.assertRaises(media_http.MediaTooLarge):
            async for _ in media_http.limited_chunks(response, 5):
                pass
        await response.aclose()


class CapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_jobs_fail_immediately_and_release_on_cancel(self):
        capacity = limits.Capacity(1)
        entered = asyncio.Event()
        async def worker():
            async with capacity.slot():
                entered.set()
                await asyncio.sleep(30)
        task = asyncio.create_task(worker())
        await entered.wait()
        with self.assertRaises(limits.CapacityExceeded):
            async with capacity.slot():
                pass
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(capacity.active, 0)

    async def test_subprocess_is_reaped_after_timeout_and_cancellation(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", "import time; time.sleep(30)",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                task = asyncio.create_task(limits.communicate_process(proc, 0.05 if not cancel else 30))
                if cancel:
                    await asyncio.sleep(0.05)
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
                    await task
                self.assertIsNotNone(proc.returncode)


class SessionTests(unittest.TestCase):
    def test_capacity_preserves_active_login_and_reclaims_expired(self):
        store = sessions.SessionStore(ttl=10, max_sessions=1)
        first = store.create()
        store.complete_qr(first, "secret", {"name": "A"})
        with self.assertRaises(sessions.SessionCapacityExceeded):
            store.create()
        self.assertEqual(store.get(first.sid).bili_cookie, "secret")
        first.last_seen -= 11
        second = store.create()
        self.assertNotEqual(first.sid, second.sid)
        self.assertEqual(store.count, 1)

    def test_logout_invalidates_pending_qr(self):
        store = sessions.SessionStore()
        item = store.create()
        store.begin_qr(item, "pending")
        store.logout_bili(item)
        self.assertFalse(store.qr_is_current(item, "pending"))


class PreparedTests(unittest.TestCase):
    def test_ownership_capacity_and_expiry_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            path = first / "video.mp4"
            path.write_bytes(b"video")
            store = PreparedFiles(maximum=1, ttl=10)
            token = store.add(path, "A", "video.mp4", "video/mp4")
            self.assertIsNone(store.claim(token, "B"))
            with self.assertRaises(PreparedCapacityExceeded):
                store.add(second / "other.mp4", "A", "other.mp4", "video/mp4")
            self.assertFalse(second.exists())
            self.assertIsNotNone(store.claim(token, "A"))
            store.items[token].expires_at = 0
            store.purge()
            self.assertTrue(path.exists())  # An active transfer must not be deleted.
            store.release(token)
            self.assertFalse(first.exists())


if __name__ == "__main__":
    unittest.main()
