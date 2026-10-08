"""Short-lived, browser-owned generated files for native downloads."""

import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from ffmpeg_tool import cleanup_dir
from resource_limits import positive_int


class PreparedCapacityExceeded(Exception):
    pass


@dataclass
class PreparedFile:
    path: Path
    owner: str
    filename: str
    media_type: str
    expires_at: float
    active: bool = False


class PreparedFiles:
    def __init__(self, maximum=None, ttl=None):
        self.maximum = maximum if maximum is not None else positive_int("MAX_PREPARED_FILES", 8)
        self.ttl = ttl if ttl is not None else positive_int("PREPARED_FILE_TTL", 600)
        self.items: dict[str, PreparedFile] = {}

    def add(self, path, owner, filename, media_type):
        self.purge()
        if len(self.items) >= self.maximum:
            cleanup_dir(path.parent)
            raise PreparedCapacityExceeded("待下载文件已满，请稍后重试")
        token = secrets.token_urlsafe(32)
        self.items[token] = PreparedFile(path, owner, filename, media_type, time.monotonic() + self.ttl)
        return token

    def claim(self, token, owner):
        self.purge()
        item = self.items.get(token)
        if item is None or item.owner != owner or item.active:
            return None
        item.active = True
        return item

    def finish(self, token):
        item = self.items.pop(token, None)
        if item:
            cleanup_dir(item.path.parent)

    def release(self, token):
        item = self.items.get(token)
        if item:
            item.active = False
        self.purge()

    def purge(self):
        now = time.monotonic()
        for token, item in list(self.items.items()):
            if not item.active and now >= item.expires_at:
                self.finish(token)

    def close(self):
        for token in list(self.items):
            self.finish(token)
