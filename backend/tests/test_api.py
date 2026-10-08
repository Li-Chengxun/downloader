import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import main
import resource_limits as limits
import sessions
import slideshow
from prepared_files import PreparedFiles


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        yield self.content


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(main, "sessions", sessions.SessionStore(max_sessions=5)),
            patch.object(main, "prepared", PreparedFiles(maximum=2, ttl=60)),
            patch.object(limits, "jobs", limits.Capacity(1)),
            patch.object(limits, "downloads", limits.Capacity(2)),
        ]
        for item in self.patches:
            item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        main.prepared.close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def artifact(self, name="video.mp4"):
        directory = self.root / "output"
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_bytes(b"0123456789")
        return path

    def media_factory(self, handler):
        return lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    async def test_static_and_health_requests_do_not_create_sessions(self):
        for path in ("/", "/static/vendor/qrcode.js", "/missing", "/api/cover?url=https://evil.invalid/x"):
            response = await self.client.get(path)
            self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(main.sessions.count, 0)

    async def test_application_lifespan_starts_and_closes_clients(self):
        async with main.lifespan(main.app):
            self.assertIsNotNone(main.douyin._wtf_client)
            self.assertIsNotNone(main.bili._client)
            response = await self.client.get("/")
            self.assertEqual(response.status_code, 200)
        self.assertIsNone(main.douyin._wtf_client)
        self.assertIsNone(main.bili._client)

    async def test_job_timeout_releases_capacity(self):
        async def work(*args, **kwargs):
            await asyncio.sleep(30)
        with patch.object(limits, "JOB_TIMEOUT", 0.01), \
             patch.object(main.slideshow, "pack_zip", side_effect=work):
            response = await self.client.post("/api/images/zip", json={"images": [{"url": "https://p3.douyinpic.com/x"}]})
        self.assertEqual(response.status_code, 504)
        self.assertEqual(limits.jobs.active, 0)

    async def test_generated_file_size_rejection_cleans_output(self):
        path = self.artifact("images.zip")
        with patch.object(limits, "MAX_STREAM_BYTES", 5), \
             patch.object(main.slideshow, "pack_zip", AsyncMock(return_value=path)):
            response = await self.client.post("/api/images/zip?prepare=true", json={"images": [{"url": "https://p3.douyinpic.com/x"}]})
        self.assertEqual(response.status_code, 413)
        self.assertFalse(path.parent.exists())

    async def test_session_cookie_flags_and_capacity_response(self):
        response = await self.client.get("/api/bili/login")
        cookie = response.headers["set-cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=lax"):
            self.assertIn(flag, cookie)
        main.sessions = sessions.SessionStore(max_sessions=1)
        main.sessions.create()
        self.client.cookies.clear()
        response = await self.client.get("/api/bili/login")
        self.assertEqual(response.status_code, 503)
        self.assertIn("retry-after", response.headers)

    async def test_download_rejects_keyword_bypass(self):
        response = await self.client.get("/api/download", params={"url": "https://douyin.attacker.invalid/x"})
        self.assertEqual(response.status_code, 400)

    async def test_range_and_if_range_are_forwarded_with_partial_headers(self):
        def handler(request):
            self.assertEqual(request.headers["range"], "bytes=2-5")
            self.assertEqual(request.headers["if-range"], '"etag"')
            return httpx.Response(206, headers={"content-range": "bytes 2-5/10",
                                  "content-length": "4", "accept-ranges": "bytes", "etag": '"etag"'},
                                  stream=BytesStream(b"2345"))
        with patch.object(main.media_http, "media_client", self.media_factory(handler)):
            response = await self.client.get("/api/download", params={"url": "https://v.douyinvod.com/x"},
                                             headers={"Range": "bytes=2-5", "If-Range": '"etag"'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"2345")
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(response.headers["etag"], '"etag"')
        self.assertEqual(limits.downloads.active, 0)

    async def test_range_unsatisfiable_is_preserved(self):
        with patch.object(main.media_http, "media_client", self.media_factory(
            lambda r: httpx.Response(416, headers={"content-range": "bytes */10"}, stream=BytesStream(b""))
        )):
            response = await self.client.get("/api/download", params={"url": "https://v.douyinvod.com/x"},
                                             headers={"Range": "bytes=20-"})
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response.headers["content-range"], "bytes */10")

    async def test_oversize_download_is_rejected_before_streaming(self):
        with patch.object(main.media_http, "media_client", self.media_factory(
            lambda r: httpx.Response(200, headers={"content-length": str(limits.MAX_STREAM_BYTES + 1)},
                                     stream=BytesStream(b"x"))
        )):
            response = await self.client.get("/api/download", params={"url": "https://v.douyinvod.com/x"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(limits.downloads.active, 0)

    async def test_download_slot_released_after_upstream_error(self):
        with patch.object(main.media_http, "media_client", self.media_factory(
            lambda r: httpx.Response(403, stream=BytesStream(b"denied"))
        )):
            response = await self.client.get("/api/download", params={"url": "https://v.douyinvod.com/x"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(limits.downloads.active, 0)

    async def test_cover_html_is_not_served_from_same_origin(self):
        with patch.object(main.media_http, "media_client", self.media_factory(
            lambda r: httpx.Response(200, headers={"content-type": "text/html"}, stream=BytesStream(b"<script/>"))
        )):
            response = await self.client.get("/api/cover", params={"url": "https://i0.hdslb.com/x"})
        self.assertEqual(response.status_code, 502)

    async def test_pack_input_limits_are_validated(self):
        for payload in (
            {"images": []},
            {"images": [{"url": "https://p3.douyinpic.com/x"}] * (limits.MAX_IMAGES + 1)},
            {"images": [{"url": "https://p3.douyinpic.com/x"}], "per_image_sec": 100},
            {"images": [{"urls": ["x" * 8193]}]},
        ):
            with self.subTest(payload_size=len(str(payload))):
                response = await self.client.post("/api/images/zip", json=payload)
                self.assertEqual(response.status_code, 422)

    async def test_full_conversion_capacity_returns_429(self):
        async with limits.jobs.slot():
            response = await self.client.post("/api/images/zip", json={"images": [{"url": "https://p3.douyinpic.com/x"}]})
        self.assertEqual(response.status_code, 429)

    async def test_prepared_file_ownership_range_and_expiry(self):
        path = self.artifact("images.zip")
        with patch.object(main.slideshow, "pack_zip", AsyncMock(return_value=path)):
            response = await self.client.post("/api/images/zip?prepare=true", json={
                "images": [{"url": "https://p3.douyinpic.com/x"}], "filename": "images"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(limits.jobs.active, 0)
        download_url = response.json()["download_url"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://test") as other:
            self.assertEqual((await other.get(download_url)).status_code, 404)
        response = await self.client.get(download_url, headers={"Range": "bytes=2-5"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"2345")
        self.assertTrue(path.exists())
        response = await self.client.get(download_url)
        self.assertEqual(response.content, b"0123456789")
        main.prepared.items[download_url.rsplit("/", 1)[1]].expires_at = 0
        response = await self.client.get(download_url)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(path.parent.exists())

    async def test_legacy_binary_endpoint_still_cleans_and_releases(self):
        path = self.artifact("images.zip")
        with patch.object(main.slideshow, "pack_zip", AsyncMock(return_value=path)):
            response = await self.client.post("/api/images/zip", json={"images": [{"url": "https://p3.douyinpic.com/x"}]})
        self.assertEqual(response.content, b"0123456789")
        self.assertEqual(limits.jobs.active, 0)
        self.assertFalse(path.parent.exists())

    async def test_qr_result_cannot_restore_login_after_logout(self):
        sess = main.sessions.create()
        main.sessions.begin_qr(sess, "old")
        self.client.cookies.set(sessions.SESSION_COOKIE, sess.sid)
        async def poll(key):
            main.sessions.logout_bili(sess)
            return {"status": "success", "cookie": "secret", "user": {"is_login": True}}
        with patch.object(main.bili, "qr_poll", side_effect=poll):
            response = await self.client.get("/api/bili/login/qr/poll?key=old")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(sess.logged_in)

    async def test_parse_forwards_browser_login_cookie_and_ffmpeg_capability(self):
        sess = main.sessions.create()
        main.sessions.complete_qr(sess, "SESSDATA=synthetic-account", {"is_login": True, "vip": False})
        self.client.cookies.set(sessions.SESSION_COOKIE, sess.sid)
        with patch.object(main.bili, "parse", AsyncMock(return_value={"ok": True, "platform": "bilibili"})) as parse, \
             patch.object(main.ffmpeg_tool, "available", return_value=True):
            response = await self.client.get("/api/parse", params={"url": "BV1GJ411x7h7"})
        parse.assert_awaited_once_with("BV1GJ411x7h7", page=None, ep=None, cookie=sess.bili_cookie)
        self.assertTrue(response.json()["server_ffmpeg"])

    async def test_cancelled_slideshow_removes_temporary_directory(self):
        directory = self.root / "cancelled"
        directory.mkdir()
        started = asyncio.Event()
        async def handler(request):
            started.set()
            await asyncio.sleep(30)
        with patch.object(slideshow, "ffmpeg_path", return_value="dummy"), \
             patch.object(slideshow.tempfile, "mkdtemp", return_value=str(directory)), \
             patch.object(slideshow.media_http, "media_client", self.media_factory(handler)):
            task = asyncio.create_task(slideshow.build_slideshow([{"url": "https://p3.douyinpic.com/x"}]))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
