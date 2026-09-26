"""main.py 的集成测试。

用桩模块替换 astrbot 依赖，这样不用真的起 AstrBot 就能验证指令流程。

main.py 用的是 ``from .core import ...`` 这种包内相对导入（跟 AstrBot 的加载方式
一致），所以这里必须以「插件目录名」为包名导入，不能直接 ``import main``。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
PKG_NAME = PKG_ROOT.name
sys.path.insert(0, str(PKG_ROOT.parent))
sys.path.insert(0, str(PKG_ROOT))

# ---------------------------------------------------------------- 桩模块

_SENT: list[tuple[str, object]] = []


class _Plain:
    def __init__(self, text: str) -> None:
        self.text = text


class _Image:
    def __init__(self, path: str) -> None:
        self.path = path

    @staticmethod
    def fromFileSystem(path, **_):
        return _Image(path)


class _MessageChain:
    def __init__(self, parts: list) -> None:
        self.parts = parts


class _Result:
    def __init__(self, text: str) -> None:
        self.text = text


class _Event:
    def __init__(self, origin: str = "qq:FriendMessage:123") -> None:
        self.unified_msg_origin = origin

    def plain_result(self, text: str) -> _Result:
        return _Result(text)

    def image_result(self, p: str) -> _Result:  # pragma: no cover - 备用
        return _Result(p)


class _Ctx:
    def __init__(self) -> None:
        self.ok = True

    async def send_message(self, session: str, chain) -> bool:
        _SENT.append((session, chain))
        return self.ok


def _install_stubs() -> None:
    def mod(name: str, **attrs) -> types.ModuleType:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    class _Star:
        def __init__(self, context) -> None:
            self.context = context

    class _Config(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    def _register(*_a, **_k):
        def deco(cls):
            return cls

        return deco

    def _command(_name, **_k):
        def deco(fn):
            return fn

        return deco

    class _FilterNS:
        @staticmethod
        def command(_name, **_k):
            def deco(fn):
                return fn

            return deco

        @staticmethod
        def regex(*_a, **_k):
            return lambda fn: fn

    def _noop_filter(*_a, **_k):
        return lambda fn: fn

    mod("astrbot")
    mod(
        "astrbot.api",
        AstrBotConfig=_Config,
        logger=types.SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            exception=lambda *a, **k: None,
            debug=lambda *a, **k: None,
        ),
    )
    mod("astrbot.api.event", AstrMessageEvent=_Event, MessageChain=_MessageChain)
    mod(
        "astrbot.api.event.filter",
        command=_FilterNS.command,
        regex=_FilterNS.regex,
        permission_type=_noop_filter,
        platform_adapter_type=_noop_filter,
        event_message_type=_noop_filter,
        llm_tool=_noop_filter,
    )
    mod("astrbot.api.star", Context=_Ctx, Star=_Star, register=_register)
    mod("astrbot.core")
    mod("astrbot.core.message")
    mod("astrbot.core.message.components", Image=_Image, Plain=_Plain)
    mod("astrbot.core.utils")

    tmp = Path(tempfile.mkdtemp())
    mod("astrbot.core.utils.astrbot_path", get_astrbot_data_path=lambda: str(tmp))
    mod(
        "astrbot.core.message.message_event_result",
        MessageChain=_MessageChain,
    )


_install_stubs()

plugin_main = __import__(f"{PKG_NAME}.main", fromlist=["main"])
client_mod = __import__(f"{PKG_NAME}.core.client", fromlist=["client"])
qrlogin_mod = __import__(f"{PKG_NAME}.core.qrlogin", fromlist=["qrlogin"])


# ---------------------------------------------------------------- 辅助


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text


def _drain(gen) -> str:
    """跑完一个 async generator，拿最后一条文本。"""
    async def run():
        out = []
        async for item in gen:
            out.append(item.text)
        return "\n".join(out)

    return asyncio.run(run())


def _task(task_id="t1", name="作业", group="高数", hours=5, **kw):
    from datetime import timedelta

    from core.models import now

    base = {
        "task_id": task_id,
        "name": name,
        "group_name": group,
        "group_id": "77",
        "task_type": 2,
        "end_time": now() + timedelta(hours=hours),
    }
    base.update(kw)
    from core.models import Task

    return Task(**base)


def _make_plugin(**cfg):
    from astrbot.api import AstrBotConfig

    conf = AstrBotConfig()
    conf.update(
        {
            "school": "whut",
            "access_token": "",
            "check_interval_minutes": 30,
            "remind_hours": 24,
            "notify_new": True,
            "notify_urgent": True,
            "push_sessions": [],
            "proxy": "",
            "qr_ttl_seconds": 60,
        }
    )
    conf.update(cfg)
    # 每个实例给一份独立的 state.json，否则「已推送」记录会跨用例串味
    plugin_main.DATA_DIR = Path(tempfile.mkdtemp())
    return plugin_main.XiaoyaHomeworkPlugin(_Ctx(), conf)


class _FakeClient:
    """替掉 XiaoyaClient，脚本化各种返回。"""

    tasks: list = []
    who: dict = {"name": "小明"}
    error: Exception | None = None

    def __init__(self, token, school="whut", proxy=None, **_):
        self.token = token
        self.school = school
        self.proxy = proxy

    async def fetch_unfinished(self):
        if type(self).error:
            raise type(self).error
        return list(type(self).tasks)

    async def whoami(self):
        if type(self).error:
            raise type(self).error
        return dict(type(self).who)

    async def fetch_courses(self, flag=1):
        return [{"id": "1"}, {"id": "2"}]

    async def aclose(self):
        return None


def _patch_client(monkey: object = None):
    plugin_main.XiaoyaClient = _FakeClient


# ---------------------------------------------------------------- 测试


def test_help_lists_all_commands():
    p = _make_plugin()
    text = _drain(p.cmd_help(_Event()))
    for cmd in ("/小雅登录", "/小雅绑定", "/小雅解绑", "/小雅作业", "/小雅状态"):
        assert cmd in text
    assert "武汉理工大学" in text


def test_tasks_without_token_tells_to_login():
    p = _make_plugin()
    assert "扫码" in _drain(p.cmd_tasks(_Event()))


def test_bind_without_arg_shows_usage():
    p = _make_plugin()
    text = _drain(p.cmd_bind(_Event(), ""))
    assert "用法" in text
    assert "prd-access-token" in text


def test_bind_stores_token_and_session():
    _patch_client()
    try:
        p = _make_plugin()
        text = _drain(p.cmd_bind(_Event("qq:FriendMessage:999"), "GOOD_TOKEN"))
        assert "绑定成功" in text
        assert p.config["access_token"] == "GOOD_TOKEN"
        assert p.config["bind_session"] == "qq:FriendMessage:999"
        assert p.storage.user_name == "小明"
    finally:
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_bind_rejects_bad_token():
    _FakeClient.error = client_mod.AuthExpired("token 已过期")
    _patch_client()
    try:
        p = _make_plugin()
        text = _drain(p.cmd_bind(_Event(), "BAD"))
        assert "不认" in text
        assert not p.config.get("access_token")
    finally:
        _FakeClient.error = None
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_tasks_renders_digest():
    _FakeClient.tasks = [_task(), _task("t2", "英语作业", "英语", hours=50)]
    _patch_client()
    try:
        p = _make_plugin(access_token="T")
        text = _drain(p.cmd_tasks(_Event()))
        assert "共 2 项" in text
        assert "高数" in text and "英语" in text
    finally:
        _FakeClient.tasks = []
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_tasks_days_filter():
    _FakeClient.tasks = [
        _task("t1", "近的", hours=5),
        _task("t2", "远的", hours=24 * 30),
    ]
    _patch_client()
    try:
        p = _make_plugin(access_token="T")
        text = _drain(p.cmd_tasks(_Event(), "3"))
        assert "近的" in text
        assert "远的" not in text
    finally:
        _FakeClient.tasks = []
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_tasks_rejects_bad_days():
    _patch_client()
    try:
        p = _make_plugin(access_token="T")
        assert "不是天数" in _drain(p.cmd_tasks(_Event(), "abc"))
    finally:
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_status_unbound():
    p = _make_plugin()
    text = _drain(p.cmd_status(_Event()))
    assert "未绑定" in text
    assert "已推送任务：0 条" in text


def test_unbind_clears_everything():
    p = _make_plugin(access_token="T", bind_session="qq:FriendMessage:1")
    p.storage.mark_notified(["a"])
    p.storage.save()
    text = _drain(p.cmd_unbind(_Event()))
    assert "已解绑" in text
    assert p.config["access_token"] == ""
    assert p.storage.data["notified"] == {}


def test_unbind_when_not_bound():
    p = _make_plugin()
    assert "本来就没绑" in _drain(p.cmd_unbind(_Event()))


def test_login_refuses_when_already_bound():
    p = _make_plugin(access_token="T")
    text = _drain(p.cmd_login(_Event()))
    assert "已经绑定过" in text


def test_login_spawns_task_when_unbound():
    p = _make_plugin()
    spawned: list = []

    def fake_spawn(session: str) -> None:
        spawned.append(session)

    p._spawn_login = fake_spawn
    text = _drain(p.cmd_login(_Event("qq:FriendMessage:7")))
    assert "正在生成二维码" in text
    assert spawned == ["qq:FriendMessage:7"]


def test_check_once_pushes_new_then_dedupes():
    # 5 天后截止，避开临期催办，单独验证「新任务推送」这一条路径
    _FakeClient.tasks = [_task(hours=24 * 5)]
    _patch_client()
    try:
        p = _make_plugin(access_token="T", push_sessions=["qq:FriendMessage:1"])
        _SENT.clear()

        first = asyncio.run(p.check_once())
        assert first == 1
        assert len(_SENT) == 1
        # 单条任务走 render_new_task，多条才合并成「新增 N 项」
        assert "小雅有新任务了" in _SENT[0][1].parts[0].text

        _SENT.clear()
        second = asyncio.run(p.check_once())
        assert second == 0
        assert _SENT == []
    finally:
        _FakeClient.tasks = []
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_check_once_urgent_only_once():
    _FakeClient.tasks = [_task(hours=3)]
    _patch_client()
    try:
        p = _make_plugin(
            access_token="T",
            push_sessions=["qq:FriendMessage:1"],
            remind_hours=24,
        )
        # 第一次：既算新任务也进催办阈值
        _SENT.clear()
        asyncio.run(p.check_once())
        assert len(_SENT) == 2

        # 第二次：都推过了，不该再推
        _SENT.clear()
        asyncio.run(p.check_once())
        assert _SENT == []
    finally:
        _FakeClient.tasks = []
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_check_once_notify_urgent_disabled():
    _FakeClient.tasks = [_task(hours=3)]
    _patch_client()
    try:
        p = _make_plugin(
            access_token="T",
            push_sessions=["qq:FriendMessage:1"],
            notify_urgent=False,
        )
        _SENT.clear()
        asyncio.run(p.check_once())
        assert len(_SENT) == 1  # 只有新任务那条
    finally:
        _FakeClient.tasks = []
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_check_once_expired_token_alerts_once():
    _FakeClient.error = client_mod.AuthExpired("token 已过期")
    _patch_client()
    try:
        p = _make_plugin(access_token="T", push_sessions=["qq:FriendMessage:1"])
        _SENT.clear()
        asyncio.run(p.check_once())
        assert len(_SENT) == 1
        assert "失效" in _SENT[0][1].parts[0].text

        _SENT.clear()
        asyncio.run(p.check_once())
        assert _SENT == []  # 只告警一次
    finally:
        _FakeClient.error = None
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_check_once_no_token_is_noop():
    p = _make_plugin()
    _SENT.clear()
    assert asyncio.run(p.check_once()) == 0
    assert _SENT == []


def test_sessions_fallback_to_bind_session():
    p = _make_plugin(bind_session="qq:GroupMessage:42")
    assert p._sessions() == ["qq:GroupMessage:42"]

    p2 = _make_plugin(bind_session="qq:GroupMessage:42", push_sessions=["a", "b"])
    assert p2._sessions() == ["a", "b"]


def test_interval_and_hours_parsing():
    p = _make_plugin(check_interval_minutes=0, remind_hours="坏数据")
    assert p._interval_seconds() == 60.0
    assert p._remind_hours() == 24.0

    p2 = _make_plugin(check_interval_minutes=15, remind_hours=6)
    assert p2._interval_seconds() == 900.0
    assert p2._remind_hours() == 6.0


def test_unknown_school_falls_back():
    p = _make_plugin(school="不存在的学校")
    assert p._school() == "whut"
    assert "武汉理工大学" in p._school_label()


def test_qr_login_full_flow_success():
    """扫码登录主流程：出码 -> 轮询 -> 换 token -> 回执。"""

    class _FakeQr:
        def __init__(self, school="whut", proxy=None, **_):
            self.school = school
            self.proxy = proxy
            self.closed = False

        async def create_session(self):
            return qrlogin_mod.QrSession(
                qr_url="https://x/scan?key=abc&qrtype=LOGIN_BY_SCAN",
                key="abc",
                state="st1234",
            )

        async def wait_for_confirm(self, session, timeout=60.0, on_status=None):
            assert session.key == "abc"

        async def fetch_token(self, state):
            assert state == "st1234"
            return qrlogin_mod.QrLoginResult(token="FRESH_TOKEN", method="A")

        async def aclose(self):
            self.closed = True

    plugin_main.QrLoginClient = _FakeQr
    _patch_client()
    _SENT.clear()
    try:
        p = _make_plugin()
        asyncio.run(p._run_qr_login("qq:FriendMessage:5"))
        assert p.config["access_token"] == "FRESH_TOKEN"
        assert p.config["bind_session"] == "qq:FriendMessage:5"
        texts = [c.parts[-1].text for _, c in _SENT]
        assert any("小雅登录" in t for t in texts)  # 等扫码的提示
        assert any("绑定成功" in t for t in texts)
    finally:
        plugin_main.QrLoginClient = qrlogin_mod.__dict__["QrLoginClient"]
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_qr_login_reports_denial():
    class _FakeQr:
        def __init__(self, school="whut", proxy=None, **_):
            pass

        async def create_session(self):
            return qrlogin_mod.QrSession(
                qr_url="https://x/scan?key=abc", key="abc", state="s"
            )

        async def wait_for_confirm(self, session, timeout=60.0, on_status=None):
            raise qrlogin_mod.QrLoginDenied("你在手机上拒绝了本次登录")

        async def fetch_token(self, state):  # pragma: no cover
            raise AssertionError("不该走到这")

        async def aclose(self):
            return None

    plugin_main.QrLoginClient = _FakeQr
    _SENT.clear()
    try:
        p = _make_plugin()
        asyncio.run(p._run_qr_login("qq:FriendMessage:5"))
        assert not p.config.get("access_token")
        texts = [c.parts[-1].text for _, c in _SENT]
        assert any("拒绝" in t for t in texts)
    finally:
        plugin_main.QrLoginClient = qrlogin_mod.__dict__["QrLoginClient"]
