"""models 模块的测试。"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import (
    Task,
    filter_by_window,
    group_by_course,
    now,
    parse_task_list,
    parse_ts,
    sort_tasks,
)


def _ts(offset_days: float = 1.0) -> str:
    return (now() + timedelta(days=offset_days)).strftime("%Y-%m-%d %H:%M:%S")


def test_parse_ts_formats():
    assert parse_ts("2024-05-06 13:00:00") is not None
    assert parse_ts("2024-05-06T13:00:00") is not None
    assert parse_ts("not a date") is None
    assert parse_ts(None) is None
    assert parse_ts(1715000000) is not None


def test_parse_task_list_shapes():
    flat = [{"task_id": 1, "name": "a", "end_time": _ts()}]
    nested = {"data": {"student_tasks": [{"task_id": 2, "name": "b"}]}}
    listed = {"data": [{"task_id": 3, "name": "c"}]}

    assert len(parse_task_list(flat)) == 1
    assert len(parse_task_list(nested)) == 1
    assert len(parse_task_list(listed)) == 1
    assert parse_task_list(None) == []
    assert parse_task_list({"data": {}}) == []


def test_task_type_mapping():
    task = Task(task_id="1", name="x", task_type=2)
    assert task.type_name == "课堂练习"
    assert Task(task_id="1", name="x", task_type=99).type_name == "任务"


def test_done_flag():
    assert Task(task_id="1", name="x", finish=2).done is True
    assert Task(task_id="1", name="x", finish=0).done is False


def test_weight_ordering():
    soon = Task(task_id="a", name="soon", end_time=now() + timedelta(hours=5))
    mid = Task(task_id="b", name="mid", end_time=now() + timedelta(days=2))
    far = Task(task_id="c", name="far", end_time=now() + timedelta(days=30))
    past = Task(task_id="d", name="past", end_time=now() - timedelta(days=1))

    assert soon.weight == 100
    assert mid.weight == 50
    assert far.weight == 10
    assert past.weight == -1

    ordered = sort_tasks([far, past, mid, soon])
    assert [t.task_id for t in ordered] == ["a", "b", "c", "d"]


def test_key_fallback():
    t = Task(task_id="", name="n", group_id="g", node_id="nd", resource_id="rs")
    assert t.key == "g:nd:rs:n"
    assert Task(task_id="9", name="n").key == "9"


def test_filter_by_window():
    tasks = [
        Task(task_id="a", name="in", end_time=now() + timedelta(hours=5)),
        Task(task_id="b", name="out", end_time=now() + timedelta(days=10)),
        Task(task_id="c", name="done", end_time=now() + timedelta(hours=5), finish=2),
        Task(task_id="d", name="past", end_time=now() - timedelta(days=1)),
    ]
    got = {t.task_id for t in filter_by_window(tasks)}
    assert got == {"a", "b"}

    got3 = {t.task_id for t in filter_by_window(tasks, 3)}
    assert got3 == {"a"}


def test_group_by_course():
    tasks = [
        Task(task_id="a", name="a", group_name="高数", end_time=now() + timedelta(hours=2)),
        Task(task_id="b", name="b", group_name="高数", end_time=now() + timedelta(hours=9)),
        Task(task_id="c", name="c", group_name="英语", end_time=now() + timedelta(hours=3)),
    ]
    grouped = group_by_course(tasks)
    assert set(grouped) == {"高数", "英语"}
    assert [t.task_id for t in grouped["高数"]] == ["a", "b"]


def test_deadline_text_variants():
    assert "过期" in Task(task_id="1", name="x", end_time=now() - timedelta(days=2)).deadline_text()
    assert "还剩" in Task(task_id="1", name="x", end_time=now() + timedelta(hours=5)).deadline_text()
    assert Task(task_id="1", name="x").deadline_text() == "无截止时间"


def test_urls():
    t = Task(
        task_id="1", name="x", group_id="77", node_id="88", resource_id="99"
    )
    assert t.detail_url("https://h.example") == (
        "https://h.example/app/jx-web/mycourse/77/resource/99/88"
    )
    assert t.course_url("https://h.example").endswith("/mycourse/77")
    assert Task(task_id="1", name="x").detail_url("https://h") == ""


def test_urls_add_scheme_when_missing():
    """QQ 只认带协议头的可点链接，少了 https:// 就点不开。"""
    t = Task(task_id="1", name="x", group_id="77", node_id="88", resource_id="99")
    assert t.detail_url("whut.ai-augmented.com").startswith("https://whut.")
    assert t.course_url("whut.ai-augmented.com") == (
        "https://whut.ai-augmented.com/app/jx-web/mycourse/77"
    )
    # 已经带协议头的不该被重复加
    assert t.course_url("https://whut.ai-augmented.com").count("https://") == 1
    # 末尾多个斜杠也不能拼出双斜杠
    assert "//app" not in t.course_url("https://h.example/").split("://", 1)[1]
