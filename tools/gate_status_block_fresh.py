#!/usr/bin/env python3
"""门禁：文档里那一段「当前状态」必须是刚生成的，不能是过期的手抄数字。

为什么需要这道门禁
------------------
那一段写的全是**能从产物取到的事实**：测试条数、注入条数、源码行数、契约覆盖数。
实测踩过：加了测试、改了文件却忘了重新生成，文档就一直写着测试 367 条
（实际 390）、注入 31 条（实际 36），还漏掉了一个新增的测试文件。
**过期的入口文档比空白更糟** —— 它会让人按错的数字去排查。

做法
----
把文档复制到临时目录，在副本上跑**同一个生成器**（``tools/status.py --update``），
再把两边的生成段抽出来逐行比较。

刻意**复用生成器自己的标记常量**（``MARKER_START`` / ``MARKER_END``）来定位那一段，
不在这里另写一套扫描：两套定位逻辑必然分歧 —— 哪天标记或格式改了，
另一套会**悄悄失效**（自己不报错，只是再也抓不到东西，比没有门禁更坏）。

比较时**忽略**「相对基线的改动：N 个未提交文件」这一行：它随工作区状态变化，
手里只要有一个没提交的文件就会不一样，门禁就变成了"永远是红的"。
会误报的门禁最后只会被关掉，所以宁可窄一点。

用法
----
    python tools/gate_status_block_fresh.py               # 检查真实文档
    python tools/gate_status_block_fresh.py --doc X.md    # 检查指定文档（自检用）
    python tools/gate_status_block_fresh.py --self-test   # 反向自检：过期数字必须被抓
"""

from __future__ import annotations

import argparse
import difflib
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DOC = REPO / "docs" / "实施进度.md"
GENERATOR = REPO / "tools" / "status.py"

#: 这几行是**环境状态**，不是"从产物取到的事实"，比较时必须排除：
#:
#: - 「相对基线的改动」随工作区变化，手里有一个没提交的文件就不一样；
#: - 「分支 / 基线」在 CI 里必然是别的值（分支是 main、且浅克隆里可能没有
#:   ``origin/main``，生成器会写"（不可用）"）。
#:
#: 实测踩过：只忽略第一条时，门禁在本机绿、到 CI 里三个 Python 版本全红 ——
#: 那正好是"会误报的门禁最后只会被关掉"的形态，所以这条忽略规则也要有反向测试
#: （见 self_test 的情形 4）。
VOLATILE = re.compile(r"^\s*-\s*(相对基线的改动：|分支：)")

sys.path.insert(0, str(REPO / "tools"))
import status as status_module  # noqa: E402  （复用它的标记常量，见模块开头说明）


def _region(text: str) -> list[str] | None:
    """抽出生成段（含起止标记之间的内容）。找不到标记返回 None。"""
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines)
         if line.startswith(status_module.MARKER_START)), None
    )
    if start is None:
        return None
    end = next(
        (i for i, line in enumerate(lines[start:], start)
         if line.strip() == status_module.MARKER_END), None
    )
    if end is None:
        return None
    return lines[start:end]


def _comparable(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if not VOLATILE.match(line.strip())]


