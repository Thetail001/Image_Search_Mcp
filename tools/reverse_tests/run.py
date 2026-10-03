"""反向测试执行器：把每个缺陷放回代码里，确认门禁真的会红。

用法：
    python tools/reverse_tests/run.py            # 跑全部
    python tools/reverse_tests/run.py --list     # 只列出注入项

工作方式（这四步缺一步，反向测试就不成立）：

1. 把 ``src/`` ``tests/`` ``pyproject.toml`` 复制到一个临时目录
2. 在**副本**里做文本替换注入（不动工作区，避免"忘了还原"这种事）
3. 用 ``PYTHONPATH=<副本>/src`` 跑指定的那条测试，并**先验证导入到的模块
   确实是副本里的那一份** —— 否则跑的还是干净代码，反向测试是假的
4. 判定：测试必须**因断言失败**而红（退出码 1）。
   退出码 2 是收集/用法错误、5 是没收集到测试 —— 那不算抓住，
   因为"跑不起来"和"发现问题"是两件事。旧门禁恰恰就是在这里骗过自己的。

判定结果分布越是集中在"没抓住"，越说明门禁是摆设。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bugs import INJECTIONS, BugInjection  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_TIMEOUT = 60
TEST_TIMEOUT = 300

#: 只复制这些顶层条目到临时副本（够跑测试就行，不含 tools/ 与 .venv/）
COPY_ENTRIES = ("src", "tests", "pyproject.toml", "README.md", "LICENSE")


@dataclass
class Outcome:
    injection: BugInjection
    caught: bool
    detail: str

    @property
    def symbol(self) -> str:
        return "抓住" if self.caught else "没抓住"


def _make_copy(workdir: Path) -> Path:
    target = workdir / "repo"
    target.mkdir()
    for entry in COPY_ENTRIES:
        source = REPO_ROOT / entry
        if source.is_dir():
            shutil.copytree(source, target / entry,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, target / entry)
    return target


def _apply(injection: BugInjection, repo: Path) -> str | None:
    """在副本里做替换。返回 None 表示成功，否则返回错误说明。"""
    path = repo / injection.path
    if not path.exists():
        return f"找不到文件 {injection.path}"

    text = path.read_text(encoding="utf-8")
    occurrences = text.count(injection.old)
    if occurrences == 0:
        return (
            f"注入片段在 {injection.path} 里找不到 —— "
            "多半是源码改了而注入没跟着改，这条门禁已经失效"
        )
    if occurrences > 1:
        return f"注入片段在 {injection.path} 里出现 {occurrences} 次，无法确定改哪一处"

    path.write_text(text.replace(injection.old, injection.new), encoding="utf-8")
    return None


def _env(repo: Path) -> dict[str, str]:
    import os

    env = dict(os.environ)
    # 副本的 src 必须排在前面，才能盖过可编辑安装指向的原始路径
    env["PYTHONPATH"] = f"{repo / 'src'}:{env.get('PYTHONPATH', '')}".rstrip(":")
    env.pop("MCP_AUTH_TOKEN", None)
    return env


def _verify_module_origin(repo: Path, env: dict[str, str], python: str) -> str | None:
    """确认导入到的是副本里的模块。返回 None 表示确认通过。"""
    probe = (
        "import image_search_mcp.server as s, image_search_mcp.main as m, "
        "image_search_mcp.credentials as c, image_search_mcp.safe_download as d; "
        "print(s.__file__); print(m.__file__); print(c.__file__); print(d.__file__)"
    )
    result = subprocess.run(
        [python, "-c", probe], capture_output=True, text=True,
        env=env, cwd=repo, timeout=PROBE_TIMEOUT,
    )
    if result.returncode != 0:
        return f"导入探针失败：{result.stderr.strip()[:400]}"

    expected = str(repo.resolve())
    for line in result.stdout.strip().splitlines():
        if not line.startswith(expected):
            return (
                "导入到的模块不在临时副本里，跑的是干净代码 —— "
                f"反向测试无效。探针输出：{result.stdout.strip()[:300]}"
            )
    return None


def run_one(injection: BugInjection, python: str, workdir: Path, quiet: bool) -> Outcome:
    repo = _make_copy(workdir)

    problem = _apply(injection, repo)
    if problem is not None:
        return Outcome(injection, False, problem)

    env = _env(repo)

    origin_problem = _verify_module_origin(repo, env, python)
    if origin_problem is not None:
        return Outcome(injection, False, origin_problem)

    result = subprocess.run(
        [python, "-m", "pytest", injection.test, "-q",
         "--timeout=30", "--timeout-method=thread", "-p", "no:cacheprovider"],
        capture_output=True, text=True, env=env, cwd=repo, timeout=TEST_TIMEOUT,
    )
    output = result.stdout + result.stderr
    tail = "\n".join(output.strip().splitlines()[-12:])

    if result.returncode == 0:
        return Outcome(
            injection, False,
            "注入之后测试仍然通过 —— 这条门禁是摆设，它不会发现这个缺陷",
        )

    if result.returncode == 1:
        test_name = injection.test.split("::", 1)[1]

        # 测试确实跑了：名字要出现在输出里（超时堆栈里也会带）
        if test_name not in output:
            return Outcome(
                injection, False,
                f"红了但不是指定的那条测试（输出里找不到 {test_name}）：{tail}",
            )

        if "Timeout" in output and "FAILED" not in output and "failed" not in output:
            # 抓住了，但方式不干净。仍然算门禁有效 —— 只是要指出来。
            return Outcome(
                injection, True,
                "被抓住了，但是以**超时**的方式（挂到 pytest-timeout 才结束），"
                "不是一条干净的断言失败。建议改测试：让它快速失败，"
                "否则 CI 上表现为『卡住』而不是『失败』",
            )

        if "FAILED" not in output and "failed" not in output:
            return Outcome(injection, False, f"退出码 1 但输出里没有失败记录：{tail}")

        return Outcome(injection, True, "指定测试变红，符合预期")

    if result.returncode in (2, 5):
        return Outcome(
            injection, False,
            f"退出码 {result.returncode}：跑不起来（收集/用法错误），这不等于抓住了缺陷。"
            f"{tail}",
        )
    return Outcome(injection, False, f"退出码 {result.returncode}：{tail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="反向测试：确认门禁会红")
    parser.add_argument("--list", action="store_true", help="只列出注入项")
    parser.add_argument("--only", action="append", default=None,
                        help="只跑指定的注入（可重复）")
    parser.add_argument("--python", default=sys.executable, help="用哪个解释器")
    args = parser.parse_args(argv)

    selected = INJECTIONS
    if args.only:
        wanted = set(args.only)
        selected = tuple(i for i in INJECTIONS if i.name in wanted)
        missing = wanted - {i.name for i in selected}
        if missing:
            print(f"没有这些注入：{sorted(missing)}", file=sys.stderr)
            return 2

    if args.list:
        for injection in selected:
            print(f"{injection.name}\n    {injection.path}\n    → {injection.test}\n    {injection.why}")
        return 0

    print(f"反向测试：{len(selected)} 项注入\n" + "=" * 72)
    outcomes: list[Outcome] = []
    for index, injection in enumerate(selected, 1):
        print(f"\n[{index}/{len(selected)}] {injection.name}")
        print(f"    放回：{injection.why}")
        print(f"    应被抓住：{injection.test}")
        with tempfile.TemporaryDirectory(prefix="reverse-") as tmp:
            outcome = run_one(injection, args.python, Path(tmp), quiet=False)
        outcomes.append(outcome)
        print(f"    → {outcome.symbol}：{outcome.detail}")
        if outcome.caught:
            print("    ✓ 门禁有效")

    caught = [o for o in outcomes if o.caught]
    missed = [o for o in outcomes if not o.caught]

    print("\n" + "=" * 72)
    print(f"合计：抓住 {len(caught)}/{len(outcomes)}")
    for outcome in missed:
        print(f"  没抓住：{outcome.injection.name} —— {outcome.detail}")

    if missed:
        print("\n结论：有门禁不会发现它本该发现的缺陷。这比没有门禁更糟 —— "
              "因为它看起来有人在看着。")
        return 1

    print("\n结论：每条注入都被指定的测试抓住，门禁可判定。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
