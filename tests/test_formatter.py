"""formatter 与 qrlogin 工具函数的测试。"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.formatter import (
    render_batch_new,
    render_digest,
    render_login_done,
    render_new_task,
    render_status,
    render_token_expired,
    render_urgent,
)
from core.models import Task, now
from core.qrlogin import (
    ST_CONFIRMED,
    ST_DENIED,
    STATUS_TEXT,
    _code_from_response,
    _token_from_cookies,
    extract_key,
    new_state,
)

HOST = "https://whut.ai-augmented.com"


def _task(**kw) -> Task:
    base = {
        "task_id": "t1",
        "name": "第一次作业",
        "group_name": "高等数学",
        "group_id": "77",
        "node_id": "88",
        "resource_id": "99",
        "task_type": 2,
        "end_time": now() + timedelta(hours=5),
    }
    base.update(kw)
    return Task(**base)


def test_render_digest_empty():
    out = render_digest([])
    assert "没有未完成任务" in out


def test_render_digest_groups_and_urgent_count():
    tasks = [
        _task(task_id="a", name="A", group_name="高数"),
        _task(task_id="b", name="B", group_name="英语", task_type=5),
    ]
    out = render_digest(tasks)
    assert "共 2 项" in out
    assert "2 项将在 24 小时内截止" in out
    assert "高数" in out and "英语" in out
    assert "课堂练习" in out and "问卷" in out


def test_render_digest_no_urgent_line():
    out = render_digest([_task(end_time=now() + timedelta(days=10))])
    assert "24 小时内截止" not in out


def test_render_new_task_single():
    out = render_new_task(_task(), HOST)
    assert "小雅有新任务了" in out
    assert "高等数学" in out
    assert "第一次作业" in out
    assert "/app/jx-web/mycourse" in out


def test_render_urgent():
    out = render_urgent(_task(), HOST)
    assert "快截止了" in out
    assert "还剩" in out


def test_render_batch_new_single_delegates():
    out = render_batch_new([_task()], HOST)
    assert "小雅有新任务了" in out


def test_render_batch_new_multiple():
    tasks = [
        _task(task_id="a", name="A"),
        _task(task_id="b", name="B", group_name="英语"),
    ]
    out = render_batch_new(tasks, HOST)
    assert "新增 2 项待办" in out
    assert "A" in out and "B" in out


def test_render_batch_new_empty():
    assert render_batch_new([]) == ""


def test_render_status_bound_and_unbound():
    out = render_status(False, "", 0, 0, 0, "武汉理工大学（理工智课）")
    assert "未绑定" in out
    assert "从未" in out

    out2 = render_status(True, "小明", 1.0, 1.0, 7, "x", "boom")
    assert "已绑定 小明" in out2
    assert "已推送任务：7 条" in out2
    assert "boom" in out2


def test_render_login_and_expired():
    assert "绑定成功" in render_login_done("小明", 7, 12, "A")
    assert "小雅登录已失效" in render_token_expired()


def test_extract_key():
    url = "https://infra.ai-augmented.com/app/auth/mobileScan?key=abc123&qrtype=LOGIN_BY_SCAN"
    assert extract_key(url) == "abc123"


def test_extract_key_missing():
    try:
        extract_key("https://example.com/noquery")
    except Exception as exc:
        assert "key" in str(exc)
    else:
        raise AssertionError("应该抛异常")


def test_new_state_shape():
    s = new_state()
    assert len(s) == 6
    assert s.isalnum()
    # 连续生成不应该每次都一样（概率上极小会撞，撞了也不影响功能）
    assert len({new_state() for _ in range(20)}) > 1


def test_status_text_covers_enum():
    for code in (0, 1, ST_CONFIRMED, ST_DENIED, 4):
        assert code in STATUS_TEXT


def test_code_from_response_location():
    class FakeResp:
        headers = {"location": "https://x.example/cb?code=THE_CODE&state=abc"}

    assert _code_from_response(FakeResp()) == "THE_CODE"


def test_code_from_response_empty():
    class FakeResp:
        headers = {}

    assert _code_from_response(FakeResp()) == ""


def test_token_from_cookies():
    cookies = [
        "other=1; Path=/; HttpOnly",
        "prd-access-token=JWT_VALUE_HERE; Path=/; HttpOnly; Secure",
    ]
    assert _token_from_cookies(cookies) == "JWT_VALUE_HERE"


def test_token_from_cookies_absent():
    assert _token_from_cookies(["foo=bar; Path=/"]) == ""
    assert _token_from_cookies([]) == ""
