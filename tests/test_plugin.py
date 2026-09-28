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
            "push_enabled": True,
            "push_sessions": "",
            "proxy": "",
            "qr_ttl_seconds": 60,
        }
    )
    conf.update(cfg)
    # 每个实例给一份独立的 state.json，否则「已推送」记录会跨用例串味
    plugin_main.DATA_DIR = Path(tempfile.mkdtemp())
    # bind_session 现在存在 state.json 里，不再是 AstrBot 配置项
    bind = cfg.pop("bind_session", "")
    plugin = plugin_main.XiaoyaHomeworkPlugin(_Ctx(), conf)
    if bind:
        plugin.storage.set_bind_session(bind)
    return plugin


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
        assert p.storage.bind_session == "qq:FriendMessage:999"
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
        # 没有 refresh token 时要说清是「没法自动续期」，不是笼统的失效
        assert "没有 refresh token" in _SENT[0][1].parts[0].text

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
        assert p.storage.bind_session == "qq:FriendMessage:5"
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


# ---------------------------------------------------------------- 推送开关


def test_sessions_prefers_explicit_target():
    p = _make_plugin(push_sessions="qq:Group:999", bind_session="qq:FriendMessage:1")
    assert p._sessions() == ["qq:Group:999"]


def test_sessions_falls_back_to_bind_session():
    p = _make_plugin(push_sessions="", bind_session="qq:FriendMessage:1")
    assert p._sessions() == ["qq:FriendMessage:1"]


def test_sessions_accepts_legacy_list():
    """老配置里 push_sessions 是 list，schema 改 string 后可能还留着。"""
    p = _make_plugin(push_sessions=["qq:Group:1", " qq:Group:2 "], bind_session="x")
    assert p._sessions() == ["qq:Group:1", "qq:Group:2"]


def test_sessions_empty_when_nothing_bound():
    assert _make_plugin(push_sessions="", bind_session="")._sessions() == []


def test_push_enabled_defaults_true():
    p = _make_plugin()
    del p.config["push_enabled"]
    assert p._push_enabled() is True


def test_push_disabled_does_not_mark_notified():
    """关着的时候不能把任务标记成已推送，否则重新打开就什么都收不到。"""
    _patch_client()
    _FakeClient.tasks = [_task()]
    try:
        _SENT.clear()
        p = _make_plugin(
            push_enabled=False, access_token="tok", bind_session="qq:FriendMessage:1"
        )
        assert asyncio.run(p.check_once()) == 0
        assert not p.storage.has_notified("t1")
        assert not _SENT
    finally:
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_push_enabled_marks_and_sends():
    _patch_client()
    _FakeClient.tasks = [_task()]
    try:
        _SENT.clear()
        p = _make_plugin(
            push_enabled=True,
            access_token="tok",
            bind_session="qq:FriendMessage:1",
            remind_hours=0,  # 关掉催办，只走「新任务」这一条
        )
        assert asyncio.run(p.check_once()) == 1
        assert p.storage.has_notified("t1")
        assert _SENT
    finally:
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_push_cmd_reports_status():
    p = _make_plugin(bind_session="qq:FriendMessage:7")
    text = _drain(p.cmd_push(_Event()))
    assert "开" in text
    assert "qq:FriendMessage:7" in text


def test_push_cmd_toggles_off_and_on():
    p = _make_plugin(push_enabled=True, bind_session="qq:FriendMessage:7")
    _SENT.clear()
    text = _drain(p.cmd_push(_Event(), "关"))
    assert p.config["push_enabled"] is False
    assert "关" in text

    text = _drain(p.cmd_push(_Event(), "开"))
    assert p.config["push_enabled"] is True
    assert "开" in text


