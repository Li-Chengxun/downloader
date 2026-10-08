"""Shared limits for single-process downloads and conversion jobs."""

import asyncio
import os
from contextlib import asynccontextmanager


def positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


MAX_IMAGES = positive_int("MAX_IMAGES", 100)
MAX_IMAGE_BYTES = positive_int("MAX_IMAGE_BYTES", 20 * 1024 * 1024)
MAX_STREAM_BYTES = positive_int("MAX_STREAM_BYTES", 4 * 1024 * 1024 * 1024)
JOB_TIMEOUT = positive_int("JOB_TIMEOUT", 900)
FFMPEG_TIMEOUT = positive_int("FFMPEG_TIMEOUT", 300)


class CapacityExceeded(Exception):
    pass


class Capacity:
    """Fail immediately when full; do not create an unbounded wait queue."""
    def __init__(self, maximum: int):
        self.maximum = maximum
        self.active = 0

    @asynccontextmanager
    async def slot(self):
        if self.active >= self.maximum:
            raise CapacityExceeded("服务器任务已满，请稍后重试")
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1


jobs = Capacity(positive_int("MAX_MEDIA_JOBS", 2))
downloads = Capacity(positive_int("MAX_DOWNLOADS", 16))


async def communicate_process(proc, timeout=FFMPEG_TIMEOUT):
    """Timeout or cancellation must reap ffmpeg before its temp files are removed."""
    task = asyncio.create_task(proc.communicate())
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except BaseException:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await asyncio.shield(task)
        raise
