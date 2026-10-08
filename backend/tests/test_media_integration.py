"""Real ffmpeg and ZIP smoke tests using synthetic images, never platform traffic."""

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import slideshow
from ffmpeg_tool import ffmpeg_path


class MediaIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_slideshow_and_zip(self):
        ff = ffmpeg_path()
        if not ff:
            self.skipTest("ffmpeg executable is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = []
            for index, color in enumerate(("red", "blue")):
                image = root / f"{index}.png"
                import asyncio
                proc = await asyncio.create_subprocess_exec(
                    ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c={color}:s=64x64", "-frames:v", "1", str(image),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, error = await slideshow.limits.communicate_process(proc)
                self.assertEqual(proc.returncode, 0, error.decode("utf-8", "replace"))
                images.append(image.read_bytes())
            def factory(**kwargs):
                def handler(request):
                    return httpx.Response(200, headers={"content-type": "image/png"},
                                          content=images[int(request.url.path[1:])])
                return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
            payload = [{"url": f"https://p3.douyinpic.com/{i}", "ext": "png", "width": 64, "height": 64}
                       for i in range(2)]
            video = archive = None
            try:
                with patch.object(slideshow.media_http, "media_client", factory):
                    video = await slideshow.build_slideshow(payload, 0.5)
                    archive = await slideshow.pack_zip(payload, meta={"title": "测试", "desc": "正文"})
                self.assertGreater(video.stat().st_size, 0)
                self.assertIn(b"ftyp", video.read_bytes()[:32])
                with zipfile.ZipFile(archive) as bundle:
                    self.assertEqual(len(bundle.namelist()), 3)
                    self.assertEqual(bundle.read("01.png"), images[0])
                    with zipfile.ZipFile(io.BytesIO(bundle.read(slideshow.DOCX_NAME))) as document:
                        self.assertIn("正文", document.read("word/document.xml").decode("utf-8"))
            finally:
                for path in (video, archive):
                    if path:
                        slideshow.cleanup_dir(path.parent)


if __name__ == "__main__":
    unittest.main()
