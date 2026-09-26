"""小雅平台只读 API 客户端。

只封装查询类接口。任何写操作（提交任务、刷时长）都不在这里出现。
"""

from __future__ import annotations

from typing import Any

import httpx

from .models import Task, parse_task_list

DEFAULT_TIMEOUT = 20.0

# 平台按学校分配子域名，不同学校 client_id / redirect_uri 都不同。
# 目前只内置了作者本人所在的武汉理工大学（平台名「理工智课」）。
SCHOOLS: dict[str, dict[str, str]] = {
    "whut": {
        "label": "武汉理工大学（理工智课）",
        "host": "whut.ai-augmented.com",
        "client_id": "xy_client_whut",
        "redirect_uri": "https://whut.ai-augmented.com/api/jw-starcmooc/user/authorCallback",
        "school_code": "10497",
    },
    "ccnu": {
        "label": "华中师范大学（小雅）",
        "host": "ccnu.ai-augmented.com",
        "client_id": "xy_client_ccnu",
        "redirect_uri": "https://ccnu.ai-augmented.com/api/jw-starcmooc/user/authorCallback",
        "school_code": "10511",
    },
}
DEFAULT_SCHOOL = "whut"


class XiaoyaError(Exception):
    """接口调用失败。"""


class AuthExpired(XiaoyaError):
    """token 失效或缺失，需要重新登录。"""


class XiaoyaClient:
    """小雅课程平台只读客户端。

    认证方式：小雅网页登录后 cookie 里��� ``prd-access-token``，
    请求时以 ``Authorization: Bearer <token>`` 发送。
    """

    def __init__(
        self,
        token: str,
        school: str = DEFAULT_SCHOOL,
        timeout: float = DEFAULT_TIMEOUT,
        proxy: str | None = None,
    ) -> None:
        if school not in SCHOOLS:
            raise XiaoyaError(f"未知学校标识：{school}")
        self.token = token.strip()
        self.school_key = school
        self.school = SCHOOLS[school]
        self.host = self.school["host"]
        self.base = f"https://{self.host}"

        self._client = httpx.AsyncClient(
            timeout=timeout,
            proxy=proxy or None,
            follow_redirects=False,
            headers={
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json; charset=utf-8",
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

    async def __aenter__(self) -> XiaoyaClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---- 内部 ----

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.token}"}

    async def _get_json(self, path: str, **params: Any) -> Any:
        url = f"{self.base}{path}"
        try:
            resp = await self._client.get(url, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise XiaoyaError(f"网络请求失败：{exc}") from exc

        # 网关对未认证请求会回 SPA 的 index.html，据此判定 token 失效。
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype.lower():
            if resp.status_code in (401, 403):
                raise AuthExpired("凭证已被拒绝")
            raise AuthExpired("接口返回了非 JSON 内容，凭证可能已失效")
        if resp.status_code == 401:
            raise AuthExpired("token 已过期")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise XiaoyaError("响应不是合法 JSON") from exc

        if isinstance(payload, dict):
            code = payload.get("code")
            if code == 401:
                raise AuthExpired("token 已过期")
            if code == 200 or payload.get("success") is True:
                return payload.get("data", payload)
            message = payload.get("message") or payload.get("msg") or code
            raise XiaoyaError(f"接口返回错误：{message}")
        return payload

    # ---- 只读接口 ----

    async def whoami(self) -> dict[str, Any]:
        """验证 token 并返回用户信息。"""
        data = await self._get_json("/api/jx-auth/oauth2/info")
        if isinstance(data, dict):
            info = data.get("info")
            if isinstance(info, dict):
                return info
            return data
        return {}

    async def fetch_unfinished(self) -> list[Task]:
        """一次性拿到全部课程下所有未完成的任务。插件的 mainstay。"""
        data = await self._get_json("/api/jx-stat/group/task/un_finish")
        return parse_task_list(data)

    async def raw_unfinished(self) -> Any:
        """未完成任务列表的原始返回，不做解析。排查字段格式时用。"""
        return await self._get_json("/api/jx-stat/group/task/un_finish")

    async def fetch_course_tasks(self, group_id: str) -> list[Task]:
        """按课程细查任务列表（un_finish 拿不到时兜底）。"""
        data = await self._get_json(
            "/api/jx-stat/group/task/queryTaskNotices",
            group_id=group_id,
            role=1,
        )
        return parse_task_list(data)

    async def fetch_courses(self, time_flag: int = 1) -> list[dict[str, Any]]:
        """课程列表。``time_flag=1`` 在读，``3`` 已结束。"""
        data = await self._get_json(
            "/api/jx-iresource/group/student/groups", time_flag=time_flag
        )
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        return []

    async def check_token(self) -> tuple[bool, str]:
        """给指令用的快速自检，返回 (是否有效, 说明)。"""
        try:
            info = await self.whoami()
        except AuthExpired as exc:
            return False, str(exc)
        except XiaoyaError as exc:
            return False, str(exc)
        name = ""
        if isinstance(info, dict):
            name = str(
                info.get("name") or info.get("real_name") or info.get("nickname") or ""
            )
        return True, name or "有效"
