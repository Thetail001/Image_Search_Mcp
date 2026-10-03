"""从代码和产物生成实施状态，供文档引用。

存在理由：手写的"当前进度"必腐烂。实测过一次 —— 工程 README 写着
「汉化 v1.5 / 流水线九步 / 校验 11 项」，而实际早已是 V4.1 / 43 步。
入口文档过期比没有入口文档更糟：它会让人按错的东西去排查。

所以文档里凡是能从这里取到的，就不许手抄。

用法：
    python tools/status.py            # 人读
    python tools/status.py --markdown # 直接贴进文档
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        args, capture_output=True, text=True, cwd=REPO_ROOT, timeout=300
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _git(*args: str) -> str:
    code, out = _run(["git", *args])
    return out if code == 0 else "（不可用）"


def collect_tests() -> dict[str, int]:
    """每个测试文件有多少条用例 —— 靠 pytest 自己数，不靠数文件里的 def。"""
    code, out = _run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider"]
    )
    if code not in (0, 5):
        return {}
    counts: Counter[str] = Counter()
    for line in out.splitlines():
        match = re.match(r"^(tests/[\w/]+\.py)::", line.strip())
        if match:
            counts[match.group(1)] += 1
    return dict(sorted(counts.items()))


def count_injections() -> int:
    """反向测试注入条数 —— 问脚本本身，不数 bugs.py 里的 ``BugInjection(``。"""
    code, out = _run([sys.executable, "tools/reverse_tests/run.py", "--list"])
    if code != 0:
        return 0
    # --list 每条注入输出三行，第一行是注入名（顶格），后两行缩进。
    return len([line for line in out.splitlines() if line and not line.startswith(" ")])


def repo_facts() -> dict[str, object]:
    changed = _git("status", "--porcelain")
    changed_files = [line[3:] for line in changed.splitlines() if line.strip()]
    return {
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        # 刻意**不**记录本地 HEAD 的哈希：这份文档就在仓库里，
        # 任何写进它的哈希都会在"包含它的那个提交"完成的那一刻失效 ——
        # 自指的元数据必然过期。稳定的是基线（origin/main）和分支名。
        "base": _git("rev-parse", "--short", "origin/main"),
        "base_subject": _git("log", "-1", "--pretty=%s", "origin/main"),
        "dirty_count": len(changed_files),
        "changed_files": changed_files,
    }


def engine_facts() -> dict[str, object]:
    """引擎数与参数契约条目数 —— 直接问代码，不数文档。"""
    code, out = _run([
        sys.executable, "-c",
        "import sys; sys.path.insert(0, 'src');"
        "from image_search_mcp import params, server;"
        "print(len(server.ENGINES));"
        "print(len(params.CONTRACTS));"
        "print(sorted(server.ENGINES));",
    ])
    if code != 0:
        return {"error": out[:300]}
    lines = out.splitlines()
    return {
        "engines": int(lines[0]),
        "contracts": int(lines[1]),
        "engine_names": lines[2],
    }


def line_counts(paths: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for rel in paths:
        path = REPO_ROOT / rel
        if path.is_file():
            out[rel] = len(path.read_text(encoding="utf-8").splitlines())
    return out


NEW_SOURCES = [
    "src/image_search_mcp/params.py",
    "src/image_search_mcp/credentials.py",
    "src/image_search_mcp/safe_download.py",
]
NEW_TESTS = [
    "tests/conftest.py",
    "tests/test_param_contract.py",
    "tests/test_param_validation.py",
    "tests/test_credentials.py",
    "tests/test_safe_download.py",
    "tests/test_extras_whitelist.py",
    "tests/test_search_flow.py",
    "tests/test_result_formatting.py",
    "tests/test_auth_asgi.py",
]


def build() -> dict[str, object]:
    return {
        "repo": repo_facts(),
        "engines": engine_facts(),
        "tests": collect_tests(),
        "injections": count_injections(),
        "new_source_lines": line_counts(NEW_SOURCES),
        "new_test_lines": line_counts(NEW_TESTS),
    }


def render_markdown(data: dict[str, object]) -> str:
    repo = data["repo"]  # type: ignore[assignment]
    engines = data["engines"]  # type: ignore[assignment]
    tests = data["tests"]  # type: ignore[assignment]
    total_tests = sum(tests.values())

    lines = [
        "<!-- 本段由 tools/status.py 生成，勿手改。重新生成：",
        "     python tools/status.py --update docs/实施进度.md -->",
        "",
        f"- 分支：`{repo['branch']}`　基线：`origin/main@{repo['base']}`"
        f"（{repo['base_subject']}）",
        f"- 相对基线的改动：{repo['dirty_count']} 个未提交文件",
        f"- 引擎：{engines['engines']} 个，参数契约覆盖 {engines['contracts']} 个",
        f"- 测试：**{total_tests}** 条",
        f"- 反向测试注入：**{data['injections']}** 条",
        "",
        "### 各测试文件用例数",
        "",
    ]
    for path, count in tests.items():
        lines.append(f"- `{path}` — {count}")

    lines += ["", "### 新增源码规模", ""]
    for path, count in data["new_source_lines"].items():  # type: ignore[union-attr]
        lines.append(f"- `{path}` — {count} 行")

    lines += ["", "### 新增/改动测试规模", ""]
    for path, count in data["new_test_lines"].items():  # type: ignore[union-attr]
        lines.append(f"- `{path}` — {count} 行")

    return "\n".join(lines)


MARKER_START = "<!-- 本段由 tools/status.py 生成"
MARKER_END = "---"


def update_file(path: Path) -> int:
    """把生成的状态段写回文档里，替换两个标记之间的内容。

    手抄数字必腐烂 —— 实测过一次：工程 README 写着「汉化 v1.5 / 流水线九步」，
    而实际早已是 V4.1 / 43 步。所以这里连"把生成结果粘贴进文档"这一步
    也交给脚本做，人不要经手。
    """
    if not path.is_file():
        print(f"找不到文件：{path}", file=sys.stderr)
        return 2

    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(MARKER_START))
    except StopIteration:
        print(f"{path} 里找不到起始标记 {MARKER_START!r}", file=sys.stderr)
        return 2

    try:
        end = next(
            i for i, line in enumerate(lines[start:], start) if line.strip() == MARKER_END
        )
    except StopIteration:
        print(f"{path} 里 {start} 行之后找不到结束标记 {MARKER_END!r}", file=sys.stderr)
        return 2

    generated = render_markdown(build()).splitlines()
    new_lines = lines[:start] + generated + [""] + lines[end:]
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    print(f"已更新 {path}（第 {start + 1}–{end + 1} 行）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成实施状态")
    parser.add_argument("--markdown", action="store_true", help="输出 markdown")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--update", metavar="文件", help="把状态段写回指定文档")
    args = parser.parse_args(argv)

    if args.update:
        return update_file(Path(args.update))

    data = build()
    if args.json:
        import json

        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
