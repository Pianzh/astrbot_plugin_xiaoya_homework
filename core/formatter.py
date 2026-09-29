"""把任务列表渲染成 QQ 里好看的纯文本。"""

from __future__ import annotations

from datetime import datetime

from .models import CST, Task, group_by_course, sort_tasks

LINE = "─" * 22


def _fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return "未知"
    return dt.strftime("%m-%d %H:%M")


def _status_tag(task: Task) -> str:
    if task.hours_left < 0:
        return "⚠️已过期"
    if task.hours_left < 24:
        return "❗紧急"
    if task.hours_left < 72:
        return "⏳临近"
    return "🟢宽裕"


def render_task_line(task: Task) -> str:
    return (
        f"  {task.icon} {task.name}\n"
        f"     [{task.type_name}] {_status_tag(task)} "
        f"截止 {_fmt_time(task.end_time)} · {task.deadline_text()}"
    )


def render_digest(tasks: list[Task], title: str = "小雅待办") -> str:
    """完整清单：按课程分组，组内按紧急度排。"""
    if not tasks:
        return f"【{title}】\n{LINE}\n干干净净，没有未完成任务。"

    grouped = group_by_course(tasks)
    total = sum(len(v) for v in grouped.values())
    urgent = sum(1 for t in tasks if t.hours_left < 24)

    lines = [f"【{title}】共 {total} 项"]
    if urgent:
        lines.append(f"❗ 其中 {urgent} 项将在 24 小时内截止")
    lines.append(LINE)

    for course, items in grouped.items():
        lines.append(f"\U0001F4D6 {course}（{len(items)}）")
        for task in items:
            lines.append(render_task_line(task))
        lines.append("")

    return "\n".join(lines).rstrip()


def render_new_task(task: Task, host: str = "") -> str:
    """单条新任务提醒。"""
    lines = [
        f"\U0001F195 小雅有新任务了 · {task.type_name}",
        LINE,
        f"课程：{task.group_name}",
        f"任务：{task.name}",
        f"截止：{_fmt_time(task.end_time)}（{task.deadline_text()}）",
    ]
    url = task.detail_url(host) if host else task.course_url(host or "")
    if url:
        lines.append(f"链接：{url}")
    return "\n".join(lines)


def render_urgent(task: Task, host: str = "") -> str:
    """临期催办。"""
    lines = [
        f"⏰ 快截止了 · {task.type_name}",
        LINE,
        f"课程：{task.group_name}",
        f"任务：{task.name}",
        f"截止：{_fmt_time(task.end_time)}（{task.deadline_text()}）",
    ]
    url = task.detail_url(host) if host else ""
    if url:
        lines.append(f"链接：{url}")
    return "\n".join(lines)


def render_batch_new(tasks: list[Task], host: str = "") -> str:
    """一次扫出多条新任务时合并推送，避免刷屏。"""
    if not tasks:
        return ""
    if len(tasks) == 1:
        return render_new_task(tasks[0], host)

    items = sort_tasks(tasks)
    urgent = sum(1 for t in items if t.hours_left < 24)
    header = f"\U0001F195 小雅新增 {len(items)} 项待办"
    if urgent:
        header += f"，其中 {urgent} 项 24 小时内截止"
    lines = [header, LINE]
    for task in items:
        lines.append(f"  {task.icon} [{task.group_name}] {task.name}")
        lines.append(f"     截止 {_fmt_time(task.end_time)} · {task.deadline_text()}")
    return "\n".join(lines)


def render_status(
    bound: bool,
    user_name: str,
    last_check: float,
    last_success: float,
    notified: int,
    school_label: str,
    error: str = "",
    push_enabled: bool = True,
    push_target: str = "",
    auto_refresh: bool = True,
    has_refresh_token: bool = False,
    access_hours: float | None = None,
    refresh_hours: float | None = None,
    transient_failures: int = 0,
) -> str:
    def ts(value: float) -> str:
        if value <= 0:
            return "从未"
        return datetime.fromtimestamp(value, CST).strftime("%m-%d %H:%M")

    def left(hours: float | None, unknown: str) -> str:
        if hours is None:
            return unknown
        if hours <= 0:
            return "已过期"
        if hours < 1:
            return f"{hours * 60:.0f} 分钟"
        if hours < 48:
            return f"{hours:.0f} 小时"
        return f"{hours / 24:.1f} 天"

    lines = ["【小雅助手 · 状态】", LINE]
    lines.append(f"学校：{school_label}")
    lines.append(f"凭证：{'已绑定 ' + user_name if bound else '未绑定'}")
    lines.append(f"定时推送：{'开' if push_enabled else '关'}")
    if push_target:
        lines.append(f"推送目标：{push_target}")
    lines.append(f"自动续期：{'开' if auto_refresh else '关'}")
    if not has_refresh_token:
        lines.append("⚠ 没有续期凭证，过期后需要重新扫码")
    else:
        lines.append(f"访问凭证：{left(access_hours, '有效期未知')}")
        lines.append(f"续期凭证：{left(refresh_hours, '有效期未知')}")
    lines.append(f"上次检查：{ts(last_check)}")
    lines.append(f"上次成功：{ts(last_success)}")
    lines.append(f"已推送任务：{notified} 条")
    if transient_failures:
        lines.append(f"平台异常：连续 {transient_failures} 次没拉到数据")
    if error:
        lines.append(f"最近错误：{error}")
    return "\n".join(lines)


def render_login_pending(ttl: int) -> str:
    return (
        "\U0001F4F1 小雅登录\n"
        f"{LINE}\n"
        f"用「小雅」App 扫下面这个码，{ttl} 秒内有效。\n"
        "打开 App → 首页右上角 → 扫一扫"
    )


def render_login_done(user_name: str, courses: int, tasks: int, method: str) -> str:
    return (
        "✅ 绑定成功\n"
        f"{LINE}\n"
        f"账号：{user_name or '未知'}\n"
        f"在读课程：{courses} 门\n"
        f"未完成任务：{tasks} 项\n"
        f"（换 token 走的是 {method} 路）"
    )


def render_token_expired() -> str:
    return (
        "⚠️ 小雅登录已失效\n"
        f"{LINE}\n"
        "自动续期没能救回来，发 /小雅登录 重新扫码绑定。"
    )


def render_expiry_warning(hours_left: float, school_label: str) -> str:
    """refresh token 快到期时的提前提醒。"""
    if hours_left <= 0:
        tail = "已经过期了"
    elif hours_left < 1:
        tail = f"不到 1 小时就过期（还剩 {hours_left * 60:.0f} 分钟）"
    else:
        tail = f"还剩 {hours_left:.0f} 小时"
    return (
        "⏳ 小雅凭证快到期了\n"
        f"{LINE}\n"
        f"{school_label} 的续期凭证{tail}。\n"
        "过期后提醒会断，发 /小雅登录 重新扫码就好。"
    )


def render_refresh_failed(reason: str) -> str:
    """自动续期失败。"""
    return (
        "⚠️ 小雅凭证续期失败\n"
        f"{LINE}\n"
        f"{reason}\n"
        "发 /小雅登录 重新扫码绑定。"
    )