def regenerate(doc: pathlib.Path) -> tuple[bool, str, str]:
    """在临时副本上跑生成器。返回 (是否成功, 副本内容或空, 诊断信息)。"""
    with tempfile.TemporaryDirectory() as tmp:
        copy = pathlib.Path(tmp) / doc.name
        shutil.copy2(doc, copy)
        proc = subprocess.run(
            [sys.executable, str(GENERATOR), "--update", str(copy)],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            return False, "", (proc.stderr.strip() or proc.stdout.strip())
        return True, copy.read_text(encoding="utf-8"), ""


def check(doc: pathlib.Path) -> int:
    if not doc.is_file():
        print(f"找不到文档：{doc}")
        return 2

    ok, fresh_text, diag = regenerate(doc)
    if not ok:
        print(f"生成器没跑起来（退出码非 0）：{diag}")
        return 2

    original = _region(doc.read_text(encoding="utf-8"))
    fresh = _region(fresh_text)
    if original is None or fresh is None:
        print(f"抽不出生成段：原文={original is not None} 重新生成={fresh is not None}"
              f"（标记 {status_module.MARKER_START!r} / {status_module.MARKER_END!r}）")
        return 2

    before, after = _comparable(original), _comparable(fresh)
    if before == after:
        print(f"状态段是新鲜的（{len(after)} 行，与刚生成的一致）")
        return 0

    print("状态段过期：文档里的数字与刚生成的不一致。")
    print("修法：python tools/status.py --update docs/实施进度.md")
    print("差异（- 文档里写的 / + 实际生成的）：")
    for line in difflib.unified_diff(before, after, lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        print(f"  {line}")
    return 1


def self_test() -> int:
    """反向自检：三种情形都必须给出预期结果，否则这道门禁不算数。"""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = pathlib.Path(tmp)

        # 情形 1：新鲜文档 → 必须通过（否则门禁永远红，等于没有）
        fresh_doc = tmpdir / "fresh.md"
        shutil.copy2(DEFAULT_DOC, fresh_doc)
        ok, _, diag = regenerate(fresh_doc)
        if not ok:
            failures.append(f"情形 1 预处理失败：{diag}")
        else:
            code = check(fresh_doc)
            if code != 0:
                failures.append(f"情形 1：新鲜文档却判不通过（退出码 {code}）")
            else:
                print("情形 1 通过：新鲜文档判通过")

        # 情形 2：把测试条数改旧 → 必须被抓
        stale_doc = tmpdir / "stale.md"
        text = DEFAULT_DOC.read_text(encoding="utf-8")
        region = _region(text)
        if not region:
            failures.append("情形 2 预处理失败：抽不出生成段")
        else:
            mutated = re.sub(r"- 测试：\*\*(\d+)\*\* 条",
                             lambda m: f"- 测试：**{max(0, int(m.group(1)) - 1)}** 条",
                             text, count=1)
            if mutated == text:
                failures.append("情形 2 预处理失败：没能改到测试条数那一行")
            else:
                stale_doc.write_text(mutated, encoding="utf-8")
                code = check(stale_doc)
                if code == 0:
                    failures.append("情形 2：测试条数被改旧，门禁却判通过")
                else:
                    print(f"情形 2 通过：改旧测试条数被抓到（退出码 {code}）")

        # 情形 4：分支/基线行不同 → 必须**仍然通过**（CI 里就是这种情形）
        other_branch = tmpdir / "other-branch.md"
        swapped = re.sub(r"^- 分支：`[^`]*`", "- 分支：`main`", text, count=1, flags=re.M)
        if swapped == text:
            failures.append("情形 4 预处理失败：没能改到分支那一行")
        else:
            other_branch.write_text(swapped, encoding="utf-8")
            code = check(other_branch)
            if code != 0:
                failures.append(
                    f"情形 4：只有分支行不同却判不通过（退出码 {code}）"
                    " —— CI 里分支是 main，这会让门禁误报"
                )
            else:
                print("情形 4 通过：分支行不同不被判为过期（CI 安全）")

        # 情形 3：标记被删掉 → 必须响亮失败，不能静默通过
        broken_doc = tmpdir / "broken.md"
        broken_doc.write_text(
            text.replace(status_module.MARKER_START, "## 状态（标记被删了）", 1),
            encoding="utf-8",
        )
        code = check(broken_doc)
        if code == 0:
            failures.append("情形 3：起始标记被删，门禁却判通过")
        else:
            print(f"情形 3 通过：标记被删会响亮失败（退出码 {code}）")

    if failures:
        print("\n反向自检失败：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\n反向自检通过：四个情形都符合预期"
          "（新鲜通过、改旧被抓、分支行不同不误报、标记缺失响亮失败）")
    # 自检之后再检查**真实文档**：check.sh 只挂这一个入口，
    # 免得"跑了自检却没跑真检查"——那正好是假门禁的形态。
    print("\n---- 真实文档 ----")
    return check(DEFAULT_DOC)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查文档状态段是否新鲜")
    parser.add_argument("--doc", type=pathlib.Path, default=DEFAULT_DOC,
                        help="要检查的文档（默认 docs/实施进度.md）")
    parser.add_argument("--self-test", action="store_true", help="反向自检")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    return check(args.doc)


if __name__ == "__main__":
    raise SystemExit(main())
