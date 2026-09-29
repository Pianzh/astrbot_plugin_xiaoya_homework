"""插件运行状态持久化。

记录哪些任务已经推送过、token 是否还有效、上次检查时间。
数据落在 ``<astrbot>/data/plugin_data/astrbot_plugin_xiaoya_homework/state.json``。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

# 超过这个天数没成功检查过，就认为 token 大概率过期了
STALE_AFTER_DAYS = 7

SCHEMA_VERSION = 1


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = self._empty()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "notified": {},  # task_key -> 首次推送时间戳
            "urged": {},  # task_key -> 临期催办时间戳
            "last_check": 0.0,
            "last_success": 0.0,
            "last_error": "",
            "user_name": "",
            # 绑定时的会话标识。不能放 AstrBot 配置里：不在 _conf_schema.json
            # 里的键会被 check_config_integrity 当成废弃项删掉，推送目标就废了。
            "bind_session": "",
            # 续期后拿到的过期时刻（ISO 串）。refresh 到期才是重扫的死线。
            "access_expires_at": "",
            "refresh_expires_at": "",
            "last_refresh_ok": 0.0,
            # 连续几次因为平台临时故障没拉到数据。只用于诊断，不触发告警。
            "transient_failures": 0,
        }

    # ---- 读写 ----

    def load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            self._data = self._empty()
            return
        except OSError:
            self._data = self._empty()
            return
        try:
            data = json.loads(raw)
        except ValueError:
            self._data = self._empty()
            return
        if not isinstance(data, dict):
            self._data = self._empty()
            return
        merged = self._empty()
        merged.update(data)
        self._data = merged

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    # ---- 访问器 ----

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    @property
    def last_check(self) -> float:
        return float(self._data.get("last_check") or 0.0)

    @property
    def last_success(self) -> float:
        return float(self._data.get("last_success") or 0.0)

    @property
    def last_error(self) -> str:
        return str(self._data.get("last_error") or "")

    @property
    def user_name(self) -> str:
        return str(self._data.get("user_name") or "")

    @property
    def bind_session(self) -> str:
        return str(self._data.get("bind_session") or "")

    def set_bind_session(self, session: str) -> None:
        self._data["bind_session"] = str(session or "").strip()

    # ---- token 有效期 ----

    @property
    def access_expires_at(self) -> str:
        return str(self._data.get("access_expires_at") or "")

    @property
    def refresh_expires_at(self) -> str:
        return str(self._data.get("refresh_expires_at") or "")

    @property
    def last_refresh_ok(self) -> float:
        return float(self._data.get("last_refresh_ok") or 0.0)

    def set_token_expiry(self, access_at: str, refresh_at: str) -> None:
        self._data["access_expires_at"] = str(access_at or "")
        self._data["refresh_expires_at"] = str(refresh_at or "")
        self._data["last_refresh_ok"] = time.time()

    def clear_token_expiry(self) -> None:
        self._data["access_expires_at"] = ""
        self._data["refresh_expires_at"] = ""
        self._data["last_refresh_ok"] = 0.0

    def mark_check(self, ok: bool, error: str = "") -> None:
        self._data["last_check"] = time.time()
        if ok:
            self._data["last_success"] = time.time()
            self._data["last_error"] = ""
        else:
            self._data["last_error"] = error[:500]

    @property
    def transient_failures(self) -> int:
        return int(self._data.get("transient_failures") or 0)

    def bump_transient_failure(self) -> None:
        self._data["transient_failures"] = self.transient_failures + 1

    def clear_transient_failure(self) -> None:
        self._data["transient_failures"] = 0

    def set_user_name(self, name: str) -> None:
        self._data["user_name"] = name

    def is_stale(self, days: float = STALE_AFTER_DAYS) -> bool:
        """多久没成功检查过了。

        注意：``last_success`` 为 0 表示「还没成功过」，不是「凭证失效」。
        以前这里返回 True，轮询循环据此直接推「凭证失效」并且跳过实际
        请求，于是刚绑定完必然误报一次。判断凭证死没死只能问平台。
        """
        last = self.last_success
        if last <= 0:
            return True
        return (time.time() - last) > days * 86400

    # ---- 去重 ----

    def has_notified(self, key: str) -> bool:
        return key in self._data["notified"]

    def has_urged(self, key: str) -> bool:
        return key in self._data["urged"]

    def mark_notified(self, keys: list[str]) -> None:
        now = time.time()
        for key in keys:
            self._data["notified"].setdefault(key, now)

    def mark_urged(self, key: str) -> None:
        self._data["urged"].setdefault(key, time.time())

    def prune(self, keep: set[str]) -> None:
        """丢掉已完成任务的记录，避免 state.json 无限膨胀。

        ``keep`` 是当前还存在的未完成任务的 key 集合。
        """
        self._data["notified"] = {
            k: v for k, v in self._data["notified"].items() if k in keep
        }
        self._data["urged"] = {
            k: v for k, v in self._data["urged"].items() if k in keep
        }

    def reset(self) -> None:
        """清掉去重与时间戳，但保留绑定会话——那是推送目标，不是推送记录。"""
        bind = self.bind_session
        self._data = self._empty()
        self._data["bind_session"] = bind
