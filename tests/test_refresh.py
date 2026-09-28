"""token 续期逻辑的测试。

续期接口是照着官方前端的实现反推的，字段名和请求格式都容易记错，
所以这里把响应结构、cookie 挑选、过期时间换算都钉住。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.qrlogin import (
    _cookie_value,
    _refresh_from_cookies,
    _token_from_cookies,
)
from core.refresh import (
    CST,
    REFRESH_TOKEN_TIME_MS,
    REMEMBER_ME_DAYS,
    RefreshFailed,
    TokenRefresher,
    _parse_ts,
)

ORG_TOKEN = "eGlhb3lhX3dodXQ6ODZh"
GAIN = {
    "code": 200,
    "result": {"reactAppOrgToken": ORG_TOKEN, "infraClientId": "xy_client_whut"},
}


def _resp(status: int = 200, body: Any = None) -> httpx.Response:
    if body is None:
        return httpx.Response(status)
    return httpx.Response(status, json=body)


def _client(handler) -> TokenRefresher:
    """造一个走 MockTransport 的 TokenRefresher。

    保留真实实例的默认请求头，否则测出来的 header 跟线上不一样，
    这种坑就漏掉了（比如 content-type 被 data= 覆盖那次）。
    """
    r = TokenRefresher(school="whut")
    defaults = dict(r._client.headers)
    r._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers=defaults
    )
    return r


# ---------------------------------------------------------------- cookie 挑选

REAL_COOKIES = [
    "WT-prd-access-token=AAA; Path=/; HttpOnly",
    "WT-prd-login-schoolId=f8d0cbf5; Path=/",
    "WT-prd-refresh-token=RRR; Path=/; HttpOnly",
    "WT-prd-refresh-token-state-v2=0",
    "WT-prd-rememberme=false",
]


def test_access_token_from_cookies():
    assert _token_from_cookies(REAL_COOKIES) == "AAA"


def test_refresh_token_from_cookies():
    assert _refresh_from_cookies(REAL_COOKIES) == "RRR"


def test_refresh_token_not_confused_with_state_flag():
    """``WT-prd-refresh-token-state-v2`` 的值是 0/1，不是凭证。"""
    only_flag = ["WT-prd-refresh-token-state-v2=0"]
    assert _refresh_from_cookies(only_flag) == ""
    assert _token_from_cookies(only_flag) == ""


def test_access_token_ignores_refresh_token():
    assert _token_from_cookies(["WT-prd-refresh-token=RRR; Path=/"]) == ""


def test_cookie_value_by_suffix():
    assert _cookie_value(REAL_COOKIES, "prd-rememberme") == "false"


def test_cookies_split_on_comma():
    packed = "WT-prd-access-token=AAA; Path=/, WT-prd-refresh-token=RRR; Path=/"
    assert _token_from_cookies([packed]) == "AAA"
    assert _refresh_from_cookies([packed]) == "RRR"


# ---------------------------------------------------------------- 时间解析


def test_parse_ts_utc():
    dt = _parse_ts("2026-09-29T01:02:10.829Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.astimezone(CST).hour == 9


def test_parse_ts_rejects_garbage():
    assert _parse_ts("not a date") is None
    assert _parse_ts("") is None
    assert _parse_ts(None) is None


# ---------------------------------------------------------------- 续期请求


def test_refresh_success():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "gainReactApp" in str(request.url):
            return _resp(200, GAIN)
        return _resp(
            200,
            {
                "access_token": "NEW_ACCESS",
                "refresh_token": "NEW_REFRESH",
                "access_token_expires_in": "2026-10-05T14:02:29.125Z",
                "refresh_token_expires_in": "2026-10-05T15:02:29.125Z",
                "success": True,
                "token_type": "Bearer",
            },
        )

    r = _client(handler)
    info = asyncio.run(r.refresh("OLD_REFRESH"))
    assert info.access_token == "NEW_ACCESS"
    assert info.refresh_token == "NEW_REFRESH"
    assert info.access_expires_at is not None

    token_req = next(q for q in seen if "oauth2/token" in str(q.url))
    assert token_req.headers["authorization"] == f"Basic {ORG_TOKEN}"
    assert token_req.headers["content-type"] == "application/x-www-form-urlencoded"
    body = token_req.content.decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=OLD_REFRESH" in body
    # 官方「记住我」就是 7 天，不多要
    assert f"token_time={REFRESH_TOKEN_TIME_MS}" in body
    assert REMEMBER_ME_DAYS == 7


def test_refresh_request_body_is_not_empty():
    """回归：曾经同时传 content= 和 data=，body 被吃成空，续期请求等于没带凭据。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "gainReactApp" in str(request.url):
            return _resp(200, GAIN)
        return _resp(200, {"access_token": "A", "success": True})

    asyncio.run(_client(handler).refresh("SOME_REFRESH"))
    token_req = next(q for q in seen if "oauth2/token" in str(q.url))
    assert token_req.content, "续期请求体不能是空的"
    assert b"grant_type=refresh_token" in token_req.content


