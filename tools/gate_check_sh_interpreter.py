#!/usr/bin/env python3
"""门禁：check.sh 选出来的解释器必须是**可用的绝对路径**。

为什么值得一条门禁：check.sh 顶部选 ``PY``，底下再把它传给子进程。历史上这个组合是坏的
—— 没有 .venv 时 ``PY=python3``，调用处又拼成 ``$(pwd)/python3``，于是 CI（恰好没有 .venv）
里最后一步直接 ``FileNotFoundError``。开发机有 .venv，所以这个 bug 在本机永远看不到
（复核报告第 12 问在临时副本里才复现出来）。

这条门禁的判定方式刻意不依赖"源码看起来对不对"：

1. **真跑一遍选择逻辑**：在临时目录里造两种布局（有 .venv / 没有 .venv），
   各跑一次那段 if-chain，断言 ``PY`` 是绝对路径、且指向真实存在的解释器。
2. **调用处不许把路径拼起来**：``$PY`` 已经是绝对路径了，再拼 ``$(pwd)/$PY``
   就是这一类 bug 的复发点。规则收窄到只查这一种拼接，不查别的用法。
3. **自带反向自检**：在临时副本里把选择逻辑改回旧写法，断言上面的检查会红。
   不做反向自检的门禁不算门禁 —— 而自检**只动临时副本**，绝不碰仓库里的文件。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECK_SH = REPO / "check.sh"

#: 选解释器那段的起点。用固定标记定位，比"扫到某个 } 就停"稳。
SELECT_START = "if [[ -x .venv/bin/python ]]; then"
#: 调用处把路径拼起来的形状（这一类 bug 的复发点）
CONCAT_PATTERNS = (r"\$\(pwd\)/\$PY", r"\$\{?PWD\}?/\$PY")
TIMEOUT = 30


def extract_selection_block(text: str) -> str:
    """取出选解释器那段 if/elif/else/fi（按嵌套深度找配对的 fi）。"""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == SELECT_START)
    except StopIteration:  # pragma: no cover - 仓库被改坏时才会走到
        raise SystemExit(f"在 {CHECK_SH.name} 里找不到选解释器的起点：{SELECT_START}")

    depth = 0
    for index in range(start, len(lines)):
        stripped = lines[index].strip()
        if re.match(r"^if\b", stripped):
            depth += 1
        elif re.match(r"^fi\b", stripped):
            depth -= 1
            if depth == 0:
                return "\n".join(lines[start : index + 1])
    raise SystemExit("选解释器的 if 没有配对的 fi")  # pragma: no cover


def run_selection(block: str, cwd: Path, path_dir: Path) -> subprocess.CompletedProcess:
    script = "set -u\n" + block + '\nprintf "%s" "$PY"\n'
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(cwd),
        env={
            "PATH": f"{path_dir}:/usr/bin:/bin",
            "HOME": str(cwd),
        },
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )


def check_layout(block: str, with_venv: bool) -> list[str]:
    """造一种布局真跑一次，返回问题清单（空表示通过）。"""
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        stub_dir = root / "stubbin"
        stub_dir.mkdir()
        (stub_dir / "python3").symlink_to(sys.executable)

        layout = root / "repo"
        layout.mkdir()
        if with_venv:
            venv_bin = layout / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            (venv_bin / "python").symlink_to(sys.executable)

        result = run_selection(block, cwd=layout, path_dir=stub_dir)
        if result.returncode != 0:
            return [f"选择逻辑退出码 {result.returncode}：{result.stderr.strip()[:200]}"]
        py = result.stdout.strip()
        if not py:
            return ["选择逻辑没有给出 PY"]
        if not py.startswith("/"):
            problems.append(f"PY 不是绝对路径：{py!r}（子进程换目录后就会失效）")
        if not Path(py).exists():
            problems.append(f"PY 指向的解释器不存在：{py!r}")
        want_venv = with_venv
        if want_venv and ".venv" not in py:
            problems.append(f"有 .venv 时没有优先用它：{py!r}")
        if not want_venv and ".venv" in py:
            problems.append(f"没有 .venv 时却指向了 .venv：{py!r}")
    return problems


def check_call_sites(text: str) -> list[str]:
    """调用处不许把 $PY 和 $(pwd) 拼在一起。"""
    problems = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("#"):
            continue
        for pattern in CONCAT_PATTERNS:
            if re.search(pattern, line):
                problems.append(
                    f"check.sh:{number} 仍在把 $PY 和路径拼起来：{line.strip()[:90]}"
                )
    return problems


def main() -> int:
    text = CHECK_SH.read_text(encoding="utf-8")
    block = extract_selection_block(text)

    problems = check_call_sites(text)
    problems += check_layout(block, with_venv=False)
    problems += check_layout(block, with_venv=True)

    # ---- 反向自检：把选择逻辑改回旧写法，必须变红 -------------------------
    old_block = block.replace('PY="$(command -v python3)"', "PY=python3")
    if old_block == block:
        print("反向自检没法进行：源码形状改了而这条门禁没跟着改 —— 它已经失效了")
        return 1
    reverse = check_layout(old_block, with_venv=False)
    if not reverse:
        problems.append(
            "反向自检失败：把 PY 改回裸 python3 之后，检查仍然是绿的 —— 它测不出这个缺陷"
        )

    if problems:
        print("check.sh 的解释器处理有问题：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("check.sh 的解释器是绝对路径、两处布局都可用（含反向自检）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
