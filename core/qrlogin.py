"""小雅扫码登录。

流程照抄小雅官方登录页 ``infra.ai-augmented.com/app/auth`` 的前端实现：

1. ``GET  /api/auth/qrLogin/getCode``       拿二维码地址，二维码 key 藏在 query 里
2. ``GET  /api/auth/qrLogin/getCodeStatus`` 每秒轮询，status 2 表示已确认

   状态翻到 2 的这一刻，服务端会把 infra 会话 cookie 下发给「正在轮询的那个客户端」。
   httpx 的 cookie jar 会自动存下来，后面所有请求都带着它。

3. 扫码成功后官方前端并不调接口，而是跳到 ``/oauth2/securityNotice``。那个页面会：
   a. ``GET  /api/auth/login/listAccounts``    列出可登录账号
   b. ``POST /api/auth/login/bySelectAccount``  body ``{"xyAccountId": "..."}`` 建立会话
   c. ``GET  /api/auth/oauth/onAccountAuthRedirect``  不带任何参数，服务端凭会话 302
      到 ``redirect_uri?code=...&state=...``
4. ``GET  <redirect_uri>?code=...`` 从 ``Set-Cookie`` 里取 ``prd-access-token``

第 3 步的 a/b 两步是关键：少了它们，服务端那边根本没有已登录会话，
``onAccountAuthRedirect`` 只会回 403 Forbidden。

官方是浏览器跳转，没有稳定的公开接口，所以 3c/4 之后还留了两条退路。
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
TOKEN_PATH = "/api/auth/oauth/token"

# 扫码成功后建立 infra 会话要用到的两个
LIST_ACCOUNTS_PATH = "/api/auth/login/listAccounts"
SELECT_ACCOUNT_PATH = "/api/auth/login/bySelectAccount"

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
        self.diag: list[str] = []
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

    # ---- 诊断 ----

    def _note(self, step: str, resp: httpx.Response) -> None:
        """记一条请求结果。登录失败时把这些推给用户，一次扫描就能定位问题。"""
        cookies = [
            c.split("=", 1)[0].strip()
            for c in resp.headers.get_list("set-cookie")
            if "=" in c
        ]
        location = resp.headers.get("location", "")
        bits = [f"HTTP {resp.status_code}"]
        if location:
            bits.append(f"→ {location[:120]}")
        if cookies:
            bits.append("set-cookie: " + ",".join(cookies))
        jar = sorted(self._client.cookies.keys())
        if jar:
            bits.append("jar: " + ",".join(jar))
        body = ""
        try:
            body = resp.text.strip().replace("\n", " ")[:160]
        except Exception:
            body = "<读不出 body>"
        if body:
            bits.append(f"body: {body}")
        self.diag.append(f"[{step}] " + " | ".join(bits))

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
        self._note("getCodeStatus", resp)
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

    # ---- 步骤 3a/3b：建立 infra 会话 ----

    async def list_accounts(self) -> list[dict]:
        """扫码确认后服务端才会认，返回可登录账号列表。"""
        try:
            resp = await self._client.get(AUTH_BASE + LIST_ACCOUNTS_PATH)
        except httpx.HTTPError as exc:
            raise QrLoginError(f"取账号列表失败：{exc}") from exc
        self._note("listAccounts", resp)
        payload = _json(resp)
        if payload.get("code") != 200:
            raise QrLoginError(
                f"取账号列表被拒：{payload.get('message') or payload.get('code')}"
            )
        data = payload.get("data") or {}
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(accounts, list):
            return []
        return [
            a
            for a in accounts
            if isinstance(a, dict) and str(a.get("id") or "").strip()
        ]

    def _pick_account(self, accounts: list[dict]) -> dict:
        """挑一个账号：先按学校代码，再按当前实例，最后按「只有一个」。"""
        want = str(self.school["school_code"])
        for acc in accounts:
            if _school_code(acc) == want:
                return acc
        for acc in accounts:
            if acc.get("isInCurrentInstance"):
                return acc
        if len(accounts) == 1:
            return accounts[0]
        names = "、".join(
            f"{_school_name(a)}({_school_code(a) or '?'})" for a in accounts
        )
        raise QrLoginError(
            f"这个手机号绑了多个学校身份（{names}），插件不知道该选哪个。"
            f"请在小雅 App 里只保留{self.school['label']}，或手动绑定：/小雅绑定 <token>"
        )

    async def select_account(self) -> str:
        """选定账号，这一步才真正建立 infra 会话。返回选中的账号 id。"""
        accounts = await self.list_accounts()
        if not accounts:
            raise QrLoginError("扫码后没拿到任何账号，可能是 App 端还没确认完")
        account = self._pick_account(accounts)
        account_id = str(account.get("id") or "").strip()
        if not account_id:
            raise QrLoginError("账号列表里没有 id 字段")

        body = {"xyAccountId": account_id}
        try:
            resp = await self._client.post(
                AUTH_BASE + SELECT_ACCOUNT_PATH, json=body
            )
        except httpx.HTTPError as exc:
            raise QrLoginError(f"选定账号失败：{exc}") from exc
        self._note("bySelectAccount", resp)
        payload = _json(resp)
        if payload.get("code") != 200:
            raise QrLoginError(
                f"选定账号被拒：{payload.get('message') or payload.get('code')}"
            )
        return account_id

    # ---- 步骤 3c/3d：换 code，再换 token ----

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
        """A 路（官方正路）：onAccountAuthRedirect 不带参数，服务端凭会话 302。"""
        resp = await self._client.get(
            AUTH_BASE + ON_REDIRECT_PATH,
            headers={"referer": f"{AUTH_BASE}/app/auth/oauth2/securityNotice"},
        )
        self._note("onAccountAuthRedirect", resp)
        if resp.status_code == 403:
            raise QrLoginError("onAccountAuthRedirect 回了 403，说明 infra 会话没建起来")
        return self._callback_url(resp)

    async def _exchange_b(self, state: str) -> str:
        """B 路：重放 authorize，同一个 state 下已登录会直接跳回 redirect_uri。"""
        resp = await self._client.get(
            AUTH_BASE + AUTHORIZE_PATH, params=self._oauth_params(state)
        )
        self._note("authorize", resp)
        return self._callback_url(resp)

    def _callback_url(self, resp: httpx.Response) -> str:
        """从 302 里取出学校回调地址。

        官方浏览器的做法就是直接跳过去，所以这里原样保留 Location 上的
        每一个查询参数——``schoolCode`` 是必填的，自己拼 URL 会漏掉它，
        whut 侧就直接 500「系统异常」。
        """
        url = resp.headers.get("location", "")
        if not url:
            return ""
        parsed = urlparse(url)
        if not parse_qs(parsed.query).get("code"):
            return ""
        # 只允许跳到本校站点，别把 code 送到别处去
        if parsed.hostname not in (self.host, f"www.{self.host}"):
            raise QrLoginError(f"302 指向了意料之外的域名：{parsed.hostname}")
        return url

    async def _exchange_c(self, state: str) -> str:
        """C 路：直接找 infra 的 token 端点换 access_token，不走学校回调。

        这个端点确实存在（缺凭据时回 401），但官方前端不用它，
        所以只作为兜底。
        """
        resp = await self._client.post(
            AUTH_BASE + TOKEN_PATH,
            json={
                "grant_type": "authorization_code",
                "client_id": self.school["client_id"],
                "redirect_uri": self.redirect_uri,
                "state": state,
            },
        )
        self._note("oauth/token", resp)
        payload = _json(resp)
        data = payload.get("data") or {}
        if isinstance(data, dict):
            for field in ("access_token", "prd-access-token", "token"):
                value = data.get(field)
                if value:
                    return str(value)
        raise QrLoginError(
            f"token 端点：{payload.get('message') or payload.get('code') or resp.status_code}"
        )

    async def fetch_token(self, state: str) -> QrLoginResult:
        """换 token。

        先按官方流程建立 infra 会话，再依次试各条换 token 的路。
        每条路失败的原因都收进异常信息，方便对着日志排查。
        """
        errors: list[str] = []
        try:
            account_id = await self.select_account()
        except QrLoginError as exc:
            account_id = ""
            errors.append(f"建会话：{exc}")

        # A/B：先拿到学校回调地址，再访问它换 token
        for method, fn in (("A", lambda: self._exchange_a(state)),
                           ("B", lambda: self._exchange_b(state))):
            try:
                url = await fn()
            except QrLoginError as exc:
                errors.append(f"{method} 路：{exc}")
                continue
            except httpx.HTTPError as exc:
                errors.append(f"{method} 路：{exc}")
                continue
            if not url:
                errors.append(f"{method} 路：跳转里没有带 code 的回调地址")
                continue
            try:
                token = await self._redeem(url)
            except QrLoginError as exc:
                errors.append(f"{method} 路：{exc}")
                continue
            # _redeem 拿不到 token 一定抛异常，走到这里就是成功了
            return QrLoginResult(token=token, method=method)  # type: ignore[arg-type]

        # C：直接换 token
        try:
            token = await self._exchange_c(state)
        except QrLoginError as exc:
            errors.append(f"C 路：{exc}")
        else:
            if token:
                return QrLoginResult(token=token, method="C")  # type: ignore[arg-type]

        detail = "；".join(errors)
        if not account_id:
            detail += "。建会话那步就没过，后面几条路基本是徒劳，请把下面这段发给插件作者"
        raise QrLoginError("扫码确认了，但换 token 失败：" + detail)

    def diag_text(self, limit: int = 10) -> str:
        """把诊断记录拼成一段能直接发出去的文本。"""
        if not self.diag:
            return ""
        lines = self.diag[-limit:]
        return "── 诊断 ──\n" + "\n".join(lines)

    async def _redeem(self, url: str) -> str:
        """访问学校侧回调，从 Set-Cookie 里抓 prd-access-token。

        ``url`` 是 302 里给的完整地址，原样请求，不自己拼。
        """
        try:
            # 这个请求打到学校站点，不能再带 infra 的 origin
            resp = await self._client.get(
                url,
                headers={
                    "referer": f"{AUTH_BASE}/app/auth/",
                    "origin": "",
                    "accept": "application/json, text/plain, */*",
                },
            )
        except httpx.HTTPError as exc:
            raise QrLoginError(f"回调失败：{exc}") from exc
        self._note("学校回调", resp)

        token = _token_from_cookies(resp.headers.get_list("set-cookie"))
        if token:
            return token
        # 有些部署会把 token 直接放在重定向 body 里
        location = resp.headers.get("location")
        if location:
            token = _token_from_cookies([location])
            if token:
                return token
        # whut 这类学校站点用 {"code":10010,"msg":"..."} 报业务错误，
        # 直接把它的话带给用户，比一句「没拿到 token」有用得多
        raise QrLoginError(self._callback_error(resp))

    def _callback_error(self, resp: httpx.Response) -> str:
        """把学校回调的失败原因翻成人话。"""
        if resp.status_code >= 500:
            return f"学校回调返回 {resp.status_code}，服务端炸了"
        try:
            payload = resp.json()
        except ValueError:
            return f"学校回调返回 {resp.status_code}，body 不是 JSON"
        if not isinstance(payload, dict):
            return f"学校回调返回 {resp.status_code}，body 结构异常"
        code = payload.get("code")
        msg = payload.get("msg") or payload.get("message")
        if code in (None, 200) and not msg:
            return f"学校回调返回 {resp.status_code}，body 里没有错误信息"
        return f"学校回调拒绝了：{msg or code}"

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


def _school_of(account: dict) -> dict:
    school = account.get("school")
    return school if isinstance(school, dict) else {}


def _school_code(account: dict) -> str:
    return str(_school_of(account).get("code") or "")


def _school_name(account: dict) -> str:
    return str(_school_of(account).get("name") or "未知学校")


def _token_from_cookies(set_cookie_values: list[str]) -> str:
    """从 Set-Cookie 里找 access token。

    学校站点实际下发的是 ``WT-prd-access-token``，不同学校前缀不一样
    （infra 侧叫 ``XY_AUTH_SESSION``），所以按后缀匹配，别写死全名。
    """
    for raw in set_cookie_values:
        for chunk in raw.split(","):
            first = chunk.split(";")[0].strip()
            if "=" not in first:
                continue
            name, _, value = first.partition("=")
            name = name.strip()
            value = value.strip()
            if not value or not name.endswith("prd-access-token"):
                continue
            # 别把 refresh token 误当成 access token
            if "refresh" in name:
                continue
            return value
    return ""
