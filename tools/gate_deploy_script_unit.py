#!/usr/bin/env python3
"""门禁：部署脚本动的是**线上真正在跑的那个单元**。

== 为什么需要这条门禁 ==

`image_search_mcp_deploy.sh` 早先把单元名硬编码成 `image-search`，而线上跑的是
`image-search-mcp`（实测确认）。后果有两层：

1. 它重启的不是真正在跑的服务 —— "部署成功"了但线上还是旧代码；
2. 它会额外写一个 `/etc/systemd/system/image-search.service` 出来，
   同一台机器上出现两个长得很像的服务。

这类缺陷不会自己暴露：脚本每一步都成功，退出码 0。

== 怎么测 ==

把 `sudo` / `systemctl` / `uv` 换成本地桩，在临时目录里**真跑**脚本的更新分支，
然后断言它重启的是哪个单元名。用一个假的 `HOME`，免得 `find_uv` 摸到真的 uv
（那会真的去装/升级包）。

== 反向自检 ==

把脚本里的单元名改回 `image-search`，这条门禁必须变红。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "image_search_mcp_deploy.sh"

SUDO_STUB = """#!/bin/bash
exec "$@"
"""

#: systemctl 桩：cat 一个单元只是"存在性检查"，由 FAKE_UNIT_EXISTS 决定
SYSTEMCTL_STUB = """#!/bin/bash
case "$1" in
  cat)        exit "${FAKE_UNIT_EXISTS:-1}" ;;
  restart)    echo "STUB restart $2" ;;
  is-active)  echo "active" ;;
  enable)     echo "STUB enable $2" ;;
  daemon-reload) : ;;
  *)          : ;;
esac
"""

UV_STUB = """#!/bin/bash
echo "STUB uv $*"
"""


def run_deploy(unit_name: str | None, unit_exists: bool) -> subprocess.CompletedProcess:
    tmp = Path(tempfile.mkdtemp(prefix="deploy-gate-"))
    try:
        bindir = tmp / "bin"
        (bindir / "home").mkdir(parents=True)
        bindir.mkdir(exist_ok=True)
        for name, body in (
            ("sudo", SUDO_STUB),
            ("systemctl", SYSTEMCTL_STUB),
            ("uv", UV_STUB),
        ):
            path = bindir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)

        # 触发"已有配置 → 走更新分支"
        (tmp / ".env").write_text("PORT=8000\n", encoding="utf-8")

        env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            "HOME": str(bindir / "home"),          # 别摸到真的 uv
            "FAKE_UNIT_EXISTS": "0" if unit_exists else "1",
        }
        if unit_name is not None:
            env["IMAGE_SEARCH_UNIT"] = unit_name

        return subprocess.run(
            ["bash", str(DEPLOY)],
            input="y\n",
            capture_output=True,
            text=True,
            cwd=tmp,
            env=env,
            timeout=60,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check() -> list[str]:
    problems: list[str] = []

    # ---- 1. 单元存在：必须重启 image-search-mcp ----------------------------
    ok = run_deploy(None, unit_exists=True)
    out = ok.stdout + ok.stderr
    if "STUB restart image-search-mcp" not in out:
        problems.append(
            f"没有重启 image-search-mcp（线上真正的单元名）：{out!r}"
        )
    if ok.returncode != 0:
        problems.append(f"更新分支本该成功，却退出码 {ok.returncode}：{out!r}")

    # ---- 2. 单元不存在：必须**响亮地失败**，而不是假装成功 -----------------
    missing = run_deploy(None, unit_exists=False)
    out_missing = missing.stdout + missing.stderr
    if missing.returncode == 0:
        problems.append(
            "单元不存在时脚本仍然报了成功 —— 线上会被静默地漏掉一次部署："
            f"{out_missing!r}"
        )
    if "image-search-mcp" not in out_missing:
        problems.append(f"失败信息里没说清是哪个单元：{out_missing!r}")

    # ---- 3. 单元名可覆盖（目标机器叫别的名字时） --------------------------
    custom = run_deploy("other-mcp", unit_exists=True)
    out_custom = custom.stdout + custom.stderr
    if "STUB restart other-mcp" not in out_custom:
        problems.append(
            f"IMAGE_SEARCH_UNIT 覆盖没有生效：{out_custom!r}"
        )

    return problems


def main() -> int:
    if not DEPLOY.exists():
        print(f"找不到 {DEPLOY}", file=sys.stderr)
        return 2

    problems = check()

    # ---- 反向自检：把单元名改回 image-search，门禁必须变红 ----------------
    original = DEPLOY.read_text(encoding="utf-8")
    marker = 'UNIT_NAME="${IMAGE_SEARCH_UNIT:-image-search-mcp}"'
    if marker not in original:
        problems.append(
            "反向自检没法进行：脚本里找不到单元名那行。"
            "多半是源码改了而这条门禁没跟着改 —— 它已经失效了"
        )
    else:
        broken = original.replace(
            marker, 'UNIT_NAME="image-search"', 1
        )
        DEPLOY.write_text(broken, encoding="utf-8")
        try:
            if not check():
                problems.append(
                    "反向自检失败：把单元名改回 image-search 之后门禁仍然是绿的"
                    " —— 说明它测不出这个缺陷"
                )
        finally:
            DEPLOY.write_text(original, encoding="utf-8")

    if problems:
        print("部署脚本的单元名有问题：")
        for item in problems:
            print(f"  - {item}")
        return 1

    print("部署脚本动的单元与线上一致（缺失时会响亮失败；含反向自检）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
