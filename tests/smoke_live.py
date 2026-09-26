"""集成冒烟测试：打真实的小雅接口。

不是单元测试的替代品，只用来确认协议没猜错。默认不跑，需要显式执行：

    python tests/smoke_live.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.client import SCHOOLS, AuthExpired, XiaoyaClient
from core.qrlogin import QrLoginClient, render_qr_png

PROXY = os.environ.get("SMOKE_PROXY") or None


def ok(label: str, extra: str = "") -> None:
    print(f"  [OK]   {label}{(' — ' + extra) if extra else ''}")


def bad(label: str, extra: str = "") -> None:
    print(f"  [FAIL] {label}{(' — ' + extra) if extra else ''}")


async def test_bad_token_rejected() -> None:
    print("\n1. 无效 token 应被识别为 AuthExpired")
    client = XiaoyaClient("obviously-not-a-real-token", school="whut", proxy=PROXY)
    try:
        await client.fetch_unfinished()
        bad("无效 token 竟然拉到了数据")
    except AuthExpired as exc:
        ok("正确抛出 AuthExpired", str(exc))
    except Exception as exc:
        bad("抛出了非预期的异常", repr(exc))
    finally:
        await client.aclose()


async def test_courses_endpoint_shape() -> None:
    print("\n2. 课程列表接口连通性（预期 401，但能拿到 JSON 说明路径对）")
    client = XiaoyaClient("dummy", school="whut", proxy=PROXY)
    try:
        await client.fetch_courses(1)
        bad("dummy token 竟然通过了")
    except AuthExpired:
        ok("路径正确，服务端认得这个接口")
    except Exception as exc:
        bad("非预期异常", repr(exc))
    finally:
        await client.aclose()


async def test_school_hosts_reachable() -> None:
    print(f"\n3. 各学校站点可达性（代理：{PROXY or '直连'}）")
    import httpx

    for key, info in SCHOOLS.items():
        url = f"https://{info['host']}/api/jx-stat/group/task/un_finish"
        try:
            async with httpx.AsyncClient(timeout=15, proxy=PROXY) as c:
                r = await c.get(url, headers={"authorization": "Bearer x"})
            ctype = r.headers.get("content-type", "")
            if r.status_code == 401 and "json" in ctype:
                ok(f"{key} ({info['host']})", "返回 JSON 401")
            else:
                bad(f"{key}", f"HTTP {r.status_code} ctype={ctype}")
        except Exception as exc:
            bad(f"{key}", repr(exc))


async def test_qr_session() -> None:
    print("\n4. 扫码登录：取二维码 + 轮询状态")
    client = QrLoginClient(school="whut", proxy=PROXY)
    try:
        session = await client.create_session()
    except Exception as exc:
        bad("取二维码失败", repr(exc))
        await client.aclose()
        return
    ok("取到二维码", session.qr_url)
    ok("解析出 key", session.key)
    ok("state 长度", f"{len(session.state)}")

    try:
        status = await client.poll_status(session.key)
        ok("轮询成功", f"status={status} (0=等待扫码)")
    except Exception as exc:
        bad("轮询失败", repr(exc))
    finally:
        await client.aclose()

    dest = Path(tempfile.mkdtemp()) / "qr.png"
    path = render_qr_png(session.qr_url, dest)
    if path and path.exists() and path.stat().st_size > 500:
        ok("二维码渲染成功", f"{path} ({path.stat().st_size} bytes)")
        try:
            from PIL import Image

            with Image.open(path) as im:
                ok("PNG 可正常解码", f"{im.size[0]}x{im.size[1]} {im.format}")
        except ImportError:
            print("  [SKIP] 没装 PIL，跳过解码校验")
    else:
        bad("二维码渲染失败")


async def main() -> None:
    print("=" * 60)
    print("小雅作业提醒 · 集成冒烟测试")
    print("=" * 60)
    await test_bad_token_rejected()
    await test_courses_endpoint_shape()
    await test_school_hosts_reachable()
    await test_qr_session()
    print("\n" + "=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