def test_push_cmd_on_resets_dedupe():
    """重新打开推送要把闸门放掉，不然当下的任务算「已推送」。"""
    p = _make_plugin(push_enabled=False, bind_session="qq:FriendMessage:7")
    p.storage.mark_notified(["t1"])
    p.storage.save()
    _drain(p.cmd_push(_Event(), "开"))
    assert not p.storage.has_notified("t1")


def test_push_cmd_rejects_garbage():
    p = _make_plugin()
    text = _drain(p.cmd_push(_Event(), "maybe"))
    assert "看不懂" in text
    assert p.config["push_enabled"] is True


def test_push_cmd_without_target_says_so():
    text = _drain(_make_plugin(push_sessions="", bind_session="").cmd_push(_Event()))
    assert "还没绑定" in text


def test_save_config_called_when_available():
    """AstrBotConfig.save_config 存在时必须调，不然重启就丢。"""
    calls: list[int] = []

    p = _make_plugin()
    p.config.save_config = lambda *a, **k: calls.append(1)
    p._save_config()
    assert calls == [1]


def test_save_config_swallowed_when_missing():
    """桩配置没有 save_config，不能因此炸掉。"""
    p = _make_plugin()
    assert not hasattr(p.config, "save_config")
    p._save_config()  # 不抛异常就算过


def test_status_shows_push_state():
    p = _make_plugin(push_enabled=False, bind_session="qq:FriendMessage:7")
    text = _drain(p.cmd_status(_Event()))
    assert "定时推送：关" in text
    assert "qq:FriendMessage:7" in text


# ------------------------------------------- 绑定会话不能放 AstrBot 配置里


def test_bind_session_lives_in_storage_not_config():
    """bind_session 不在 _conf_schema.json 里，放 config 会被完整性校验删掉。"""
    p = _make_plugin()
    assert "bind_session" not in p.config
    p.storage.set_bind_session("qq:Group:555")
    p.storage.save()
    assert p.storage.bind_session == "qq:Group:555"
    assert "bind_session" not in p.config


