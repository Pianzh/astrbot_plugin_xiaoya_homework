"""小雅平台任务的数据模型与分类/紧急度规则。

字段命名沿用小雅 ``jx-stat/group/task`` 接口的原始返回，不做改写，
方便日后对照官方文档或抓包排查。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

# 小雅的时间字段是 "2024-05-06 13:00:00" 这种东八区裸字符串，
# 不是 ISO8601，所以不能用 fromisoformat 直接解。
_TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")

CST = timezone(timedelta(hours=8))

# task_type -> (图标, 中文名)。取自 XiaoYaEasyTasks 用户脚本的映射表。
TASK_TYPES: dict[int, tuple[str, str]] = {
    1: ("\U0001F4FA", "自主观看"),
    2: ("✍️", "课堂练习"),
    3: ("\U0001F4DA", "试卷"),
    4: ("\U0001F4AF", "测验"),
    5: ("\U0001F4CB", "问卷"),
    6: ("\U0001F4AD", "讨论"),
}
UNKNOWN_TYPE = ("\U0001F4CC", "任务")

# finish == 2 表示已完成
FINISH_DONE = 2


def parse_ts(value: Any) -> datetime | None:
    """把平台的时间字符串解析成带时区的 datetime。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=CST)
    if isinstance(value, (int, float)):
        # 毫秒时间戳
        sec = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(sec, CST)
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    m = _TS_RE.search(value)
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()), tzinfo=CST)
    except ValueError:
        return None


def now() -> datetime:
    return datetime.now(CST)


@dataclass(slots=True)
class Task:
    """一条未完成（或任意状态）的课程任务。"""

    task_id: str
    name: str
    group_id: str = ""
    group_name: str = "未知课程"
    task_type: int = 0
    finish: int = 0
    start_time: datetime | None = None
    end_time: datetime | None = None
    node_id: str = ""
    resource_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # ---- 派生属性 ----

    @property
    def done(self) -> bool:
        return self.finish == FINISH_DONE

    @property
    def icon(self) -> str:
        return TASK_TYPES.get(self.task_type, UNKNOWN_TYPE)[0]

    @property
    def type_name(self) -> str:
        return TASK_TYPES.get(self.task_type, UNKNOWN_TYPE)[1]

    @property
    def key(self) -> str:
        """去重用的稳定标识。优先用 task_id，缺失时退回节点/资源 id 组合。"""
        if self.task_id:
            return str(self.task_id)
        return f"{self.group_id}:{self.node_id}:{self.resource_id}:{self.name}"

    @property
    def weight(self) -> int:
        """排序权重，越大越紧急。抄自 XiaoYaEasyTasks 的 getTaskWeight。"""
        if self.end_time is None:
            return 0
        left = self.end_time - now()
        if left.total_seconds() < 0:
            return -1
        if left < timedelta(days=1):
            return 100
        if left < timedelta(days=3):
            return 50
        return 10

    @property
    def is_overdue(self) -> bool:
        return self.end_time is not None and self.end_time < now()

    @property
    def hours_left(self) -> float:
        if self.end_time is None:
            return float("inf")
        return (self.end_time - now()).total_seconds() / 3600.0

    def deadline_text(self) -> str:
        """人类可读的截止描述，例如「还剩 5 小时」「已过期 2 天」。"""
        if self.end_time is None:
            return "无截止时间"
        delta = self.end_time - now()
        if delta.total_seconds() < 0:
            over = -delta
            if over < timedelta(hours=1):
                return "刚刚过期"
            if over < timedelta(days=1):
                return f"已过期 {int(over.total_seconds() // 3600)} 小时"
            return f"已过期 {over.days} 天"
        if delta < timedelta(hours=1):
            return f"还剩 {max(1, int(delta.total_seconds() // 60))} 分钟"
        if delta < timedelta(days=1):
            return f"还剩 {int(delta.total_seconds() // 3600)} 小时"
        return f"还剩 {delta.days} 天"

    def detail_url(self, host: str) -> str:
        """拼出在浏览器里直接打开该任务的链接。"""
        if not self.group_id:
            return ""
        parts = [_site(host), "app", "jx-web", "mycourse", self.group_id]
        if self.resource_id:
            parts.extend(["resource", self.resource_id])
            if self.node_id:
                parts.append(self.node_id)
        return "/".join(parts)

    def course_url(self, host: str) -> str:
        if not self.group_id:
            return ""
        return "/".join([_site(host), "app", "jx-web", "mycourse", self.group_id])

    # ---- 构造 ----

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> Task:
        return cls(
            task_id=str(data.get("task_id") or data.get("id") or ""),
            name=str(data.get("name") or data.get("task_name") or "未命名任务"),
            group_id=str(data.get("group_id") or ""),
            group_name=str(data.get("group_name") or "未知课程"),
            task_type=_as_int(data.get("task_type")),
            finish=_as_int(data.get("finish")),
            start_time=parse_ts(data.get("start_time")),
            end_time=parse_ts(data.get("end_time")),
            node_id=str(data.get("node_id") or ""),
            resource_id=str(data.get("resource_id") or ""),
            raw=data,
        )


def _site(host: str) -> str:
    """补全协议头。QQ 里只有带 https:// 的地址才认成可点链接。"""
    host = (host or "").strip().rstrip("/")
    if not host:
        return ""
    if "://" not in host:
        host = f"https://{host}"
    return host


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_task_list(payload: Any) -> list[Task]:
    """从小雅各种形状的返回里尽最大努力抠出任务数组。

    ``jx-stat/group/task/un_finish`` 直接返回数组；
    ``queryTaskNotices`` 藏在 ``data.student_tasks`` 里。两种都兼容。
    """
    items = _locate_task_array(payload)
    tasks: list[Task] = []
    for item in items:
        if isinstance(item, dict):
            tasks.append(Task.from_raw(item))
    return tasks


def _locate_task_array(payload: Any) -> Iterable[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for field in ("student_tasks", "tasks", "list", "records", "items"):
            value = data.get(field)
            if isinstance(value, list):
                return value
    return []


def sort_tasks(tasks: Iterable[Task]) -> list[Task]:
    """按紧急度降序；同权重按截止时间升序。"""
    return sorted(
        tasks,
        key=lambda t: (-t.weight, t.end_time or now() + timedelta(days=3650)),
    )


def group_by_course(tasks: Iterable[Task]) -> dict[str, list[Task]]:
    """按课程名分组，组内按紧急度排好。"""
    buckets: dict[str, list[Task]] = {}
    for task in sort_tasks(tasks):
        buckets.setdefault(task.group_name, []).append(task)
    return buckets


def filter_by_window(
    tasks: Iterable[Task], days: float | None = None
) -> list[Task]:
    """只保留未完成、且（可选）N 天内截止的任务。过期的一律剔除。"""
    result: list[Task] = []
    for task in tasks:
        if task.done or task.is_overdue:
            continue
        if days is not None and task.end_time is not None and (
            task.end_time > now() + timedelta(days=days)
        ):
            continue
        result.append(task)
    return sort_tasks(result)


def urlencode(value: str) -> str:
    return quote(value, safe="")
