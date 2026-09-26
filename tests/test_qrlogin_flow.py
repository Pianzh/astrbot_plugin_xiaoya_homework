"""扫码登录第三步（换 token）的链路测试。

这几步全是纯 HTTP 编排，用 httpx.MockTransport 把官方流程的每种走向都走一遍：
建会话 → onAccountAuthRedirect → 学校回调拿 cookie。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.qrlogin import QrLoginClient, QrLoginError

ACCOUNT = {
    "id": "acc-uuid-1",
    "isInCurrentInstance": True,
    "school": {"id": 7, "code": "10497", "name": "武汉理工大学"},
}
CALLBACK = "https://whut.ai-augmented.com/api/jw-starcmooc/user/authorCallback"


def _pick(accounts: list[dict]) -> dict:
    return QrLoginClient(school="whut")._pick_account(accounts)


def _resp(
    status: int = 200,
    json_body: Any = None,
    location: str = "",
    cookies: list[str] | None = None,
) -> httpx.Response:
    headers: dict[str, str] = {}
    if location:
        headers["location"] = location
    if cookies:
        headers["set-cookie"] = ", ".join(cookies)
    if json_body is None:
        return httpx.Response(status, headers=headers)
    return httpx.Response(status, headers=headers, json=json_body)


def _ok(code: int = 200, message: str = "ok", data: Any = None) -> dict:
    return {"code": code, "message": message, "data": data or {}}


def _client(handler) -> QrLoginClient:
    """造一个 QrLoginClient，把底层 httpx 换成 MockTransport。"""
    c = QrLoginClient(school="whut")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def _route(url: str) -> str:
    """把完整 URL 缩成末段路径，handler 里好写。"""
    return url.split("?")[0].removeprefix("https://infra.ai-augmented.com")


# ---------------------------------------------------------------- 选账号


def test_pick_prefers_matching_school_code():
    accounts = [
        {"id": "a", "school": {"code": "10511", "name": "华中师范大学"}},
        {"id": "b", "school": {"code": "10497", "name": "武汉理工大学"}},
    ]
    assert _pick(accounts)["id"] == "b"


def test_pick_falls_back_to_current_instance():
    accounts = [
        {"id": "a", "school": {"code": "10511", "name": "华师"}},
        {
            "id": "b",
            "school": {"code": "99999", "name": "别的"},
            "isInCurrentInstance": True,
        },
    ]
    assert _pick(accounts)["id"] == "b"


def test_pick_single_account():
    assert _pick([ACCOUNT])["id"] == "acc-uuid-1"


def test_pick_ambiguous_raises_with_names():
    accounts = [
        {"id": "a", "school": {"code": "1", "name": "学校甲"}},
        {"id": "b", "school": {"code": "2", "name": "学校乙"}},
    ]
    try:
        _pick(accounts)
    except QrLoginError as exc:
        assert "学校甲" in str(exc) and "学校乙" in str(exc)
        assert "/小雅绑定" in str(exc)
    else:
        raise AssertionError("多学校身份时应该报错")


def test_pick_ignores_malformed_accounts():
    """list_accounts 已经滤掉没有 id 的条目，_pick_account 只在该挑的里面挑。"""
    c = _client(
        lambda r: _resp(
            json_body=_ok(data={"accounts": [{"nope": 1}, ACCOUNT, "垃圾数据"]})
        )
    )
    import asyncio

    assert len(asyncio.run(c.list_accounts())) == 1
    assert _pick([ACCOUNT])["id"] == "acc-uuid-1"


# ---------------------------------------------------------------- 建会话


def test_select_account_happy_path():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = _route(str(request.url))
        if path == "/api/auth/login/listAccounts":
            return _resp(json_body=_ok(data={"accounts": [ACCOUNT]}))
        if path == "/api/auth/login/bySelectAccount":
            seen.append(json.loads(request.content))
            return _resp(json_body=_ok())
        raise AssertionError(f"不该打 {path}")

    c = _client(handler)
    assert asyncio.run(c.select_account()) == "acc-uuid-1"
    assert seen == [{"xyAccountId": "acc-uuid-1"}]


def test_list_accounts_not_logged_in():
    c = _client(
        lambda r: _resp(json_body={"code": 10009, "message": "未登录"})
    )
    try:
        asyncio.run(c.list_accounts())
    except QrLoginError as exc:
        assert "未登录" in str(exc)
    else:
        raise AssertionError("未登录时应该报错")


def test_select_account_rejects_bad_id():
    c = _client(
        lambda r: _resp(json_body={"code": 400, "message": "id 必须是uuid"})
    )
    try:
        asyncio.run(c.select_account())
    except QrLoginError as exc:
        assert "uuid" in str(exc)
    else:
        raise AssertionError("应该报错")


def test_select_account_empty_list():
    c = _client(lambda r: _resp(json_body=_ok(data={"accounts": []})))
    try:
        asyncio.run(c.select_account())
    except QrLoginError as exc:
        assert "没拿到任何账号" in str(exc)
    else:
        raise AssertionError("空列表应该报错")


# ---------------------------------------------------------------- 换 token


def _flow_handler(
    *,
    accounts: Any = None,
    redirect_code: str | None = "THE_CODE",
    redeem_cookies: tuple[str, ...] = ("WT-prd-access-token=THE_TOKEN; Path=/",),
    redeem_status: int = 200,
    redeem_body: Any = None,
):
    """官方正路的假服务端。

    ``redirect_code=None`` 模拟 onAccountAuthRedirect 回 403。
    """
    if accounts is None:
        accounts = _ok(data={"accounts": [ACCOUNT]})

    def handler(request: httpx.Request) -> httpx.Response:
        path = _route(str(request.url))
        if path == "/api/auth/login/listAccounts":
            return _resp(json_body=accounts)
        if path == "/api/auth/login/bySelectAccount":
            return _resp(json_body=_ok())
        if path == "/api/auth/oauth/onAccountAuthRedirect":
            if redirect_code is None:
                return _resp(403, json_body={"code": 403, "message": "Forbidden"})
            return _resp(302, location=_callback(redirect_code))
        if "authorCallback" in path:
            if redeem_body is not None:
                return _resp(redeem_status, json_body=redeem_body)
            return _resp(redeem_status, cookies=list(redeem_cookies))
        if path == "/api/auth/oauth/token":
            return _resp(401, json_body={"code": 401, "message": "Unauthorized"})
        if path == "/api/auth/oauth/authorize":
            return _resp(302, location="https://whut.ai-augmented.com/app/auth")
        raise AssertionError(f"不该打 {path}")

    return handler


def _callback(code: str) -> str:
    """官方 302 给的完整回调地址，schoolCode 是必填的。"""
    return f"{CALLBACK}?code={code}&state=st1234&schoolCode=10497"


def test_fetch_token_happy_path():
    c = _client(_flow_handler())
    result = asyncio.run(c.fetch_token("st1234"))
    assert result.token == "THE_TOKEN"
    assert result.method == "A"


def test_fetch_token_falls_back_to_authorize():
    """A 路 403 时，B 路重放 authorize 应该能兜住。"""
    base = _flow_handler(redirect_code=None)

    def handler(request: httpx.Request) -> httpx.Response:
        if _route(str(request.url)) == "/api/auth/oauth/authorize":
            return _resp(302, location=_callback("FALLBACK"))
        return base(request)

    c = _client(handler)
    result = asyncio.run(c.fetch_token("st1234"))
    assert result.method == "B"
    assert result.token == "THE_TOKEN"


def test_fetch_token_falls_back_to_token_endpoint():
    """A/B 都不行时，C 路直接找 token 端点。"""
    base = _flow_handler(redirect_code=None)

    def handler(request: httpx.Request) -> httpx.Response:
        path = _route(str(request.url))
        if path == "/api/auth/oauth/authorize":
            return _resp(302, location="https://whut.ai-augmented.com/app/auth")
        if path == "/api/auth/oauth/token":
            return _resp(200, json_body=_ok(data={"access_token": "C_PATH_TOKEN"}))
        return base(request)

    c = _client(handler)
    result = asyncio.run(c.fetch_token("st1234"))
    assert result.method == "C"
    assert result.token == "C_PATH_TOKEN"


def test_fetch_token_all_fail_reports_every_route():
    def handler(request: httpx.Request) -> httpx.Response:
        path = _route(str(request.url))
        if path == "/api/auth/login/listAccounts":
            return _resp(json_body={"code": 10009, "message": "未登录"})
        if path == "/api/auth/oauth/onAccountAuthRedirect":
            return _resp(403, json_body={"code": 403, "message": "Forbidden"})
        if path == "/api/auth/oauth/authorize":
            return _resp(302, location="https://whut.ai-augmented.com/app/auth")
        if path == "/api/auth/oauth/token":
            return _resp(401, json_body={"code": 401, "message": "Unauthorized"})
        if "authorCallback" in path:
            return _resp(200, cookies=["SESSION=abc; Path=/"])
        raise AssertionError(path)

    c = _client(handler)
    try:
        asyncio.run(c.fetch_token("st1234"))
    except QrLoginError as exc:
        msg = str(exc)
        assert "建会话" in msg
        assert "A 路" in msg and "B 路" in msg and "C 路" in msg
        assert "403" in msg
    else:
        raise AssertionError("全挂时应该报错")


def test_fetch_token_missing_cookie_reports_it():
    c = _client(_flow_handler(redeem_cookies=("SESSION=abc; Path=/",)))
    try:
        asyncio.run(c.fetch_token("st1234"))
    except QrLoginError as exc:
        assert "学校回调" in str(exc)
    else:
        raise AssertionError("没有 token cookie 时应该报错")


# ------------------------------------------------------- 回调地址要原样用


def test_callback_url_keeps_every_param():
    """schoolCode 是必填的，自己拼 URL 漏掉就会 500。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "authorCallback" in str(request.url):
            seen.append(str(request.url))
            return _resp(200, cookies=["WT-prd-access-token=T; Path=/"])
        return _flow_handler()(request)

    asyncio.run(_client(handler).fetch_token("st1234"))
    assert seen, "回调根本没被访问"
    assert "schoolCode=10497" in seen[0]
    assert "code=THE_CODE" in seen[0]
    assert "state=st1234" in seen[0]


