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

    def mark_check(self, ok: bool, error: str = "") -> None:
        self._data["last_check"] = time.time()
        if ok:
            self._data["last_success"] = time.time()
            self._data["last_error"] = ""
        else:
            self._data["last_error"] = error[:500]

    def set_user_name(self, name: str) -> None:
        self._data["user_name"] = name

    def is_stale(self) -> bool:
        last = self.last_success
        if last <= 0:
            return True
        return (time.time() - last) > STALE_AFTER_DAYS * 86400

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
        self._data = self._empty()
