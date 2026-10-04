#!/usr/bin/env python3
"""门禁自检：``check.sh`` 的 ``step()`` 必须报出**真实的**退出码。

== 为什么需要这条门禁 ==

``step()`` 原先是这么写的：

    if "$@"; then
      PASSED_STEPS+=("$name"); echo "   → 通过"; return 0
    fi
    local code=$?          # ← 读的是 if 复合命令自己的状态

POSIX 规定：if 复合命令在条件为假且没有 else 分支时，退出状态是 **0**。
于是任何失败都被印成"退出码 0"，``return`` 也返回 0 —— 报出来的原因是假的。
(实测：反向测试那条步骤明明报"不通过"，后面却挂着"退出码 0"。)

这类缺陷很难自己暴露：失败的**判定**是对的（FAILED_STEPS 里记上了），
只有**原因**是错的。而人排查时看的就是那个原因。

== 怎么测 ==

从 ``check.sh`` 里把 ``step`` 函数**抠出来**放进 bash 子进程，喂两条命令：
一条成功的（``true``）和一条以退出码 7 失败的。
断言成功那次报"通过"且返回 0，失败那次报出真实的 7 且返回 7。

抠函数而不是复制一份：复制出来的副本只能证明副本是对的。

== 反向自检 ==

把抠出来的 ``step`` 改回**上面那种旧写法**，这条门禁必须变红。
不做这一步的话，一个永远返回"通过"的空壳子也能骗过它。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "check.sh"

#: 修复后的写法（抠出来做替换用）
FIXED_BLOCK = (
    '  local code=0\n'
    '  "$@" || code=$?\n'
    '  if [[ $code -eq 0 ]]; then\n'
    '    PASSED_STEPS+=("$name")\n'
    '    echo "   → 通过"\n'
    '    return 0\n'
    '  fi\n'
)

#: 缺陷写法：if 之后才读 $?
BUGGY_BLOCK = (
    '  if "$@"; then\n'
    '    PASSED_STEPS+=("$name")\n'
    '    echo "   → 通过"\n'
    '    return 0\n'
    '  fi\n'
    '  local code=$?\n'
)


def extract_step_function(text: str) -> str:
    """把 ``step() { ... }`` 整段抠出来。"""
    start = text.index("step() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def run_step_function(function_source: str, body: str) -> subprocess.CompletedProcess:
    script = function_source + "\n" + body
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def check(function_source: str) -> list[str]:
    """返回问题清单；空列表表示这条门禁通过。"""
    problems: list[str] = []

    ok = run_step_function(
        function_source,
        'FAILED_STEPS=()\n'
        'PASSED_STEPS=()\n'
        'step "假的成功步骤" true\n'
        'echo "RET=$?"',
    )
    if "→ 通过" not in ok.stdout:
        problems.append(f"成功的命令没被报成通过：{ok.stdout!r} / {ok.stderr!r}")
    if "RET=0" not in ok.stdout:
        problems.append(f"成功的命令没有返回 0：{ok.stdout!r}")

    bad = run_step_function(
        function_source,
        'FAILED_STEPS=()\n'
        'PASSED_STEPS=()\n'
        'step "假的失败步骤" bash -c "exit 7"\n'
        'echo "RET=$?"',
    )
    if "退出码 7" not in bad.stdout:
        problems.append(
            "失败的命令没有报出真实的退出码 7 —— 报出来的原因是假的："
            f"{bad.stdout!r} / {bad.stderr!r}"
        )
    if "RET=7" not in bad.stdout:
        problems.append(
            f"step() 没有把真实退出码返回给调用方：{bad.stdout!r}"
        )
    if "假的失败步骤" not in bad.stdout:
        problems.append(f"失败没有被记进清单：{bad.stdout!r}")

    return problems


def main() -> int:
    if not CHECK.exists():
        print(f"找不到 {CHECK}", file=sys.stderr)
        return 2

    source = CHECK.read_text(encoding="utf-8")

    try:
        function_source = extract_step_function(source)
    except ValueError:
        print("check.sh 里找不到 step() { ... } 函数 —— 门禁失效，请跟着源码改", file=sys.stderr)
        return 2

    problems = check(function_source)

    # ---- 反向自检：改回旧写法，门禁必须变红 --------------------------------
    if FIXED_BLOCK not in function_source:
        problems.append(
            "反向自检没法进行：抠出来的 step() 里找不到修复后的那段。"
            "多半是源码改了而这条门禁没跟着改 —— 它已经失效了"
        )
    else:
        buggy = function_source.replace(FIXED_BLOCK, BUGGY_BLOCK, 1)
        if buggy == function_source:  # pragma: no cover - 上面已经拦住了
            problems.append("反向自检构造失败：替换没有生效")
        elif not check(buggy):
            problems.append(
                "反向自检失败：把 step() 改回'if 之后才读 $?'的旧写法之后，"
                "这条门禁仍然是绿的 —— 说明它根本测不出这个缺陷"
            )

    if problems:
        print("check.sh 的退出码上报有问题：")
        for item in problems:
            print(f"  - {item}")
        return 1

    print("step() 的上报退出码与真实退出码一致（含反向自检）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
