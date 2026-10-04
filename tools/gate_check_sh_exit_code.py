#!/usr/bin/env python3
"""门禁自检：``check.sh`` 的 ``step()`` 必须报出**真实的**退出码，并把失败**记进清单**。

== 为什么需要这条门禁（两个都真实发生过）==

1. ``step()`` 原先是这么写的：

       if "$@"; then
         PASSED_STEPS+=("$name"); echo "   → 通过"; return 0
       fi
       local code=$?          # ← 读的是 if 复合命令自己的状态

   POSIX 规定：if 复合命令在条件为假且没有 else 分支时，退出状态是 **0**。
   于是任何失败都被印成"退出码 0"，``return`` 也返回 0 —— 报出来的原因是假的。

2. 后来这条门禁自己的断言是弱的：它只检查"步骤名出现在输出里"，
   可是 ``step()`` **开头就会打印名字**。所以把记进 ``FAILED_STEPS`` 的那一行删掉，
   这条门禁照样绿（复核报告第 10 问实测返回空问题清单）。

这类缺陷很难自己暴露：判定**看起来**是对的，只有原因是错的（或者根本没记上）。
所以这里既查"报出来的退出码"，也查"清单里到底有几个、是哪一个"。

== 怎么测 ==

从 ``check.sh`` 里把 ``step`` 函数**抠出来**放进 bash 子进程（带 ``set -uo pipefail``，
与 check.sh 一致），喂成功/失败两条命令，断言：

- 成功那次：报"通过"、返回 0、``PASSED_STEPS`` 恰好 1 项且是它、``FAILED_STEPS`` 为空；
- 失败那次：报出真实的 7、返回 7、``FAILED_STEPS`` 恰好 1 项且是它、``PASSED_STEPS`` 为空；
- 端到端：照搬 check.sh 末尾的汇总逻辑跑一遍，有失败步骤时进程必须真的退出 1。

抠函数而不是复制一份：复制出来的副本只能证明副本是对的。

== 反向自检 ==

在**抠出来的副本**上（不动仓库文件）做两次变异，都必须让门禁变红：
① 把 ``step`` 改回"if 之后才读 $?"的旧写法；② 删掉"记进清单"那一行。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "check.sh"

TIMEOUT = 60

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

#: 在**副本**上删掉这一行来验证门禁真的在读数组
APPEND_MARKER = "FAILED_STEPS+=("


def extract_step_function(text: str) -> str:
    """把 ``step() { ... }`` 整段抠出来。

    按**大括号深度**找收尾，而不是"第一个独占一行的 }"。判定前先剥掉行尾注释 ——
    函数头真实写法是 ``step() {  # step <名字> <命令...>``，行尾不是 ``{``，
    不剥注释就会把整份文件都当成函数体（写这条门禁时就先踩了一次）。
    """
    start = text.index("step() {")
    depth = 0
    collected: list[str] = []
    for line in text[start:].splitlines(keepends=True):
        collected.append(line)
        code = line.split("#", 1)[0].strip()
        if code.endswith("{"):
            depth += 1
        if code.startswith("}"):
            depth -= 1
            if depth <= 0:
                break
    return "".join(collected)


def run_step_function(function_source: str, body: str) -> subprocess.CompletedProcess:
    # set -uo pipefail 与 check.sh 保持一致：不这样做，这里跑的是一个"更宽松"的环境，
    # 未定义变量之类的差异就会让我们测不到真实行为。
    script = "set -uo pipefail\n" + function_source + "\n" + body
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=TIMEOUT,
    )


def _list_of() -> str:
    """打印两个清单的**计数与内容**（真去看数组，不看有没有打印过名字）。

    同时打计数是因为 ``set -u`` 下空数组的 ``${arr[*]}`` 行为不够稳，
    而 ``${#arr[@]}`` 一定是可靠的。
    """
    return (
        'printf "FAILED_N=%s\\n" "${#FAILED_STEPS[@]}"\n'
        'printf "PASSED_N=%s\\n" "${#PASSED_STEPS[@]}"\n'
        'printf "FAILED_LIST=%s\\n" "${FAILED_STEPS[*]:-}"\n'
        'printf "PASSED_LIST=%s\\n" "${PASSED_STEPS[*]:-}"\n'
    )


def check(function_source: str) -> list[str]:
    """返回问题清单；空列表表示这条门禁通过。"""
    problems: list[str] = []

    # ---- 成功步骤 --------------------------------------------------------
    ok = run_step_function(
        function_source,
        "FAILED_STEPS=()\n"
        "PASSED_STEPS=()\n"
        'step "假的成功步骤" true\n'
        'echo "RET=$?"\n'
        + _list_of(),
    )
    if "→ 通过" not in ok.stdout:
        problems.append(f"成功的命令没被报成通过：{ok.stdout!r} / {ok.stderr!r}")
    if "RET=0" not in ok.stdout:
        problems.append(f"成功的命令没有返回 0：{ok.stdout!r}")
    if "PASSED_N=1" not in ok.stdout or "假的成功步骤" not in ok.stdout.split("PASSED_LIST=")[-1]:
        problems.append(f"成功的步骤没被记进 PASSED_STEPS：{ok.stdout!r}")
    if "FAILED_N=0" not in ok.stdout:
        problems.append(f"成功那轮 FAILED_STEPS 不为空：{ok.stdout!r}")

    # ---- 失败步骤 --------------------------------------------------------
    bad = run_step_function(
        function_source,
        "FAILED_STEPS=()\n"
        "PASSED_STEPS=()\n"
        'step "假的失败步骤" bash -c "exit 7"\n'
        'echo "RET=$?"\n'
        + _list_of(),
    )
    if "退出码 7" not in bad.stdout:
        problems.append(
            "失败的命令没有报出真实的退出码 7 —— 报出来的原因是假的："
            f"{bad.stdout!r} / {bad.stderr!r}"
        )
    if "RET=7" not in bad.stdout:
        problems.append(f"step() 没有把真实退出码返回给调用方：{bad.stdout!r}")
    if "FAILED_N=1" not in bad.stdout or "假的失败步骤" not in bad.stdout.split("FAILED_LIST=")[-1]:
        problems.append(
            "失败的步骤没有被记进 FAILED_STEPS —— 汇总会漏掉它，整体退出码会是 0"
            "（旧断言只看输出里有没有名字，正好被这条骗过）："
            f"{bad.stdout!r}"
        )
    if "PASSED_N=0" not in bad.stdout:
        problems.append(f"失败那轮 PASSED_STEPS 不为空（把失败算成通过了）：{bad.stdout!r}")

    # ---- 端到端：照搬 check.sh 末尾的汇总逻辑 ----------------------------
    summary_body = (
        "FAILED_STEPS=()\n"
        "PASSED_STEPS=()\n"
        'step "甲" true || true\n'
        'step "乙" bash -c "exit 3" || true\n'
        "if [[ ${#FAILED_STEPS[@]} -gt 0 ]]; then exit 1; fi\n"
        "exit 0\n"
    )
    summary = run_step_function(function_source, summary_body)
    if summary.returncode != 1:
        problems.append(
            "汇总逻辑在有条步骤失败时仍然退出 0 —— 门禁整体是绿的，"
            f"CI 会放行一个有失败的提交：退出码 {summary.returncode}，{summary.stdout!r}"
        )

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

    # ---- 反向自检 ①：改回旧写法，门禁必须变红 ------------------------------
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

    # ---- 反向自检 ②：删掉「记进清单」那一行，门禁必须变红 -------------------
    kept = [line for line in function_source.splitlines(keepends=True)
            if APPEND_MARKER not in line]
    dropped = "".join(kept)
    if dropped == function_source:
        problems.append(
            f"反向自检没法进行：step() 里找不到 {APPEND_MARKER} 那行（源码形状变了？）"
        )
    elif not check(dropped):
        problems.append(
            "反向自检失败：把「记进 FAILED_STEPS」删掉之后门禁仍然是绿的 —— "
            "说明它没有真去读数组（复核报告第 10 问就是这个漏洞）"
        )

    if problems:
        print("check.sh 的退出码上报有问题：")
        for item in problems:
            print(f"  - {item}")
        return 1

    print("step() 上报的退出码与真实值一致、失败真的进了清单（含两项反向自检）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