def test_schema_declares_every_config_key_the_plugin_writes():
    """插件往 config 里写的键，必须都出现在 _conf_schema.json 里。"""
    import json

    schema = json.loads(
        (PKG_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
    )
    written = {
        "access_token",
        "refresh_token",
        "push_enabled",
        "push_sessions",
        "school",
        "proxy",
    }
    missing = written - set(schema)
    assert not missing, f"这些键没在 schema 里声明，会被 check_config_integrity 删掉：{missing}"


def test_storage_reset_keeps_bind_session():
    p = _make_plugin()
    p.storage.set_bind_session("qq:Group:555")
    p.storage.mark_notified(["t1"])
    p.storage.reset()
    assert p.storage.bind_session == "qq:Group:555"
    assert not p.storage.has_notified("t1")


def test_storage_persists_bind_session_across_reload():
    p = _make_plugin()
    p.storage.set_bind_session("qq:Group:555")
    p.storage.save()
    p.storage.load()
    assert p.storage.bind_session == "qq:Group:555"


def test_initialize_migrates_legacy_bind_session():
    p = _make_plugin()
    p.config["bind_session"] = "qq:Legacy:999"
    asyncio.run(p.initialize())
    assert p.storage.bind_session == "qq:Legacy:999"
    assert p._sessions() == ["qq:Legacy:999"]


def test_initialize_does_not_clobber_existing_bind_session():
    p = _make_plugin(bind_session="qq:Current:1")
    p.config["bind_session"] = "qq:Legacy:999"
    asyncio.run(p.initialize())
    assert p.storage.bind_session == "qq:Current:1"


def test_migration_reads_legacy_from_config_file():
    """AstrBotConfig 构造时就清了 bind_session，只能去原始文件里捞。"""
    import json

    p = _make_plugin()
    cfg_file = Path(tempfile.mkdtemp()) / "plugin_config.json"
    cfg_file.write_text(
        json.dumps({"school": "whut", "bind_session": "qq:Legacy:999"}),
        encoding="utf-8-sig",
    )
    p.config.config_path = str(cfg_file)
    assert "bind_session" not in p.config  # 模拟已被剥掉
    p._migrate_bind_session()
    assert p.storage.bind_session == "qq:Legacy:999"


def test_migration_tolerates_missing_config_file():
    p = _make_plugin()
    p.config.config_path = "/nonexistent/nope.json"
    p._migrate_bind_session()  # 不抛异常
    assert p.storage.bind_session == ""


def test_migration_tolerates_corrupt_config_file():
    p = _make_plugin()
    cfg_file = Path(tempfile.mkdtemp()) / "bad.json"
    cfg_file.write_text("{ 这不是 json", encoding="utf-8")
    p.config.config_path = str(cfg_file)
    p._migrate_bind_session()
    assert p.storage.bind_session == ""


# ------------------------------------------------- qr_debug 默认关闭


def _failing_qr_login(qr_debug):
    """造一个换 token 失败的扫码流程，跑完把推送内容还回来。"""

    class _FakeQr:
        def __init__(self, school="whut", proxy=None, **_):
            self.diag = ["[listAccounts] HTTP 200 | body: {}"]

        async def create_session(self):
            return qrlogin_mod.QrSession(
                qr_url="https://x/scan?key=abc", key="abc", state="s"
            )

        async def wait_for_confirm(self, session, timeout=60.0, on_status=None):
            return None

        async def fetch_token(self, state):
            raise qrlogin_mod.QrLoginError("扫码确认了，但换 token 失败：建会话：A 路 403")

        def diag_text(self, limit=10):
            return "── 诊断 ──\n[self.diag] HTTP 403"

        async def aclose(self):
            return None

    plugin_main.QrLoginClient = _FakeQr
    _SENT.clear()
    try:
        p = _make_plugin(qr_debug=qr_debug)
        asyncio.run(p._run_qr_login("qq:FriendMessage:5"))
        return "\n".join(c.parts[-1].text for _, c in _SENT)
    finally:
        plugin_main.QrLoginClient = qrlogin_mod.__dict__["QrLoginClient"]


def test_qr_debug_off_by_default():
    """默认不该往 QQ 推诊断，登录失败只报一句人话。"""
    text = _failing_qr_login(qr_debug=False)
    assert "换 token 失败" in text
    assert "诊断" not in text
    assert "HTTP 403" not in text


def test_qr_debug_on_includes_diagnostics():
    text = _failing_qr_login(qr_debug=True)
    assert "换 token 失败" in text
    assert "诊断" in text
    assert "HTTP 403" in text


def test_qr_debug_default_is_false_in_schema():
    """schema 默认值和代码里的兜底默认值必须一致。"""
    import json

    schema = json.loads(
        (PKG_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
    )
    assert schema["qr_debug"]["default"] is False
    assert _make_plugin(qr_debug=False)._flag("qr_debug", False) is False


# ------------------------------------------------- 自动续期与到期提醒


def _fake_refresher(*, ok=True, hours=168.0, refresh_hours=169.0):
    """装一个假的 TokenRefresher，记录调用次数。

    异常类必须从 main.py 用的那份模块里取——测试里 ``core`` 和
    ``astrbot_plugin_xiaoya_homework.core`` 是两个独立模块对象，
    同一个类名在这儿是两个不同的类，except 接不住会掉进兜底分支。
    """
    from datetime import datetime, timedelta

    pkg_refresh = __import__(f"{PKG_NAME}.core.refresh", fromlist=["refresh"])
    CST = pkg_refresh.CST
    RefreshFailed = pkg_refresh.RefreshFailed
    TokenInfo = pkg_refresh.TokenInfo

    state = {"calls": 0}

    class _Fake:
        def __init__(self, school="whut", proxy=None, **_):
            state["school"] = school

        async def refresh(self, rt):
            state["calls"] += 1
            state["refresh_token"] = rt
            if not ok:
                raise RefreshFailed("refresh token 已失效，需要重新扫码")
            now = datetime.now(CST)
            return TokenInfo(
                access_token="NEW_ACCESS",
                refresh_token="NEW_REFRESH",
                access_expires_at=now + timedelta(hours=hours),
                refresh_expires_at=now + timedelta(hours=refresh_hours),
            )

        async def aclose(self):
            return None

    plugin_main.TokenRefresher = _Fake
    return state


def _restore_refresher():
    pkg_refresh = __import__(f"{PKG_NAME}.core.refresh", fromlist=["refresh"])
    plugin_main.TokenRefresher = pkg_refresh.TokenRefresher


def test_try_refresh_writes_tokens_and_expiry():
    calls = _fake_refresher()
    _SENT.clear()
    try:
        p = _make_plugin(access_token="OLD", refresh_token="RT_OLD")
        assert asyncio.run(p.try_refresh()) is True
        assert p.config["access_token"] == "NEW_ACCESS"
        assert p.config["refresh_token"] == "NEW_REFRESH"
        assert p.storage.access_expires_at
        assert p.storage.refresh_expires_at
        assert calls["refresh_token"] == "RT_OLD"
    finally:
        _restore_refresher()


def test_try_refresh_failure_pushes_reason():
    _fake_refresher(ok=False)
    _SENT.clear()
    try:
        p = _make_plugin(
            access_token="OLD", refresh_token="DEAD", push_sessions="qq:FriendMessage:1"
        )
        assert asyncio.run(p.try_refresh()) is False
        text = "\n".join(c.parts[-1].text for _, c in _SENT)
        assert "续期失败" in text
        assert "重新扫码" in text
    finally:
        _restore_refresher()


def test_try_refresh_noop_without_refresh_token():
    calls = _fake_refresher()
    try:
        p = _make_plugin(access_token="OLD", refresh_token="")
        assert asyncio.run(p.try_refresh()) is False
        assert calls["calls"] == 0
    finally:
        _restore_refresher()


def test_try_refresh_respects_auto_refresh_off():
    calls = _fake_refresher()
    try:
        p = _make_plugin(access_token="OLD", refresh_token="RT", auto_refresh=False)
        assert asyncio.run(p.try_refresh()) is False
        assert calls["calls"] == 0
    finally:
        _restore_refresher()


def test_ensure_fresh_refreshes_when_near_expiry():
    from datetime import datetime, timedelta

    from core.refresh import CST

    calls = _fake_refresher()
    try:
        p = _make_plugin(access_token="OLD", refresh_token="RT")
        # 只剩 1 小时，低于默认 12 小时阈值 → 该续
        p.storage.set_token_expiry(
            (datetime.now(CST) + timedelta(hours=1)).isoformat(), ""
        )
        assert asyncio.run(p._ensure_fresh_token()) is True
        assert calls["calls"] == 1
    finally:
        _restore_refresher()


def test_ensure_fresh_skips_when_plenty_of_time():
    from datetime import datetime, timedelta

    from core.refresh import CST

    calls = _fake_refresher()
    try:
        p = _make_plugin(access_token="OLD", refresh_token="RT")
        p.storage.set_token_expiry(
            (datetime.now(CST) + timedelta(hours=100)).isoformat(), ""
        )
        assert asyncio.run(p._ensure_fresh_token()) is True
        assert calls["calls"] == 0
    finally:
        _restore_refresher()


def test_ensure_fresh_skips_when_expiry_unknown():
    """旧版本升级上来没有有效期记录，不该一上来就狂刷续期接口。"""
    calls = _fake_refresher()
    try:
        p = _make_plugin(access_token="OLD", refresh_token="RT")
        p.storage.clear_token_expiry()
        assert asyncio.run(p._ensure_fresh_token()) is True
        assert calls["calls"] == 0
    finally:
        _restore_refresher()


def test_expiry_warning_pushed_once():
    from datetime import datetime, timedelta

    from core.refresh import CST

    _SENT.clear()
    try:
        p = _make_plugin(push_sessions="qq:FriendMessage:1", expiry_warn_hours=48)
        p.storage.set_token_expiry(
            "",
            (datetime.now(CST) + timedelta(hours=5)).isoformat(),
        )
        asyncio.run(p._warn_expiry_risk())
        assert len(_SENT) == 1
        assert "快到期" in _SENT[0][1].parts[0].text
        asyncio.run(p._warn_expiry_risk())
        assert len(_SENT) == 1, "同一次临期只该提醒一次"
    finally:
        pass


def test_expiry_warning_quiet_when_far_out():
    from datetime import datetime, timedelta

    from core.refresh import CST

    _SENT.clear()
    p = _make_plugin(push_sessions="qq:FriendMessage:1")
    p.storage.set_token_expiry(
        "", (datetime.now(CST) + timedelta(hours=200)).isoformat()
    )
    asyncio.run(p._warn_expiry_risk())
    assert _SENT == []


def test_expiry_warning_respects_zero_threshold():
    from datetime import datetime, timedelta

    from core.refresh import CST

    _SENT.clear()
    p = _make_plugin(push_sessions="qq:FriendMessage:1", expiry_warn_hours=0)
    p.storage.set_token_expiry(
        "", (datetime.now(CST) + timedelta(hours=1)).isoformat()
    )
    asyncio.run(p._warn_expiry_risk())
    assert _SENT == []


def test_status_shows_token_lifetimes():
    from datetime import datetime, timedelta

    from core.refresh import CST

    p = _make_plugin(access_token="T", refresh_token="RT", auto_refresh=True)
    p.storage.set_token_expiry(
        (datetime.now(CST) + timedelta(hours=3)).isoformat(),
        (datetime.now(CST) + timedelta(days=6)).isoformat(),
    )
    text = _drain(p.cmd_status(_Event()))
    assert "自动续期：开" in text
    assert "3 小时" in text
    assert "6.0 天" in text


def test_status_warns_when_no_refresh_token():
    p = _make_plugin(access_token="T", refresh_token="")
    text = _drain(p.cmd_status(_Event()))
    assert "没有续期凭证" in text


def test_unbind_clears_refresh_token():
    p = _make_plugin(access_token="T", refresh_token="RT")
    p.storage.set_token_expiry("2026-10-01T00:00:00+08:00", "2026-10-05T00:00:00+08:00")
    _drain(p.cmd_unbind(_Event()))
    assert p.config["access_token"] == ""
    assert p.config["refresh_token"] == ""
    assert p.storage.access_expires_at == ""
    assert p.storage.refresh_expires_at == ""


# --------------------------------------- 凭证过期后还能不能重新登录


def test_login_allowed_when_token_present():
    """凭证过期是常态，不能把人挡在门外。"""
    _FakeClient.error = None
    _patch_client()
    try:
        p = _make_plugin(access_token="OLD")
        spawned: list[str] = []
        p._spawn_login = lambda session: spawned.append(session)  # type: ignore[method-assign]
        text = _drain(p.cmd_login(_Event()))
        assert "二维码" in text
        assert spawned == ["qq:FriendMessage:123"], "有旧凭证时也该照常发起扫码"
    finally:
        _FakeClient.error = None
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_login_hint_says_token_dead():
    _FakeClient.error = client_mod.AuthExpired("token 已过期")
    _patch_client()
    try:
        p = _make_plugin(access_token="DEAD")
        text = _drain(p.cmd_login(_Event()))
        assert "失效" in text
        assert "二维码" in text
    finally:
        _FakeClient.error = None
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_login_hint_says_token_still_good():
    _FakeClient.error = None
    _patch_client()
    try:
        p = _make_plugin(access_token="GOOD")
        text = _drain(p.cmd_login(_Event()))
        assert "仍然有效" in text
        assert "覆盖" in text
    finally:
        _FakeClient.error = None
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_login_hint_silent_when_unbound():
    _FakeClient.error = None
    _patch_client()
    try:
        p = _make_plugin(access_token="")
        text = _drain(p.cmd_login(_Event()))
        assert text.strip() == "正在生成二维码，马上发给你……"
    finally:
        _FakeClient.error = None
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_login_hint_hides_misleading_state_on_network_error():
    """网络抽风时说不出好坏，就别乱下结论。"""
    _FakeClient.error = client_mod.XiaoyaError("网络请求失败")
    _patch_client()
    try:
        p = _make_plugin(access_token="T")
        text = _drain(p.cmd_login(_Event()))
        assert "失效" not in text
        assert "二维码" in text
    finally:
        _FakeClient.error = None
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_failed_rescan_keeps_old_credentials():
    """扫码失败绝不能把还能用的凭证毁掉——覆盖只发生在换到新 token 之后。"""
    class _FakeQr:
        def __init__(self, school="whut", proxy=None, **_):
            pass

        async def create_session(self):
            return qrlogin_mod.QrSession(
                qr_url="https://x/scan?key=abc", key="abc", state="s"
            )

        async def wait_for_confirm(self, session, timeout=60.0, on_status=None):
            return None

        async def fetch_token(self, state):
            raise qrlogin_mod.QrLoginDenied("二维码已过期")

        async def aclose(self):
            return None

    plugin_main.QrLoginClient = _FakeQr
    try:
        p = _make_plugin(access_token="STILL_GOOD", refresh_token="RT_GOOD")
        p.storage.mark_notified(["t1"])
        p.storage.save()
        asyncio.run(p._run_qr_login("qq:FriendMessage:5"))
        assert p.config["access_token"] == "STILL_GOOD"
        assert p.config["refresh_token"] == "RT_GOOD"
        assert p.storage.has_notified("t1")
    finally:
        plugin_main.QrLoginClient = qrlogin_mod.__dict__["QrLoginClient"]


def test_rescan_replaces_credentials_and_clears_dedupe():
    class _FakeQr:
        def __init__(self, school="whut", proxy=None, **_):
            pass

        async def create_session(self):
            return qrlogin_mod.QrSession(
                qr_url="https://x/scan?key=abc", key="abc", state="s"
            )

        async def wait_for_confirm(self, session, timeout=60.0, on_status=None):
            return None

        async def fetch_token(self, state):
            return qrlogin_mod.QrLoginResult(
                token="NEW_TOK", method="A", refresh_token="NEW_RT"
            )

        async def aclose(self):
            return None

    plugin_main.QrLoginClient = _FakeQr
    _patch_client()
    try:
        p = _make_plugin(access_token="OLD", refresh_token="OLD_RT")
        p.storage.mark_notified(["t1"])
        p.storage.save()
        asyncio.run(p._run_qr_login("qq:FriendMessage:9"))
        assert p.config["access_token"] == "NEW_TOK"
        assert p.config["refresh_token"] == "NEW_RT"
        # 去重记录要清掉，不然新凭证下当天的任务不会推
        assert not p.storage.has_notified("t1")
        assert p.storage.bind_session == "qq:FriendMessage:9"
    finally:
        plugin_main.QrLoginClient = qrlogin_mod.__dict__["QrLoginClient"]
        plugin_main.XiaoyaClient = client_mod.__dict__["XiaoyaClient"]


def test_login_does_not_spawn_twice():
    """连着刷会同时跑好几个流程，二维码一堆，挡一下。"""
    p = _make_plugin()
    p._login_tasks.add(object())
    spawned: list[str] = []
    p._spawn_login = lambda session: spawned.append(session)  # type: ignore[method-assign]
    text = _drain(p.cmd_login(_Event()))
    assert spawned == []
    assert "已经有一个二维码" in text
