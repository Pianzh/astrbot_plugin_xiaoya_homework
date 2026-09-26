"""小雅扫码登录。

流程（全部来自小雅官方登录页 ``infra.ai-augmented.com/app/auth`` 的前端实现）：

1. ``GET /api/auth/qrLogin/getCode``      拿二维码地址，二维码 key 藏在 query 里
2. ``GET /api/auth/qrLogin/getCodeStatus`` 每秒轮询，status 2 表示已确认
3. 换取 authorization code，再回调学校站点拿 ``prd-access-token`` cookie

第 3 步官方前端走的是浏览器跳转，服务端没有稳定的公开接口，
所以这里准备了 A/B 两条路径依次尝试。
"""

from __future__ import annotations

import asyncio
import secrets
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

import httpx

from .client import SCHOOLS, XiaoyaError

AUTH_BASE = "https://infra.ai-augmented.com"

# getCodeStatus 的 status 枚举
ST_ACTIVE = 0  # 等待扫码
ST_SCANNED = 1  # 已扫描，等待确认
ST_CONFIRMED = 2  # 已确认
ST_DENIED = 3  # 用户拒绝
ST_EXPIRED = 4  # 二维码过期

STATUS_TEXT = {
    ST_ACTIVE: "等待扫码",
    ST_SCANNED: "已扫描，请在手机上确认",
    ST_CONFIRMED: "确认成功",
    ST_DENIED: "你在手机上拒绝了本次登录",
    ST_EXPIRED: "二维码已过期",
}

# getCode 与状态码是平行的两套 API
QCODE_PATH = "/api/auth/qrLogin/getCode"
QSTATUS_PATH = "/api/auth/qrLogin/getCodeStatus"
ON_REDIRECT_PATH = "/api/auth/oauth/onAccountAuthRedirect"
AUTHORIZE_PATH = "/api/auth/oauth/authorize"

ALPHABET = string.ascii_lowercase + string.digits


class QrLoginError(XiaoyaError):
    """扫码登录失败。"""


class QrLoginDenied(QrLoginError):
    """用户拒绝或二维码过期。"""


@dataclass(slots=True)
class QrSession:
    qr_url: str
    key: str
    state: str


@dataclass(slots=True)
class QrLoginResult:
    token: str
    method: Literal["A", "B"]


def new_state() -> str:
    """官方前端用 Math.random().toString(36).substring(2,8)，等长即可。"""
    return "".join(secrets.choice(ALPHABET) for _ in range(6))


def extract_key(qr_url: str) -> str:
    """二维码 key 藏在 qrCodeUrl 的 query 参数里。"""
    query = parse_qs(urlparse(qr_url).query)
    key = query.get("key", [""])[0]
    if not key:
        raise QrLoginError("二维码地址里没有 key，接口可能改版了")
    return key


def render_qr_png(qr_url: str, dest: Path) -> Path | None:
    """把二维码渲染成 PNG。``qrcode`` 不可用时返回 None，由调用方降级成纯文本。"""
    try:
        import qrcode
    except ImportError:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(dest)
    return dest


