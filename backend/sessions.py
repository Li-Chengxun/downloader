"""浏览器会话存储（纯内存）。

为什么需要它：扫码登录拿到的是**账号凭据**。如果把它存成全局变量，那么
A 扫码之后，B、C 所有访客都会用 A 的账号去下载——既侵犯隐私，也可能让
账号被平台判定为异常共享而封禁。

所以这里给每个浏览器分配一个随机 session id（HttpOnly Cookie 承载），
把凭据绑在**各自的会话**上，互相看不见。

生命周期：**只存在内存里**。进程重启（含容器重建 / 重新部署）后全部失效，
需要重新扫码。这是刻意的取舍——凭据不落盘，就没有服务器被入侵后账号
外泄的风险。如果你的场景希望长期免扫码，请在 ``.env`` 里配置
``BILI_COOKIE``：那是服务端级的全局兜底，属于「这台服务器统一用这个账号」，
语义与会话登录不同，代码里会被区分对待（见 ``bilibili.resolve_cookie``）。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: 承载 session id 的 Cookie 名
SESSION_COOKIE = "vd_sid"

#: 会话有效期（秒）。默认 30 天。
DEFAULT_TTL = 30 * 24 * 3600

#: 二维码扫码会话的有效期（秒），B 站二维码本身约 3 分钟失效
QR_TTL = 180


@dataclass
class Session:
    """一个访客的会话。"""

    sid: str
    created_at: float
    last_seen: float
    #: 该访客扫码登录后拿到的 B 站 Cookie（仅存内存，不下发给前端）
    bili_cookie: str = ""
    #: 该访客的 B 站账号信息（昵称 / UID / 头像 / 会员状态）
    bili_user: Dict[str, str] = field(default_factory=dict)
    #: 登录时间戳（0 表示未登录）
    login_at: float = 0.0
    #: 进行中的扫码会话：qrcode_key 与其创建时间
    qr_key: str = ""
    qr_at: float = 0.0

    @property
    def logged_in(self) -> bool:
        return bool(self.bili_cookie)

    def public_login(self) -> Dict[str, str]:
        """可安全下发给前端的登录信息（不含凭据）。"""
        return dict(self.bili_user) if self.logged_in else {}


class SessionStore:
    """会话表。单进程内存字典——本项目是单容器单进程部署，够用。"""

    def __init__(self, ttl: int = DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._data: Dict[str, Session] = {}

    # ---------------------------------------------------------- 基础操作

    def create(self) -> Session:
        now = time.time()
        sid = secrets.token_urlsafe(24)
        sess = Session(sid=sid, created_at=now, last_seen=now)
        self._data[sid] = sess
        # 顺手清理，避免长期运行后内存里堆积过期会话
        if len(self._data) > 64:
            self.purge()
        return sess

    def get(self, sid: Optional[str]) -> Optional[Session]:
        """按 id 取会话；不存在或已过期返回 None，并刷新活跃时间。"""
        if not sid:
            return None
        sess = self._data.get(sid)
        if sess is None:
            return None
        if time.time() - sess.last_seen > self._ttl:
            self._data.pop(sid, None)
            return None
        sess.last_seen = time.time()
        return sess

    def drop(self, sid: Optional[str]) -> bool:
        return bool(sid and self._data.pop(sid, None) is not None)

    # ---------------------------------------------------------- 扫码登录

    def begin_qr(self, sess: Session, qrcode_key: str) -> None:
        """登记一个进行中的扫码会话（同一时间只保留最新的一个）。"""
        sess.qr_key = qrcode_key
        sess.qr_at = time.time()

    def qr_is_current(self, sess: Session, qrcode_key: str) -> bool:
        """校验这次轮询确实属于本会话当前那个二维码。

        防止有人拿别人的 qrcode_key 来轮询，把登录结果写进自己的会话。
        """
        if not sess.qr_key or sess.qr_key != qrcode_key:
            return False
        return time.time() - sess.qr_at <= QR_TTL

    def complete_qr(self, sess: Session, cookie: str, user: Dict[str, str]) -> None:
        sess.bili_cookie = cookie
        sess.bili_user = user
        sess.login_at = time.time()
        sess.qr_key = ""
        sess.qr_at = 0.0

    def logout_bili(self, sess: Session) -> None:
        sess.bili_cookie = ""
        sess.bili_user = {}
        sess.login_at = 0.0

    # ---------------------------------------------------------- 维护

    def purge(self) -> int:
        """清理过期会话，返回清理条数。"""
        now = time.time()
        dead: List[str] = [sid for sid, s in self._data.items() if now - s.last_seen > self._ttl]
        for sid in dead:
            self._data.pop(sid, None)
        return len(dead)

    @property
    def count(self) -> int:
        return len(self._data)

    @property
    def logged_in_count(self) -> int:
        return sum(1 for s in self._data.values() if s.logged_in)