def test_refresh_keeps_old_token_when_none_returned():
    def handler(request: httpx.Request) -> httpx.Response:
        if "gainReactApp" in str(request.url):
            return _resp(200, GAIN)
        return _resp(200, {"access_token": "A2", "success": True})

    r = _client(handler)
    info = asyncio.run(r.refresh("OLD"))
    assert info.refresh_token == "OLD"


def test_refresh_rejects_empty_token():
    r = _client(lambda q: _resp(200, GAIN))
    try:
        asyncio.run(r.refresh("  "))
    except RefreshFailed as exc:
        assert "重新扫码" in str(exc)
    else:
        raise AssertionError("空 refresh token 应该直接报错")


def test_refresh_401_says_rescan():
    def handler(request: httpx.Request) -> httpx.Response:
        if "gainReactApp" in str(request.url):
            return _resp(200, GAIN)
        return _resp(401)

    r = _client(handler)
    try:
        asyncio.run(r.refresh("DEAD"))
    except RefreshFailed as exc:
        assert "重新扫码" in str(exc)
    else:
        raise AssertionError("401 应该报需要重扫")


def test_refresh_business_error_surfaced():
    def handler(request: httpx.Request) -> httpx.Response:
        if "gainReactApp" in str(request.url):
            return _resp(200, GAIN)
        return _resp(200, {"message": "refresh_token 无效"})

    r = _client(handler)
    try:
        asyncio.run(r.refresh("BAD"))
    except RefreshFailed as exc:
        assert "refresh_token 无效" in str(exc)
    else:
        raise AssertionError("业务错误应该被报出来")


def test_org_token_cached():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "gainReactApp" in str(request.url):
            calls["n"] += 1
            return _resp(200, GAIN)
        return _resp(200, {"access_token": "A", "success": True})

    async def run():
        r = _client(handler)
        await r.refresh("R1")
        await r.refresh("R2")
        await r.aclose()

    asyncio.run(run())
    assert calls["n"] == 1, "orgToken 应该缓存，不该每轮都去取"


def test_missing_org_token_reports_clearly():
    r = _client(lambda q: _resp(200, {"code": 200, "result": {}}))
    try:
        asyncio.run(r.org_token())
    except RefreshFailed as exc:
        assert "reactAppOrgToken" in str(exc)
    else:
        raise AssertionError("缺 orgToken 应该报错")


# ---------------------------------------------------------------- 小时换算


def test_hours_left_counts_down():
    from core.refresh import TokenInfo

    future = datetime.now(CST) + timedelta(hours=5)
    info = TokenInfo("a", "r", future, future)
    assert 4.0 < info.hours_left("access") <= 5.0


def test_hours_left_zero_when_unknown():
    from core.refresh import TokenInfo

    info = TokenInfo("a", "r", None, None)
    assert info.hours_left("access") == 0.0


# ---------------------------------------------------------------- 实网验证


def test_live_refresh_against_real_endpoint():
    """拿真 refresh token 打一次真接口。

    没设 ``XY_REFRESH_TOKEN`` 就跳过，所以 CI 上不会跑。
    """
    rt = os.environ.get("XY_REFRESH_TOKEN", "").strip()
    if not rt:
        import pytest

        pytest.skip("没给 XY_REFRESH_TOKEN，跳过实网验证")

    r = TokenRefresher(school="whut", proxy=os.environ.get("XY_PROXY") or None)

    async def run():
        try:
            return await r.refresh(rt)
        finally:
            await r.aclose()

    info = asyncio.run(run())
    assert info.access_token
    # 实测：不带 token_time 只有 24 小时，带 7 天就是 7 天
    assert info.refresh_expires_at is not None
    left = info.hours_left("refresh")
    assert 160 < left < 180, f"续期凭证应该给 7 天左右，实际 {left:.1f} 小时"
    print(json.dumps({"refresh_hours": round(left, 1)}, ensure_ascii=False))
