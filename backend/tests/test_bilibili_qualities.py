"""Regression coverage for ordinary-account DASH quality and WinGet detection."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bilibili
import ffmpeg_tool


class FFmpegDetectionTests(unittest.TestCase):
    def test_bad_path_shim_falls_back_to_installed_binary_and_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            shim, real = Path(directory) / "shim.exe", Path(directory) / "ffmpeg.exe"
            shim.touch()
            real.touch()
            with patch.object(ffmpeg_tool, "FFMPEG_BIN", ""), \
                 patch.object(ffmpeg_tool, "_ffmpeg_cache", None), \
                 patch.object(ffmpeg_tool.shutil, "which", return_value=str(shim)), \
                 patch.object(ffmpeg_tool, "_winget_ffmpeg_paths", return_value=[str(real)]), \
                 patch.object(ffmpeg_tool, "_ffmpeg_runs", side_effect=[False, True]) as probe:
                self.assertEqual(ffmpeg_tool.ffmpeg_path(), str(real))
                self.assertTrue(ffmpeg_tool.available())
                self.assertEqual(probe.call_count, 2)

    def test_explicit_valid_binary_keeps_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "ffmpeg.exe"
            real.touch()
            with patch.object(ffmpeg_tool, "FFMPEG_BIN", str(real)), \
                 patch.object(ffmpeg_tool, "_ffmpeg_cache", None), \
                 patch.object(ffmpeg_tool.shutil, "which", return_value=None), \
                 patch.object(ffmpeg_tool, "_winget_ffmpeg_paths") as fallback, \
                 patch.object(ffmpeg_tool, "_ffmpeg_runs", return_value=True):
                self.assertEqual(ffmpeg_tool.ffmpeg_path(), str(real))
                fallback.assert_not_called()

    def test_home_installation_is_found_with_redirected_localappdata(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            real = home / "AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_test/ffmpeg-version/bin/ffmpeg.exe"
            real.parent.mkdir(parents=True)
            real.touch()
            with patch.object(ffmpeg_tool.sys, "platform", "win32"), \
                 patch.object(ffmpeg_tool.Path, "home", return_value=home), \
                 patch.dict(ffmpeg_tool.os.environ, {"LOCALAPPDATA": str(home / "redirected")}):
                self.assertEqual(ffmpeg_tool._winget_ffmpeg_paths(), [str(real)])


class BilibiliQualityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.account_cookie = "SESSDATA=synthetic-account"
        self.patches = [
            patch.object(bilibili, "_ensure_client", return_value=object()),
            patch.object(bilibili, "_api_get", side_effect=self.api_response),
            patch.object(bilibili, "dash_supported", return_value=True),
            patch.object(bilibili, "_fingerprint_report", AsyncMock(return_value={})),
            patch.object(bilibili, "fetch_nav", AsyncMock(return_value={"is_login": True, "vip": False})),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])

    async def api_response(self, api, params, cookie="", **kwargs):
        self.assertEqual(cookie, self.account_cookie)
        if api == bilibili._VIEW_API:
            return {"code": 0, "data": {"bvid": "BV1GJ411x7h7", "title": "test", "pages": [{"cid": 1, "duration": 60}]}}
        self.assertEqual(api, bilibili._PLAYURL_API)
        if params["fnval"] == 1:
            return {"code": 0, "data": {"quality": 64, "accept_quality": [112, 80, 64],
                    "durl": [{"url": "https://upos.bilivideo.com/mp4", "size": 100, "length": 60000}]}}
        return {"code": 0, "data": {"accept_quality": [112, 80, 64], "dash": {
            "video": [{"id": 80, "codecid": 7, "width": 1920, "height": 1080, "bandwidth": 1000000,
                       "baseUrl": "https://upos.bilivideo.com/video"}],
            "audio": [{"id": 30280, "bandwidth": 128000, "baseUrl": "https://upos.bilivideo.com/audio"}],
        }}}

    async def test_ordinary_account_gets_real_1080_dash_when_mp4_is_720(self):
        result = await bilibili.parse_share_url("BV1GJ411x7h7", cookie=self.account_cookie)
        self.assertTrue(result["logged_in"])
        self.assertEqual([q["qn"] for q in result["qualities"]], [80, 64])
        self.assertTrue(result["qualities"][0]["best"])
        self.assertTrue(result["qualities"][0]["needs_merge"])

    async def test_ordinary_account_diagnostic_reports_returned_dash_not_vip_ceiling(self):
        result = await bilibili.debug_qualities("BV1GJ411x7h7", cookie=self.account_cookie)
        self.assertFalse(result["nav"]["vip"])
        self.assertIn("1080P", result["verdict"])
        self.assertNotIn("1080P+", result["verdict"])
        self.assertNotIn("封顶", result["verdict"])
        self.assertTrue(result["server_ffmpeg"])

    async def test_missing_ffmpeg_diagnostic_explains_hidden_dash(self):
        with patch.object(bilibili, "dash_supported", return_value=False):
            result = await bilibili.debug_qualities("BV1GJ411x7h7", cookie=self.account_cookie)
        self.assertIn("1080P", result["verdict"])
        self.assertIn("ffmpeg", result["verdict"])
        self.assertFalse(result["server_ffmpeg"])

    async def test_declared_quality_is_not_treated_as_a_downloadable_stream(self):
        verdict = bilibili._quality_verdict(64, {"accept_quality": [112, 80], "video_qualities": [32]}, True)
        self.assertIn("最高档位为 720P", verdict)
        self.assertNotIn("最高可下载 1080P", verdict)
