"""storage 模块的测试。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.storage import Storage


def _tmp() -> Path:
    return Path(tempfile.mkdtemp()) / "state.json"


def test_load_missing_file():
    s = Storage(_tmp())
    s.load()
    assert s.last_check == 0.0
    assert s.data["notified"] == {}


def test_load_corrupt_file():
    path = _tmp()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    s = Storage(path)
    s.load()
    assert s.last_check == 0.0


def test_save_and_reload():
    path = _tmp()
    s = Storage(path)
    s.load()
    s.mark_check(True)
    s.set_user_name("小明")
    s.mark_notified(["a", "b"])
    s.mark_urged("a")
    s.save()

    s2 = Storage(path)
    s2.load()
    assert s2.user_name == "小明"
    assert s2.has_notified("a")
    assert s2.has_notified("b")
    assert s2.has_urged("a")
    assert not s2.has_urged("b")
    assert s2.last_check > 0


def test_mark_notified_is_idempotent():
    s = Storage(_tmp())
    s.load()
    s.mark_notified(["x"])
    first = s.data["notified"]["x"]
    time.sleep(0.01)
    s.mark_notified(["x"])
    assert s.data["notified"]["x"] == first


def test_prune_drops_finished():
    s = Storage(_tmp())
    s.load()
    s.mark_notified(["keep", "gone"])
    s.mark_urged("gone")
    s.prune({"keep"})
    assert set(s.data["notified"]) == {"keep"}
    assert s.data["urged"] == {}


def test_mark_check_error():
    s = Storage(_tmp())
    s.load()
    s.mark_check(False, "boom")
    assert s.last_error == "boom"
    assert s.last_success == 0.0
    s.mark_check(True)
    assert s.last_error == ""
    assert s.last_success > 0


def test_stale():
    s = Storage(_tmp())
    s.load()
    assert s.is_stale() is True
    s.mark_check(True)
    assert s.is_stale() is False


def test_reset():
    s = Storage(_tmp())
    s.load()
    s.mark_notified(["a"])
    s.set_user_name("x")
    s.reset()
    assert s.data["notified"] == {}
    assert s.user_name == ""


def test_saved_file_is_valid_json():
    path = _tmp()
    s = Storage(path)
    s.load()
    s.mark_check(True)
    s.mark_notified(["a"])
    s.save()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["version"] == 1
    assert "a" in loaded["notified"]


def test_transient_failure_counter():
    s = Storage(_tmp())
    assert s.transient_failures == 0
    s.bump_transient_failure()
    s.bump_transient_failure()
    assert s.transient_failures == 2
    s.clear_transient_failure()
    assert s.transient_failures == 0


def test_transient_failures_persist():
    p = _tmp()
    a = Storage(p)
    a.bump_transient_failure()
    a.save()
    b = Storage(p)
    b.load()
    assert b.transient_failures == 1


def test_is_stale_means_never_succeeded_not_expired():
    """last_success 为 0 是「还没成功过」，不能当成「凭证失效」。

    以前轮询循环直接拿这个判断去推失效告警，于是每次重新绑定都误报。
    """
    s = Storage(_tmp())
    assert s.last_success == 0.0
    assert s.is_stale() is True
    s.mark_check(True)
    assert s.is_stale() is False