class QrLoginClient:
    """扫码登录会话。"""

    def __init__(
        self,
        school: str = "whut",
        timeout: float = 20.0,
        proxy: str | None = None,
    ) -> None:
        if school not in SCHOOLS:
            raise QrLoginError(f"未知学校标识：{school}")
        self.school_key = school
        self.school = SCHOOLS[school]
        self.host = self.school["host"]
        self.redirect_uri = self.school["redirect_uri"]
        self._client = httpx.AsyncClient(
            timeout=timeout,
            proxy=proxy or None,
            follow_redirects=False,
            headers={
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json; charset=utf-8",
                "referer": f"{AUTH_BASE}/app/auth/oauth2/qrcodeLogin",
                "origin": AUTH_BASE,
                "user-agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> QrLoginClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    # ---- 步骤 1：取码 ----

    async def create_session(self) -> QrSession:
        state = new_state()
        params = {
            "clientId": self.school["client_id"],
            "state": state,
            "redirectUri": self.redirect_uri,
            "weekNoLoginStatus": 0,
        }
        try:
            resp = await self._client.get(AUTH_BASE + QCODE_PATH, params=params)
        except httpx.HTTPError as exc:
            raise QrLoginError(f"请求二维码失败：{exc}") from exc

        payload = _json(resp)
        if payload.get("code") != 200:
            raise QrLoginError(
                f"取二维码被拒：{payload.get('message') or payload.get('code')}"
            )
        data = payload.get("data") or {}
        qr_url = data.get("qrCodeUrl")
        if not qr_url:
            raise QrLoginError("接口没返回二维码地址")
        return QrSession(qr_url=qr_url, key=extract_key(qr_url), state=state)

    # ---- 步骤 2：轮询 ----

    async def poll_status(self, key: str) -> int:
        try:
            resp = await self._client.get(AUTH_BASE + QSTATUS_PATH, params={"code": key})
        except httpx.HTTPError as exc:
            raise QrLoginError(f"轮询失败：{exc}") from exc
        payload = _json(resp)
        if payload.get("code") != 200:
            raise QrLoginError(
                f"轮询被拒：{payload.get('message') or payload.get('code')}"
            )
        data = payload.get("data") or {}
        try:
            return int(data.get("status"))
        except (TypeError, ValueError) as exc:
            raise QrLoginError("轮询返回了无法识别的状态") from exc

    async def wait_for_confirm(
        self,
        session: QrSession,
        timeout: float = 60.0,
        interval: float = 1.5,
        on_status: object = None,
    ) -> None:
        """轮询到用户确认为止。``on_status`` 是 ``async (int) -> None`` 回调。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last = -1
        while loop.time() < deadline:
            status = await self.poll_status(session.key)
            if status != last:
                last = status
                if on_status is not None:
                    await on_status(status)  # type: ignore[misc]
            if status == ST_CONFIRMED:
                return
            if status in (ST_DENIED, ST_EXPIRED):
                raise QrLoginDenied(STATUS_TEXT.get(status, "登录未完成"))
            await asyncio.sleep(interval)
        raise QrLoginDenied("等待超时，二维码已失效")

    # ---- 步骤 3：换 token ----

    def _oauth_params(self, state: str) -> dict[str, str]:
        return {
            "response_type": "code",
            "state": state,
            "client_id": self.school["client_id"],
            "redirect_uri": self.redirect_uri,
            "school": self.school["school_code"],
            "lang": "zh_CN",
        }

    async def _exchange_a(self, state: str) -> str:
        """A 路：走 onAccountAuthRedirect，跟 302 拿 code。"""
        resp = await self._client.get(
            AUTH_BASE + ON_REDIRECT_PATH,
            params=self._oauth_params(state),
            headers={"referer": f"{AUTH_BASE}/app/auth/oauth2/securityNotice"},
        )
        return _code_from_response(resp)

    async def _exchange_b(self, state: str) -> str:
        """B 路：重放 authorize，同一个 state 下已登录会直接跳回 redirect_uri。"""
        resp = await self._client.get(
            AUTH_BASE + AUTHORIZE_PATH, params=self._oauth_params(state)
        )
        return _code_from_response(resp)

    async def fetch_token(self, state: str) -> QrLoginResult:
        """依次尝试 A、B 两条路径换取 authorization code。"""
        errors: list[str] = []
        for method, fn in (("A", self._exchange_a), ("B", self._exchange_b)):
            try:
                code = await fn(state)
            except QrLoginError as exc:
                errors.append(f"{method} 路：{exc}")
                continue
            except httpx.HTTPError as exc:
                errors.append(f"{method} 路：{exc}")
                continue
            if not code:
                errors.append(f"{method} 路：跳转里没有 code 参数")
                continue
            token = await self._redeem(code, state)
            if token:
                return QrLoginResult(token=token, method=method)  # type: ignore[arg-type]
            errors.append(f"{method} 路：回调没有返回 token")
        raise QrLoginError("换 token 失败。" + "；".join(errors))

    async def _redeem(self, code: str, state: str) -> str:
        """访问学校侧回调，从 Set-Cookie 里抓 prd-access-token。"""
        joiner = "&" if "?" in self.redirect_uri else "?"
        url = f"{self.redirect_uri}{joiner}code={code}&state={state}"
        try:
            resp = await self._client.get(
                url, headers={"referer": f"{AUTH_BASE}/app/auth/"}
            )
        except httpx.HTTPError as exc:
            raise QrLoginError(f"回调失败：{exc}") from exc

        token = _token_from_cookies(resp.headers.get_list("set-cookie"))
        if token:
            return token
        # 有些部署会把 token 直接放在重定向 body 里
        if resp.headers.get("location"):
            token = _token_from_cookies([resp.headers["location"]])
            if token:
                return token
        return ""

    # ---- 一站式 ----

    async def login(
        self,
        timeout: float = 60.0,
        on_status: object = None,
    ) -> QrLoginResult:
        session = await self.create_session()
        await self.wait_for_confirm(session, timeout=timeout, on_status=on_status)
        return await self.fetch_token(session.state)

    @property
    def qr_page_hint(self) -> str:
        return "打开「小雅」App，首页右上角扫一扫，扫下面这个码"


# ---- 小工具 ----


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError as exc:
        raise QrLoginError("接口返回的不是 JSON，可能被网关拦截了") from exc
    if not isinstance(data, dict):
        raise QrLoginError("接口返回了意料之外的结构")
    return data


def _code_from_response(resp: httpx.Response) -> str:
    """从 302 的 Location 里抠出 code。"""
    location = resp.headers.get("location", "")
    if not location:
        return ""
    query = parse_qs(urlparse(location).query)
    return query.get("code", [""])[0]


def _token_from_cookies(set_cookie_values: list[str]) -> str:
    for raw in set_cookie_values:
        for chunk in raw.split(","):
            first = chunk.split(";")[0].strip()
            if "=" not in first:
                continue
            name, _, value = first.partition("=")
            if name.strip() == "prd-access-token" and value.strip():
                return value.strip()
    return ""
