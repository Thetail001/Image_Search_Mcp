"""门禁：``tests/`` 下的测试源码不许被 .gitignore 吞掉。

为什么值得一道门禁：被忽略的测试文件在本地跑得好好的，却永远不会提交 ——
CI 上一片绿，别人的机器上什么都不跑，而"只在本机存在"的测试是最难查的一类问题。
实测过一次：``.gitignore`` 里 ``test_logic.py`` 没锚定到仓库根，
于是它会匹配任意目录下的同名文件，将来往 ``tests/`` 里放一个就会被静默吞掉。

**这道门禁刻意写窄**：只关心 ``.py`` 源码。早期版本把 ``tests/`` 下**所有**
被忽略的文件都算问题，结果每次都被 ``__pycache__/*.pyc`` 报红 ——
那种误报的门禁活不了几天，最后只会被人关掉，比没有门禁更糟。

用法：
    python tools/gate_tests_not_ignored.py            # 检查当前仓库
    python tools/gate_tests_not_ignored.py --self-test # 反向自检门禁自己
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 这些路径里的文件即便被忽略也不算问题
BENIGN_MARKERS = ("__pycache__/", ".pytest_cache/")


def _run_git(args: list[str], cwd: Path) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=cwd, timeout=120
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def ignored_test_sources(repo: Path) -> list[str]:
    """返回 ``tests/`` 下被 .gitignore 忽略的测试源码（相对路径）。"""
    code, out = _run_git(
        ["ls-files", "--others", "--ignored", "--exclude-standard", "tests/"],
        repo,
    )
    if code != 0:
        raise RuntimeError(f"git ls-files 失败（退出码 {code}）：{out}")
    if not out:
        return []

    result = []
    for line in out.splitlines():
        rel = line.strip()
        if not rel.endswith(".py"):
            continue
        if any(marker in rel for marker in BENIGN_MARKERS):
            continue
        result.append(rel)
    return result


def check(repo: Path) -> int:
    """检查一个仓库。返回 0 表示通过。"""
    try:
        offenders = ignored_test_sources(repo)
    except RuntimeError as exc:
        print(f"   → 无法检查：{exc}", file=sys.stderr)
        return 2

    if offenders:
        print("   → 不通过：以下测试源码被 .gitignore 忽略了，永远不会被提交")
        for rel in offenders:
            print(f"     {rel}")
        print("   修法：把那几条规则锚定到仓库根，例如 test_logic.py → /test_logic.py")
        return 1

    tracked_code, tracked = _run_git(["ls-files", "tests/"], repo)
    if tracked_code == 0:
        count = len([line for line in tracked.splitlines() if line.endswith(".py")])
        print(f"   → 通过（tests/ 下已跟踪的 .py 文件：{count} 个，被忽略的：0 个）")
    else:
        print("   → 通过（被忽略的测试源码：0 个）")
    return 0


def self_test() -> int:
    """反向自检：故意造一个被忽略的测试文件，门禁必须报出来。

    不这么做的话，这道门禁有可能永远返回"通过"（比如 pathspec 写错、
    git 命令静默失败），而没人会知道 —— 一个永不报警的报警器。
    """
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="gate-selftest-") as tmp:
        repo = Path(tmp)
        _run_git(["init", "-q"], repo)
        (repo / "tests").mkdir()
        # 一条**没有锚定**的忽略规则：会匹配任意目录下的同名文件
        (repo / ".gitignore").write_text("test_logic.py\n", encoding="utf-8")
        (repo / "tests" / "test_logic.py").write_text("", encoding="utf-8")
        (repo / "tests" / "test_clean.py").write_text("", encoding="utf-8")

        found = ignored_test_sources(repo)

        if "tests/test_logic.py" not in found:
            failures.append(
                f"造了一个被忽略的 tests/test_logic.py，门禁却没报出来（实际：{found}）"
            )
        if "tests/test_clean.py" in found:
            failures.append("没被忽略的 tests/test_clean.py 被误报了")

        # 第二个场景：修好规则之后必须回到通过
        (repo / ".gitignore").write_text("/test_logic.py\n", encoding="utf-8")
        after = ignored_test_sources(repo)
        if after:
            failures.append(f"把规则锚定到仓库根之后仍被报出：{after}")

        # 第三个场景：__pycache__ 里的 .pyc 不许误报（这正是早期版本的毛病）
        pycache = repo / "tests" / "__pycache__"
        pycache.mkdir()
        (pycache / "test_clean.cpython-311.pyc").write_text("", encoding="utf-8")
        (repo / ".gitignore").write_text("/test_logic.py\n__pycache__/\n", encoding="utf-8")
        with_pycache = ignored_test_sources(repo)
        if with_pycache:
            failures.append(
                f"__pycache__ 里的 .pyc 被误报了（这正是把门禁改窄的原因）：{with_pycache}"
            )

    if failures:
        print("   → 反向自检不通过，这道门禁不可信：")
        for item in failures:
            print(f"     {item}")
        return 1

    print("   → 反向自检通过（造出的被忽略文件被报出；干净文件与 __pycache__ 未被误报）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="门禁：测试源码未被 git 忽略")
    parser.add_argument("--self-test", action="store_true", help="反向自检门禁自己")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="检查哪个仓库")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    return check(Path(args.repo))


if __name__ == "__main__":
    raise SystemExit(main())
