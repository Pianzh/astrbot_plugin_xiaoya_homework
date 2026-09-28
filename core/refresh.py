"""token 自动续期。

小雅登录后服务器下发三个 cookie：

===========================  ==========================================
``WT-prd-access-token``      调业务接口用的短期凭证
``WT-prd-refresh-token``     用来换新 access token
``WT-prd-refresh-token-state-v2``  官方前端靠它判断要不要续期
===========================  ==========================================

access token 只活十来小时，refresh token 决定你多久要重扫一次。
``POST /api/jx-auth/oauth2/token`` 的返回值里直接带两个过期时间戳，
所以剩下的时间不用猜。

请求要带 ``Authorization: Basic <orgToken>``，orgToken 从
``GET /api/jw-starcmooc/base/school/gainReactApp`` 取（该接口不鉴权）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .client import DEFAULT_SCHOOL, SCHOOLS, XiaoyaError

# 官方前端在「记住我」打开时发的就是 7 天，这是平台自己定义的续期时长。
# 服务端并不校验上限（传 30 天也给），但官方从来没这么请求过，
# 超出这个数就是在利用校验缺失，不做。
REMEMBER_ME_DAYS = 7
REFRESH_TOKEN_TIME_MS = REMEMBER_ME_DAYS * 24 * 60 * 60 * 1000

GAIN_APP_PATH = "/api/jw-starcmooc/base/school/gainReactApp"
TOKEN_PATH = "/api/jx-auth/oauth2/token"

CST = timezone(timedelta(hours=8))


class RefreshFailed(XiaoyaError):
    """续期失败，只能重新扫码。"""


@dataclass
class TokenInfo:
    """一次续期拿回来的东西。"""

    access_token: str
    refresh_token: str
    access_expires_at: datetime | None
    refresh_expires_at: datetime | None

    def hours_left(self, which: str = "access") -> float:
        at = self.access_expires_at if which == "access" else self.refresh_expires_at
        if at is None:
            return 0.0
        return (at - datetime.now(CST)).total_seconds() / 3600.0


def _parse_ts(value: object) -> datetime | None:
    """解析接口返回的 ISO 时间串。

    字段名叫 ``*_expires_in`` 但给的是绝对时刻，不是时长，别被误导。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class TokenRefresher:
    """按 refresh token 换新的 access token。"""

    def __init__(
        self,
        school: str = DEFAULT_SCHOOL,
        timeout: float = 20.0,
        proxy: str | None = None,
    ) -> None:
        if school not in SCHOOLS:
            raise XiaoyaError(f"未知学校标识：{school}")
        self.school = SCHOOLS[school]
        self.base = f"https://{self.school['host']}"
        self._org_token = ""
        self._client = httpx.AsyncClient(
            timeout=timeout,
            proxy=proxy or None,
            follow_redirects=False,
            headers={
                "accept": "application/json, text/plain, */*",
                "content-type": "application/x-www-form-urlencoded",
                "user-agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
                "referer": f"{self.base}/",
                "origin": self.base,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> TokenRefresher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def org_token(self) -> str:
        """取 orgToken（``client:secret`` 的 base64），缓存住不重复请求。"""
        if self._org_token:
            return self._org_token
        try:
            resp = await self._client.get(self.base + GAIN_APP_PATH)
        except httpx.HTTPError as exc:
            raise RefreshFailed(f"取鉴权凭据失败：{exc}") from exc
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RefreshFailed("鉴权凭据接口返回的不是 JSON") from exc
        result = payload.get("result") if isinstance(payload, dict) else None
        token = str((result or {}).get("reactAppOrgToken") or "").strip()
        if not token:
            raise RefreshFailed("鉴权凭据接口没返回 reactAppOrgToken")
        self._org_token = token
        return token

    async def refresh(self, refresh_token: str) -> TokenInfo:
        """用 refresh token 换一对新 token。"""
        refresh_token = (refresh_token or "").strip()
        if not refresh_token:
            raise RefreshFailed("没有 refresh token，得重新扫码")

        auth = await self.org_token()
        body = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "token_time": str(REFRESH_TOKEN_TIME_MS),
        }
        try:
            # 注意只能给 data=。同时给 content= 会把 body 覆盖成空，
            # 续期接口就会收到一个没有 refresh_token 的空请求。
            resp = await self._client.post(
                self.base + TOKEN_PATH,
                headers={"Authorization": f"Basic {auth}"},
                data=body,
            )
        except httpx.HTTPError as exc:
            raise RefreshFailed(f"续期请求失败：{exc}") from exc

        if resp.status_code in (401, 403):
            raise RefreshFailed(
                "refresh token 已失效，需要重新扫码（可能是换设备或被挤下线）"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RefreshFailed("续期接口返回的不是 JSON") from exc
        if not isinstance(payload, dict):
            raise RefreshFailed("续期接口返回了意料之外的结构")

        access = str(payload.get("access_token") or "").strip()
        if not access:
            message = payload.get("message") or payload.get("msg") or resp.status_code
            raise RefreshFailed(f"续期被拒：{message}")

        new_refresh = str(payload.get("refresh_token") or "").strip() or refresh_token
        return TokenInfo(
            access_token=access,
            refresh_token=new_refresh,
            access_expires_at=_parse_ts(payload.get("access_token_expires_in")),
            refresh_expires_at=_parse_ts(payload.get("refresh_token_expires_in")),
        )
