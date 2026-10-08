"""Local visual fixtures: run from backend with ../.venv/Scripts/python ../_design/preview.py.

This preview serves the real frontend with synthetic content. It never contacts
video platforms and is separate from the production FastAPI application.
Enter video, images, text or bangumi in the input to inspect those layouts.
"""
import asyncio
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1] / "backend" / "static"
app = FastAPI()
app.mount("/static", StaticFiles(directory=ROOT), name="static")


@app.get("/")
async def index():
    return FileResponse(ROOT / "index.html")


@app.get("/api/bili/login")
async def login():
    return {"ok": True, "logged_in": False, "source": "none", "can_logout": False}


@app.get("/api/parse")
async def parse(url: str, ep: int = 1):
    await asyncio.sleep(0.5)
    content = {
        "ok": True, "platform": "bilibili", "kind": "video", "server_ffmpeg": True,
        "title": "把日常拍成电影：在山野之间，收藏一个安静的傍晚",
        "author": "取回 · 页面演示", "video_id": "BV1DEMO12345", "cid": 123,
        "cover": "https://i0.hdslb.com/preview", "summary": "远处的山、柔和的光与缓缓经过的云。记录旅途中的片刻，也记录生活里值得重温的小事。",
        "desc": "这是一份用于页面走查的演示内容。", "duration_text": "03:24",
        "total_pages": 1, "logged_in": False,
        "qualities": [
            {"key": "1080", "label": "1080P 高清", "codec": "H.264", "best": True,
             "format": "mp4", "width": 1920, "height": 1080, "size_bytes": 84200000,
             "size_text": "80.3 MB", "urls": ["https://upos.bilivideo.com/preview"]},
            {"key": "720", "label": "720P 清晰", "codec": "H.264", "format": "mp4",
             "width": 1280, "height": 720, "size_bytes": 41000000, "size_text": "39.1 MB",
             "urls": ["https://upos.bilivideo.com/preview"]},
            {"key": "480", "label": "480P 流畅", "codec": "H.264", "format": "mp4",
             "width": 854, "height": 480, "size_bytes": 22000000, "size_text": "21.0 MB",
             "urls": ["https://upos.bilivideo.com/preview"]},
        ],
    }
    if url == "images":
        content.update(platform="douyin", kind="images", title="山野日记｜把温柔的颜色收进相册", cover="",
                       qualities=[], images=[{"index": i, "url": f"https://p3.douyinpic.com/{i}",
                       "urls": [], "width": 960, "height": 1280, "ext": "jpg"} for i in range(1, 7)])
    elif url == "text":
        content.update(platform="douyin", kind="text", title="一些关于生活与收藏的文字", cover="", qualities=[],
                       desc="喜欢的东西，不必急着向别人解释。\n\n留一张照片，存一段声音，写下某个平凡的下午。\n\n多年以后再回来看，那些看似微小的瞬间，也许就是生活最动人的部分。")
    elif url == "bangumi":
        content.update(kind="bangumi", title="山野之间 · 第一季", total_pages=12, ep_id=ep,
                       episode_title=f"第 {ep} 集 · 远山来信", episodes=[{"ep_id": i, "short": str(i),
                       "part": f"第 {i} 集", "duration_text": "24:00", "locked": i > 5} for i in range(1, 13)])
    return content


@app.get("/api/cover")
async def cover(url: str):
    tint = (sum(map(ord, url)) % 4)
    colors = ["#e4eafa", "#f2e4dc", "#e2ece9", "#eee3ee"]
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 300">
    <rect width="480" height="300" fill="{colors[tint]}"/>
    <circle cx="370" cy="70" r="34" fill="#efd0a1"/>
    <path d="M0 240 120 110 220 225 310 140 480 280V300H0Z" fill="#a2b0be"/>
    <path d="M0 300 170 185 300 275 420 215 480 240V300Z" fill="#6a8193"/>
    <path d="M0 300 230 265 350 289 480 260V300Z" fill="#4d6679"/></svg>'''
    return Response(svg, media_type="image/svg+xml")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8766)