def test_callback_url_rejects_foreign_host():
    """302 指到别的域名就别跟，别把 code 送出去。"""
    c = _client(
        lambda r: _resp(302, location="https://evil.example.com/steal?code=X")
    )
    try:
        asyncio.run(c._callback_url(
            httpx.Response(302, headers={"location": "https://evil.example.com/steal?code=X"})
        ))
    except QrLoginError as exc:
        assert "evil.example.com" in str(exc)
    else:
        raise AssertionError("外域跳转应该被拒")


def test_callback_url_empty_without_code():
    c = QrLoginClient(school="whut")
    resp = httpx.Response(302, headers={"location": "https://whut.ai-augmented.com/app"})
    assert c._callback_url(resp) == ""


# ------------------------------------------------------- 回调的业务错误


def test_callback_business_error_is_surfaced():
    """whut 侧用 {"code":10010,"msg":"..."} 报错，要把话转述给用户。"""
    c = _client(
        _flow_handler(
            redeem_status=200,
            redeem_body={"code": 10010, "msg": "授权码已过期或不存在", "result": None},
        )
    )
    try:
        asyncio.run(c.fetch_token("st1234"))
    except QrLoginError as exc:
        assert "授权码已过期或不存在" in str(exc)
    else:
        raise AssertionError("业务错误应该被报出来")


def test_callback_500_is_surfaced():
    c = _client(_flow_handler(redeem_status=500, redeem_body={"code": 500, "msg": "系统异常。"}))
    try:
        asyncio.run(c.fetch_token("st1234"))
    except QrLoginError as exc:
        assert "500" in str(exc)
    else:
        raise AssertionError("5xx 应该被报出来")


# ---------------------------------------------------------------- 诊断


def test_diag_records_steps():
    c = _client(_flow_handler())
    asyncio.run(c.fetch_token("st1234"))
    text = c.diag_text()
    assert "listAccounts" in text
    assert "bySelectAccount" in text
    assert "onAccountAuthRedirect" in text
    assert "学校回调" in text
    assert "prd-access-token" in text


def test_diag_empty_when_nothing_happened():
    assert QrLoginClient(school="whut").diag_text() == ""
