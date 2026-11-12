#!/usr/bin/env python3
"""引擎健康看门狗：真打上游，回答"现在哪家还能用"。

为什么需要它：单元测试用的是录制/模拟响应，能防"代码被改坏"，但防不住
"上游站点改版"。2026-11 的 Yandex 故障就是这样——498 个测试全绿，
服务实际已经搜不出任何东西，靠人偶然发现。

用法：
    python tools/engine_health.py                 # 默认查全部引擎
    python tools/engine_health.py --engines BaiDu Tineye
    python tools/engine_health.py --json          # 机器可读
    python tools/engine_health.py --required BaiDu # 指定"必须可用"的引擎，失守则退出码 1

礼貌节流：全局串行，每次请求间隔 --gap 秒（默认 5），只打 1 张固定测试图。
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Iterable, Sequence

#: 固定测试图：埃菲尔铁塔（维基共享，公开可下载、六家引擎都认识）。
DEFAULT_IMAGE = (
    "https://commons.wikimedia.org/wiki/Special:FilePath/"
    "Tour_Eiffel_Wikimedia_Commons.jpg?width=600"
)

#: 引擎名 → PicImageSearch 的模块名（两者不总是一样，例如 GoogleLens）。
MODULES: dict[str, str] = {
    "SauceNAO": "saucenao",
    "Google": "google",
    "TraceMoe": "tracemoe",
    "Ascii2D": "ascii2d",
    "BaiDu": "baidu",
    "Bing": "bing",
    "EHentai": "ehentai",
    "GoogleLens": "google_lens",
    "Iqdb": "iqdb",
    "Tineye": "tineye",
    "Yandex": "yandex",
}

#: 判定标准：出结果才算可用。空结果既可能是"这张图没被收录"，也可能是坏了，
#: 所以单独记成 EMPTY，不当成通过。
STATUS_OK = "OK"
STATUS_EMPTY = "EMPTY"
STATUS_ERROR = "ERROR"


@dataclass
class EngineResult:
    engine: str
    status: str
    count: int
    seconds: float
    detail: str = ""


def verdict(results: Sequence[EngineResult], required: Iterable[str]) -> tuple[bool, list[str]]:
    """纯判定：给定结果与"必须可用"清单，返回 (是否通过, 问题描述列表)。"""
    by_engine = {r.engine: r for r in results}
    problems: list[str] = []
    for name in required:
        found = by_engine.get(name)
        if found is None:
            problems.append(f"{name}: 未执行检查")
        elif found.status != STATUS_OK:
            reason = found.detail or found.status
            problems.append(f"{name}: {reason}")
    return (not problems, problems)


def summarize(results: Sequence[EngineResult]) -> dict[str, Any]:
    """把结果聚成机器可读的摘要。"""
    return {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "image": DEFAULT_IMAGE,
        "total": len(results),
        "ok": [r.engine for r in results if r.status == STATUS_OK],
        "empty": [r.engine for r in results if r.status == STATUS_EMPTY],
        "error": {r.engine: r.detail for r in results if r.status == STATUS_ERROR},
        "results": [asdict(r) for r in results],
    }


async def probe(
    engine: str,
    image_url: str,
    *,
    call: Callable[[str, str], Awaitable[Any]] | None = None,
) -> EngineResult:
    """查一个引擎。``call`` 可注入，便于测试时避开网络。"""
    if call is None:
        call = _real_call
    started = time.time()
    try:
        raw = await call(engine, image_url)
    except Exception as exc:  # noqa: BLE001 - 任何失败都记成 ERROR，不中断整轮
        return EngineResult(
            engine=engine,
            status=STATUS_ERROR,
            count=0,
            seconds=round(time.time() - started, 1),
            detail=f"{type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:140]}",
        )
    items = raw or []
    return EngineResult(
        engine=engine,
        status=STATUS_OK if items else STATUS_EMPTY,
        count=len(items),
        seconds=round(time.time() - started, 1),
    )


async def _real_call(engine: str, image_url: str) -> Any:
    """真的打一次上游。"""
    module = importlib.import_module(f"PicImageSearch.engines.{MODULES[engine]}")
    client = getattr(module, engine)()
    try:
        response = await client.search(url=image_url)
        return getattr(response, "raw", None) or []
    finally:
        try:
            await client.close()
        except Exception:  # pragma: no cover - 关不上不影响结论
            pass


async def run(
    engines: Sequence[str],
    image_url: str,
    gap: float,
    *,
    call: Callable[[str, str], Awaitable[Any]] | None = None,
) -> list[EngineResult]:
    """串行跑完一轮，请求之间留出间隔以免被上游判成爬虫。

    ``call`` 可注入，测试时用来避开真实网络。
    """
    out: list[EngineResult] = []
    for index, engine in enumerate(engines):
        if engine not in MODULES:
            out.append(EngineResult(engine, STATUS_ERROR, 0, 0.0, "未知引擎"))
            continue
        if index:
            await asyncio.sleep(gap)
        out.append(await probe(engine, image_url, call=call))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="图片搜索引擎健康检查")
    parser.add_argument("--engines", nargs="*", default=None, help="要检查的引擎；默认全部")
    parser.add_argument("--required", nargs="*", default=None,
                        help="必须可用的引擎；默认取环境变量 ENGINE_HEALTH_REQUIRED 或 BaiDu")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="测试图 URL")
    parser.add_argument("--gap", type=float, default=5.0, help="请求间隔秒数（默认 5）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    engines = args.engines or list(MODULES)
    required = args.required
    if required is None:
        env = os.environ.get("ENGINE_HEALTH_REQUIRED", "")
        required = [x for x in env.replace(",", " ").split() if x] or ["BaiDu"]

    results = asyncio.run(run(engines, args.image, args.gap))
    passed, problems = verdict(results, required)

    if args.json:
        payload = summarize(results)
        payload["required"] = required
        payload["passed"] = passed
        payload["problems"] = problems
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(f"测试图: {args.image}")
        print(f"{'引擎':11s} {'状态':7s} {'条数':>4s} {'耗时':>6s}  说明")
        for r in results:
            mark = {STATUS_OK: "✅", STATUS_EMPTY: "⚪"}.get(r.status, "❌")
            print(f"{mark} {r.engine:11s} {r.status:7s} {r.count:>4d} {r.seconds:>5.1f}s  {r.detail[:70]}")
        print()
        if passed:
            print(f"通过：必须可用的引擎都在出结果（{', '.join(required)}）")
        else:
            print("失守：")
            for line in problems:
                print(f"  - {line}")
        ok = [r.engine for r in results if r.status == STATUS_OK]
        print(f"本轮出结果的引擎: {', '.join(ok) if ok else '（一个都没有）'}")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
