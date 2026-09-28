"""小雅作业提醒插件核心包。"""

from .client import SCHOOLS, AuthExpired, XiaoyaClient, XiaoyaError
from .formatter import (
    render_batch_new,
    render_digest,
    render_expiry_warning,
    render_login_done,
    render_login_pending,
    render_new_task,
    render_refresh_failed,
    render_status,
    render_token_expired,
    render_urgent,
)
from .models import Task, filter_by_window, group_by_course, sort_tasks
from .qrlogin import QrLoginClient, QrLoginError, render_qr_png
from .refresh import RefreshFailed, TokenInfo, TokenRefresher
from .storage import Storage

__all__ = [
    "SCHOOLS",
    "AuthExpired",
    "QrLoginClient",
    "QrLoginError",
    "RefreshFailed",
    "Storage",
    "Task",
    "TokenInfo",
    "TokenRefresher",
    "XiaoyaClient",
    "XiaoyaError",
    "filter_by_window",
    "group_by_course",
    "render_batch_new",
    "render_digest",
    "render_expiry_warning",
    "render_login_done",
    "render_login_pending",
    "render_new_task",
    "render_qr_png",
    "render_refresh_failed",
    "render_status",
    "render_token_expired",
    "render_urgent",
    "sort_tasks",
]
