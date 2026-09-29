"""小雅作业提醒 · AstrBot 插件。

只读地盯住小雅（理工智课）平台上未完成的课程任务，有新任务或者快截止时推给你。
不做任何自动提交、刷时长的操作。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Image, Plain
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core import (
    SCHOOLS,
    AuthExpired,
    QrLoginClient,
    QrLoginError,
    RefreshFailed,
    Storage,
    TokenRefresher,
    TransientError,
    XiaoyaClient,
    XiaoyaError,
    filter_by_window,
    render_batch_new,
    render_digest,
    render_expiry_warning,
    render_login_done,
    render_login_pending,
    render_qr_png,
    render_refresh_failed,
    render_status,
    render_token_expired,
    render_urgent,
)
from .core.client import DEFAULT_SCHOOL
from .core.qrlogin import STATUS_TEXT  # noqa: F401  供指令层复用状态文案
from .core.refresh import CST

PLUGIN_NAME = "astrbot_plugin_xiaoya_homework"
DATA_DIR = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME

# 平台返回非 JSON 时重试前的等待。太长会拖慢轮询，太短可能赶上对方故障。
RETRY_DELAY_SECONDS = 2.0


@register(
    PLUGIN_NAME,
    "Pianzh",
    "小雅（理工智课）作业提醒：扫码登录，定时抓未完成任务推给你。只读，不自动提交。",
    "0.1.0",
    "https://github.com/Pianzh/astrbot_plugin_xiaoya_homework",
)


class XiaoyaHomeworkPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.storage = Storage(DATA_DIR / "state.json")
        self.storage.load()

        self._task: asyncio.Task | None = None
        self._login_tasks: set[asyncio.Task] = set()
        self._alerted_expiry = False
        self._alerted_refresh_risk = False
        self._alerted_refresh_failed = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._migrate_bind_session()
        if not self._token():
            logger.info("[小雅作业] 尚未绑定凭证，发 /小雅登录 扫码绑定。")
            return
        self._task = asyncio.create_task(self._loop(), name="xiaoya-homework-loop")
        logger.info("[小雅作业] 后台轮询已启动。")

    def _migrate_bind_session(self) -> None:
        """把旧版本留在 AstrBot 配置里的 bind_session 搬进 state.json。

        之前它写在 ``self.config`` 里，但不在 ``_conf_schema.json`` 中，
        ``check_config_integrity`` 会把它当废弃键删掉。

        麻烦的是删得比插件读得早：``AstrBotConfig`` 构造时就清了，
        ``self.config`` 里根本看不到那个值。所以直接去读原始配置文件。
        """
        if self.storage.bind_session:
            return
        legacy = str(self.config.get("bind_session", "") or "").strip()
        if not legacy:
            legacy = self._legacy_bind_from_file()
        if not legacy:
            return
        self.storage.set_bind_session(legacy)
        self.storage.save()
        logger.info("[小雅作业] 已从旧配置迁移推送目标。")

    def _legacy_bind_from_file(self) -> str:
        """从插件自己的配置文件原始内容里翻出 bind_session。"""
        path = getattr(self.config, "config_path", "")
        if not path:
            return ""
        try:
            with open(path, encoding="utf-8-sig") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return ""
        if not isinstance(raw, dict):
            return ""
        return str(raw.get("bind_session") or "").strip()

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for task in list(self._login_tasks):
            task.cancel()
        if self._login_tasks:
            await asyncio.gather(*self._login_tasks, return_exceptions=True)
            self._login_tasks.clear()
        with contextlib.suppress(Exception):
            self.storage.save()
        logger.info("[小雅作业] 已停止。")

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #

    def _token(self) -> str:
        return str(self.config.get("access_token", "") or "").strip()

    def _refresh_token(self) -> str:
        return str(self.config.get("refresh_token", "") or "").strip()

    def _auto_refresh(self) -> bool:
        return self._flag("auto_refresh", True)

    def _refresh_margin_hours(self) -> float:
        try:
            return max(0.0, float(self.config.get("refresh_margin_hours", 12)))
        except (TypeError, ValueError):
            return 12.0

    def _expiry_warn_hours(self) -> float:
        try:
            return max(0.0, float(self.config.get("expiry_warn_hours", 48)))
        except (TypeError, ValueError):
            return 48.0

    def _hours_left(self, raw: str) -> float | None:
        """把存的 ISO 串换算成还剩几小时。解析不了返回 None。"""
        if not raw:
            return None
        try:
            at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (at - datetime.now(CST)).total_seconds() / 3600.0

    async def try_refresh(self) -> bool:
        """用 refresh token 换新凭证，成功就写回配置和有效期。"""
        if not self._auto_refresh():
            return False
        refresh = self._refresh_token()
        if not refresh:
            return False
        refresher = TokenRefresher(school=self._school(), proxy=self._proxy())
        try:
            info = await refresher.refresh(refresh)
        except (RefreshFailed, XiaoyaError) as exc:
            logger.warning("[小雅作业] 自动续期失败：%s", exc)
            self._alerted_refresh_failed = True
            await self._push_text(render_refresh_failed(str(exc)))
            return False
        except Exception as exc:
            logger.warning("[小雅作业] 自动续期时出现意外错误：%r", exc)
            return False
        finally:
            with contextlib.suppress(Exception):
                await refresher.aclose()

        self.config["access_token"] = info.access_token
        self.config["refresh_token"] = info.refresh_token
        self._save_config()
        self.storage.set_token_expiry(
            info.access_expires_at.isoformat() if info.access_expires_at else "",
            info.refresh_expires_at.isoformat() if info.refresh_expires_at else "",
        )
        self.storage.save()
        self._alerted_refresh_failed = False
        self._alerted_refresh_risk = False
        self._alerted_expiry = False
        logger.info(
            "[小雅作业] 凭证已自动续期，access 还剩 %.1f 小时",
            info.hours_left("access"),
        )
        return True

    async def _ensure_fresh_token(self) -> bool:
        """快到期就提前续。返回是否还能继续用。"""
        if not self._auto_refresh():
            return bool(self._token())
        left = self._hours_left(self.storage.access_expires_at)
        # 没有有效期记录（旧版本升级上来的）就先探一次，失败也不致命
        if left is None:
            return bool(self._token())
        if left > self._refresh_margin_hours():
            return True
        if await self.try_refresh():
            return True
        return bool(self._token())

    async def _warn_expiry_risk(self) -> None:
        """refresh token 快到期时提前提醒——那才是必须重扫的死线。"""
        threshold = self._expiry_warn_hours()
        if threshold <= 0 or self._alerted_refresh_risk:
            return
        left = self._hours_left(self.storage.refresh_expires_at)
        if left is None or left > threshold:
            return
        self._alerted_refresh_risk = True
        await self._push_text(render_expiry_warning(left, self._school_label()))

    def _school(self) -> str:
        key = str(self.config.get("school", DEFAULT_SCHOOL) or DEFAULT_SCHOOL)
        return key if key in SCHOOLS else DEFAULT_SCHOOL

    def _school_label(self) -> str:
        return SCHOOLS[self._school()]["label"]

    def _host(self) -> str:
        return SCHOOLS[self._school()]["host"]

    def _proxy(self) -> str | None:
        proxy = str(self.config.get("proxy", "") or "").strip()
        return proxy or None

    def _interval_seconds(self) -> float:
        try:
            minutes = float(self.config.get("check_interval_minutes", 30))
        except (TypeError, ValueError):
            minutes = 30.0
        return max(1.0, minutes) * 60.0

    def _remind_hours(self) -> float:
        try:
            hours = float(self.config.get("remind_hours", 24))
        except (TypeError, ValueError):
            hours = 24.0
        return max(0.0, hours)

    def _flag(self, name: str, default: bool = True) -> bool:
        value = self.config.get(name, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _sessions(self) -> list[str]:
        """推送目标。配置里显式指定的优先，否则用绑定时的会话。

        ``push_sessions`` 早期是 list 类型的配置项，schema 改成 string 之后
        老配置文件里可能还留着 list，所以两种形态都认。
        """
        raw = self.config.get("push_sessions", "")
        candidates = raw if isinstance(raw, (list, tuple)) else [raw]
        sessions: list[str] = []
        for item in candidates:
            text = str(item or "").strip()
            if text and text not in sessions:
                sessions.append(text)
        if sessions:
            return sessions
        fallback = self.storage.bind_session
        return [fallback] if fallback else []

    def _push_enabled(self) -> bool:
        return self._flag("push_enabled", True)

    def _save_config(self) -> None:
        """把配置写回磁盘。

        ``AstrBotConfig`` 继承 dict，改键只是改内存，不落盘的话
        重启或者面板重载之后 token 和开关全丢。
        """
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            try:
                saver()
            except Exception as exc:
                logger.warning("[小雅作业] 配置落盘失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 推送
    # ------------------------------------------------------------------ #

    async def _send(self, session: str, chain: Any) -> None:
        """发一条消息，失败只记日志，不能把轮询循环带崩。"""
        try:
            await self.context.send_message(session, chain)
        except Exception as exc:
            logger.warning("[小雅作业] 推送到 %s 失败：%s", session, exc)

    async def _push_text(self, text: str) -> None:
        if not text.strip():
            return
        for session in self._sessions():
            await self._send(session, MessageChain([Plain(text)]))

    async def _push_qr(self, png: Path, caption: str) -> None:
        chain = MessageChain([Image.fromFileSystem(str(png)), Plain(caption)])
        for session in self._sessions():
            await self._send(session, chain)

    async def _reply_text(self, session: str, text: str) -> None:
        """直接回给触发这次操作的会话。

        扫码登录时绑定会话还没记上，走 ``_sessions()`` 会推空，
        所以登录相关的消息一律用显式传进来的 session。
        """
        if text.strip():
            await self._send(session, MessageChain([Plain(text)]))

    async def _reply_qr(self, session: str, png: Path, caption: str) -> None:
        await self._send(
            session,
            MessageChain([Image.fromFileSystem(str(png)), Plain(caption)]),
        )

    # ------------------------------------------------------------------ #
    # 核心抓取
    # ------------------------------------------------------------------ #

    async def _collect(self) -> tuple[list[Any], XiaoyaClient]:
        client = XiaoyaClient(
            self._token(), school=self._school(), proxy=self._proxy()
        )
        tasks = await client.fetch_unfinished()
        return tasks, client

    async def check_once(self, *, announce: bool = False) -> int:
        """跑一轮检查，返回本次推送的消息条数。"""
        token = self._token()
        if not token:
            return 0

        # 抓之前先看凭证要不要续，别等 401 了才反应
        await self._ensure_fresh_token()
        await self._warn_expiry_risk()

        client: XiaoyaClient | None = None
        try:
            try:
                try:
                    tasks, client = await self._collect()
                except TransientError:
                    # 平台抖一下就报「凭证失效」是假警报。重试一次，
                    # 还是不行就当临时故障，不惊动用户去重新扫码。
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                    tasks, client = await self._collect()
            except AuthExpired:
                # 有 refresh token 就自己续一次再试，还不行才算真过期
                if not await self.try_refresh():
                    raise
                tasks, client = await self._collect()
            pending = filter_by_window(tasks)
            self.storage.mark_check(True)
            self.storage.clear_transient_failure()

            # 顺手记一下用户昵称
            with contextlib.suppress(Exception):
                info = await client.whoami()
                if isinstance(info, dict):
                    name = str(
                        info.get("name")
                        or info.get("real_name")
                        or info.get("nickname")
                        or ""
                    )
                    if name:
                        self.storage.set_user_name(name)

            live_keys = {t.key for t in pending}
            new_tasks = [t for t in pending if not self.storage.has_notified(t.key)]
            urgent_tasks = [
                t
                for t in pending
                if t.hours_left <= self._remind_hours()
                and not self.storage.has_urged(t.key)
            ]

            sent = 0
            host = self._host()

            if announce:
                # 手动查询：直接给完整清单，不改推送状态
                await self._push_text(render_digest(pending, f"{self._school_label()} 待办"))
                return 1

            if not self._push_enabled():
                # 开关关着的时候别动推送记录，不然任务会被静默标记成「已推送」，
                # 等你重新打开开关就什么都收不到了。只更新去重。
                self.storage.prune(live_keys)
                self.storage.save()
                return 0

            if new_tasks and self._flag("notify_new", True):
                await self._push_text(render_batch_new(new_tasks, host))
                self.storage.mark_notified([t.key for t in new_tasks])
                sent += 1

            if urgent_tasks and self._flag("notify_urgent", True):
                for task in urgent_tasks:
                    await self._push_text(render_urgent(task, host))
                    self.storage.mark_urged(task.key)
                    sent += 1
                    await asyncio.sleep(0.4)

            if not new_tasks:
                # 只更新已推送记录，不制造噪音
                self.storage.mark_notified(list(live_keys))
            self.storage.prune(live_keys)
            self.storage.save()
            self._alerted_expiry = False
            return sent

        except AuthExpired as exc:
            self.storage.mark_check(False, str(exc))
            self.storage.save()
            logger.warning("[小雅作业] 凭证失效：%s", exc)
            if not self._alerted_expiry and self._push_enabled():
                self._alerted_expiry = True
                # 有 refresh token 却还是 401，说明续期这条路也断了，
                # 说清楚是「得重扫」还是「压根没 refresh token」
                if self._refresh_token():
                    await self._push_text(render_token_expired())
                else:
                    await self._push_text(
                        render_refresh_failed("没有 refresh token，无法自动续期")
                    )
            return 1
        except TransientError as exc:
            # 平台临时故障。不报警——平台抖一下不等于凭证没了，
            # 让人白扫一次码比晚点发现问题更烦。
            self.storage.mark_check(False, str(exc))
            self.storage.bump_transient_failure()
            self.storage.save()
            logger.warning("[小雅作业] 平台临时故障：%s", exc)
            # 但如果续期凭证本身也已经过期，那是真的没救了——
            # 这时候报警是有依据的，不算猜。
            left = self._hours_left(self.storage.refresh_expires_at)
            if (
                left is not None
                and left <= 0
                and not self._alerted_expiry
                and self._push_enabled()
            ):
                self._alerted_expiry = True
                await self._push_text(
                    render_refresh_failed("续期凭证已经过期，且平台接口不响应")
                )
            return 0
        except XiaoyaError as exc:
            self.storage.mark_check(False, str(exc))
            self.storage.save()
            logger.warning("[小雅作业] 检查失败：%s", exc)
            return 0
        except Exception as exc:
            self.storage.mark_check(False, repr(exc))
            self.storage.save()
            logger.exception("[小雅作业] 检查时出现未预期错误")
            return 0
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.aclose()

    async def _loop(self) -> None:
        await asyncio.sleep(3)  # 启动后稍等，别跟 astrbot 抢资源
        while True:
            try:
                if self._token():
                    # 有凭证就去问平台，别自己替它下结论。
                    # 之前这里先看「多久没成功过」，一超过阈值就直接推
                    # 「凭证失效」并且用 elif 跳过了实际请求——于是刚绑定完
                    # last_success 还是 0，必然误报一次。
                    await self.check_once()
                elif not self._alerted_expiry and self._push_enabled():
                    # 压根没绑，任何检查都做不了，这时候才值得说一声
                    self._alerted_expiry = True
                    await self._push_text(render_token_expired())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[小雅作业] 轮询循环异常")
            await asyncio.sleep(self._interval_seconds())

    # ------------------------------------------------------------------ #
    # 扫码登录
    # ------------------------------------------------------------------ #

    async def _run_qr_login(self, session: str) -> None:
        try:
            ttl = int(self.config.get("qr_ttl_seconds", 60) or 60)
        except (TypeError, ValueError):
            ttl = 60
        ttl = max(30, min(ttl, 180))

        client = QrLoginClient(school=self._school(), proxy=self._proxy())
        try:
            qr = await client.create_session()
            png = DATA_DIR / "login_qr.png"
            path = render_qr_png(qr.qr_url, png)
            caption = render_login_pending(ttl)
            if path is not None:
                await self._reply_qr(session, path, caption)
            else:
                await self._reply_text(
                    session,
                    caption + f"\n装不上 qrcode 库，手动打开这个地址：\n{qr.qr_url}",
                )

            await client.wait_for_confirm(qr, timeout=float(ttl))
            result = await client.fetch_token(qr.state)

            self.config["access_token"] = result.token
            # refresh token 一起存下，续期靠它。拿不到就退化成「过期重扫」
            if result.refresh_token:
                self.config["refresh_token"] = result.refresh_token
            self._alerted_expiry = False
            self._alerted_refresh_risk = False
            self._alerted_refresh_failed = False
            self._save_config()
            self.storage.reset()
            self.storage.set_bind_session(session)
            self.storage.save()

            # 扫码发下来的 access token 只活十来小时，立刻续一次换成 7 天的，
            # 顺便把两个过期时刻记下来，后面提醒才有依据
            if result.refresh_token:
                with contextlib.suppress(Exception):
                    await self.try_refresh()

            # 登录成功后立刻验证一次，顺便给个像样的反馈
            name, courses, tasks = "", 0, 0
            probe = XiaoyaClient(result.token, school=self._school(), proxy=self._proxy())
            try:
                info = await probe.whoami()
                if isinstance(info, dict):
                    name = str(
                        info.get("name")
                        or info.get("real_name")
                        or info.get("nickname")
                        or ""
                    )
                with contextlib.suppress(Exception):
                    courses = len(await probe.fetch_courses(1))
                with contextlib.suppress(Exception):
                    tasks = len(filter_by_window(await probe.fetch_unfinished()))
            finally:
                with contextlib.suppress(Exception):
                    await probe.aclose()

            if name:
                self.storage.set_user_name(name)
                self.storage.save()
            await self._reply_text(
                session, render_login_done(name, courses, tasks, result.method)
            )
            logger.info("[小雅作业] 扫码登录成功（%s 路）", result.method)

        except QrLoginError as exc:
            diag = client.diag_text() if hasattr(client, "diag_text") else ""
            logger.warning("[小雅作业] 扫码登录失败：%s\n%s", exc, diag)
            text = f"⚠️ 扫码登录没成功：{exc}"
            if diag and self._flag("qr_debug", False):
                text += f"\n\n{diag}"
            text += "\n\n发 /小雅登录 可以重来。"
            await self._reply_text(session, text)
        except Exception as exc:
            logger.exception("[小雅作业] 扫码登录异常")
            await self._reply_text(session, f"⚠️ 扫码登录出错了：{exc}")
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()

    def _spawn_login(self, session: str) -> None:
        task = asyncio.create_task(self._run_qr_login(session))
        self._login_tasks.add(task)
        task.add_done_callback(self._login_tasks.discard)

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #

    @filter.command("小雅登录", alias={"小雅扫码登录", "xy登录", "小雅重登"})
    async def cmd_login(self, event: AstrMessageEvent):
        # 这里不做「已绑定就拒绝」。凭证过期是常态（access token 只活十来
        # 小时），把人挡在门外只能逼他去浏览器 F12 翻 cookie 手动绑，
        # 正好是扫码要免掉的事。重新扫码本来就是明确意图。
        # 而且 _run_qr_login 只在换到新 token 之后才覆盖，扫失败不会毁掉旧凭证。
        if self._login_tasks:
            yield event.plain_result(
                "已经有一个二维码在等了，扫那个就行。\n"
                "超过有效期会自己失效，迟到了再发一次 /小雅登录。"
            )
            return
        hint = await self._existing_credential_hint()
        self._spawn_login(str(event.unified_msg_origin))
        if hint:
            yield event.plain_result(hint + "\n正在生成二维码，马上发给你……")
        else:
            yield event.plain_result("正在生成二维码，马上发给你……")

    async def _existing_credential_hint(self) -> str:
        """已经绑过时，先说清楚旧凭证是死是活。"""
        token = self._token()
        if not token:
            return ""
        client = XiaoyaClient(token, school=self._school(), proxy=self._proxy())
        try:
            await client.whoami()
        except AuthExpired:
            return "旧凭证已经失效了，重新扫码换一个。"
        except XiaoyaError:
            # 网络抽风之类，说不准状态，别给结论
            return "已经绑定过，重新扫码会覆盖现有凭证。"
        else:
            return "已经绑定过且仍然有效，重新扫码会覆盖现有凭证。"
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()

    @filter.command("小雅绑定", alias={"xy绑定"})
    async def cmd_bind(self, event: AstrMessageEvent, token: str = ""):
        token = (token or "").strip()
        if not token:
            yield event.plain_result(
                "用法：/小雅绑定 <token>\n"
                "token 在浏览器 F12 → Application → Cookies 里找 WT-prd-access-token。"
            )
            return

        client = XiaoyaClient(token, school=self._school(), proxy=self._proxy())
        try:
            info = await client.whoami()
        except AuthExpired:
            yield event.plain_result("这个 token 平台不认，检查一下有没有复制全。")
            return
        except XiaoyaError as exc:
            yield event.plain_result(f"验证失败：{exc}")
            return
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()

        self.config["access_token"] = token
        self._alerted_expiry = False
        self._save_config()
        self.storage.reset()
        self.storage.clear_token_expiry()
        self.storage.set_bind_session(str(event.unified_msg_origin))
        name = ""
        if isinstance(info, dict):
            name = str(
                info.get("name") or info.get("real_name") or info.get("nickname") or ""
            )
        if name:
            self.storage.set_user_name(name)
        self.storage.save()
        yield event.plain_result(
            f"✅ 绑定成功，{name or '账号已验证'}。\n"
            "定时提醒已经在跑，/小雅状态 可以看情况。"
        )

    @filter.command("小雅解绑", alias={"xy解绑"})
    async def cmd_unbind(self, event: AstrMessageEvent):
        if not self._token():
            yield event.plain_result("本来就没绑。")
            return
        self.config["access_token"] = ""
        self.config["refresh_token"] = ""
        self._save_config()
        self.storage.reset()
        self.storage.clear_token_expiry()
        self.storage.save()
        self._alerted_expiry = False
        self._alerted_refresh_risk = False
        self._alerted_refresh_failed = False
        yield event.plain_result("已解绑，token 和推送记录都清了。")

    @filter.command("小雅作业", alias={"小雅待办", "xy作业"})
    async def cmd_tasks(self, event: AstrMessageEvent, days: str = ""):
        if not self._token():
            yield event.plain_result("还没绑定。发 /小雅登录 扫码。")
            return
        try:
            window = float(days) if days.strip() else None
        except ValueError:
            yield event.plain_result(f"「{days}」不是天数啊。试试 /小雅作业 3")
            return

        client = XiaoyaClient(self._token(), school=self._school(), proxy=self._proxy())
        try:
            tasks = await client.fetch_unfinished()
        except AuthExpired:
            self._alerted_expiry = True
            yield event.plain_result(render_token_expired())
            return
        except XiaoyaError as exc:
            yield event.plain_result(f"拉取失败：{exc}")
            return
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()

        pending = filter_by_window(tasks, window)
        title = self._school_label() + (" 待办" if window is None else f" {days} 天内待办")
        yield event.plain_result(render_digest(pending, title))

    @filter.command("小雅状态", alias={"xy状态"})
    async def cmd_status(self, event: AstrMessageEvent):
        token = self._token()
        name = self.storage.user_name
        if token:
            client = XiaoyaClient(token, school=self._school(), proxy=self._proxy())
            try:
                ok, info = await client.check_token()
                if ok and info and info != "有效":
                    name = info
            except Exception:
                pass
            finally:
                with contextlib.suppress(Exception):
                    await client.aclose()
        targets = self._sessions()
        yield event.plain_result(
            render_status(
                bound=bool(token),
                user_name=name,
                last_check=self.storage.last_check,
                last_success=self.storage.last_success,
                notified=len(self.storage.data.get("notified", {})),
                school_label=self._school_label(),
                error=self.storage.last_error,
                push_enabled=self._push_enabled(),
                push_target=targets[0] if targets else "",
                auto_refresh=self._auto_refresh(),
                has_refresh_token=bool(self._refresh_token()),
                access_hours=self._hours_left(self.storage.access_expires_at),
                refresh_hours=self._hours_left(self.storage.refresh_expires_at),
                transient_failures=self.storage.transient_failures,
            )
        )

    @filter.command("小雅推送", alias={"xy推送"})
    async def cmd_push(self, event: AstrMessageEvent, action: str = ""):
        verb = (action or "").strip()
        if verb:
            enabled = verb in ("开", "on", "开启", "true", "1")
            disabled = verb in ("关", "off", "关闭", "false", "0")
            if not (enabled or disabled):
                yield event.plain_result(
                    f"看不懂「{verb}」。用法：/小雅推送 开 或 /小雅推送 关，"
                    "不带参数就是看当前状态。"
                )
                return
            self.config["push_enabled"] = enabled
            self._save_config()
            if enabled:
                # 重新打开时把「已提醒过」的闸门放掉，当下的任务才算新任务
                self.storage.reset()
                self.storage.save()
                self._alerted_expiry = False
                yield event.plain_result(
                    "推送开了。下一轮检查会把当前未完成的作业推给你。"
                )
            else:
                yield event.plain_result(
                    "推送关了。指令查询照常，/小雅推送 开 可以重新打开。"
                )
            return

        targets = self._sessions()
        onoff = "开" if self._push_enabled() else "关"
        if targets:
            where = "\n".join(f"  · {t}" for t in targets)
        else:
            where = "  · 还没绑定，用 /小雅登录 绑一个"
        yield event.plain_result(
            f"定时推送：{onoff}\n"
            f"推送目标：\n{where}\n"
            f"轮询间隔：{self._interval_seconds() // 60} 分钟"
        )

    @filter.command("小雅帮助", alias={"小雅菜单", "xy帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        lines = [
            "【小雅作业提醒】",
            "─" * 22,
            "/小雅登录 — 扫码绑定（凭证过期了直接再发一次，会覆盖旧的）",
            "/小雅绑定 <token> — 手动换凭证",
            "/小雅解绑 — 清空凭证和推送记录",
            "/小雅作业 [天数] — 手动查未完成作业",
            "/小雅推送 [开|关] — 定时推送开关，不带参数看状态",
            "/小雅状态 — 看凭证有效期和轮询情况",
            "─" * 22,
            f"当前学校：{self._school_label()}",
            "只读提醒，不会替你提交任何任务。",
        ]
        yield event.plain_result("\n".join(lines))
