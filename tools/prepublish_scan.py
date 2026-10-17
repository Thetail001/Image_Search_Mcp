#!/usr/bin/env python3
"""门禁：公开仓库里不许出现内部标识与凭据。

扫描范围：
1. 工作区里被 git 跟踪的文件
2. 当前分支的提交历史（按**唯一 blob** 遍历，不是按提交重复扫）
3. ``dist/`` 里的 sdist/wheel（发布物同样是对外内容）

命中即非 0 退出，打印「路径@提交:行号:命中的词」。

**为什么不用 ``git grep``**：它走 POSIX ERE，不支持 ``\\d`` / ``\\b``，用它写
``\\bus-0\\d\\b`` 会**一行都匹配不到**，把真泄露判成干净（实测踩过）。这里统一用
Python 的 ``re``，工作区与历史口径一致。

自带 ``--self-test``：临时仓库里塞内部词 → 必须红；删掉后再提交 → 工作区绿但历史必须红。
不做反向测试的门禁不算门禁。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 内部标识：主机代号、私人称呼、私有路径、内网地址、私有域名
INTERNAL = [
    r"(?<![A-Za-z0-9])us-0[0-9](?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])hk-0[0-9](?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])de-0[0-9](?![A-Za-z0-9])",
    r"尾巴",
    r"阿沙",
    r"/root/\.hermes",
    r"/home/[a-z]+/\.hermes",
    r"(?<![0-9.])100\.50\.170\.[0-9]+(?![0-9])",
    r"(?<![0-9.])189\.24\.81\.[0-9]+(?![0-9])",
    r"(?<![0-9.])47\.243\.239\.[0-9]+(?![0-9])",
    r"(?<![A-Za-z0-9.-])thetail\.xyz(?![A-Za-z0-9.-])",
]

#: 凭据形状：只有「看起来像真值」才拦，占位值放行（由 PLACEHOLDER 判断）
SECRETS = [
    r"\bsk-[A-Za-z0-9_\-]{20,}",
    r"\bghp_[A-Za-z0-9]{20,}",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"MCP_AUTH_TOKEN\s*=\s*[A-Za-z0-9_\-]{16,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
]

#: 出现这些词的值视为占位符，不算泄露
PLACEHOLDER = re.compile(r"example|dummy|test|fake|sample|placeholder|changeme|redacted|xxxx|123456|your[-_]?", re.I)

#: 门禁自己的文件（里面写着这些词作为模式）
ALLOWLIST_PATHS = {"tools/prepublish_scan.py"}

COMPILED = re.compile("|".join(INTERNAL + SECRETS), re.IGNORECASE)
SECRET_PATTERN = re.compile("|".join(SECRETS))


def _is_placeholder(match: str) -> bool:
    return bool(PLACEHOLDER.search(match))


def _scan_text(label: str, text: str) -> list[str]:
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in COMPILED.finditer(line):
            token = match.group(0)
            # 只有凭据类才做占位符判断；内部标识一律算命中
            if SECRET_PATTERN.fullmatch(token) and _is_placeholder(token):
                continue
            hits.append(f"{label}:{lineno}:{token}")
    return hits


def _scan_bytes(label: str, data: bytes) -> list[str]:
    if b"\x00" in data[:4096]:
        return []
    return _scan_text(label, data.decode("utf-8", errors="replace"))


def _read(path: Path) -> bytes | None:
    try:
        if path.stat().st_size > 5_000_000:
            return None
        return path.read_bytes()
    except OSError:
        return None


def worktree_hits(root: Path) -> list[str]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout.split(b"\x00")
    hits: list[str] = []
    for raw in listing:
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="surrogateescape")
        if rel in ALLOWLIST_PATHS:
            continue
        data = _read(root / rel)
        if data is not None:
            hits += _scan_bytes(rel, data)
    return hits


TEXT_SUFFIXES = {
    ".md", ".rst", ".txt", ".py", ".sh", ".bash", ".toml", ".cfg", ".ini",
    ".json", ".yaml", ".yml", ".sql", ".html", ".css", ".js", ".ts", ".ipynb",
}


def history_hits(root: Path, all_refs: bool) -> list[str]:
    """按唯一 blob 遍历历史：同一份内容只扫一次。

    只读文本类后缀，避免把图片/轮子当文本硬解（也省时间）。
    """
    objects = subprocess.run(
        ["git", "rev-list", "--objects", "--all" if all_refs else "HEAD"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.splitlines()

    seen: set[str] = set()
    targets: list[tuple[str, str]] = []
    for line in objects:
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue  # 没有路径的是 commit/tree 本身
        sha, path = parts
        if path in ALLOWLIST_PATHS or sha in seen:
            continue
        if Path(path).suffix.lower() not in TEXT_SUFFIXES:
            continue
        seen.add(sha)
        targets.append((sha, path))

    hits: list[str] = []
    for sha, path in targets:
        proc = subprocess.run(
            ["git", "cat-file", "blob", sha],
            cwd=root, capture_output=True, timeout=20, check=False,
        )
        if proc.returncode != 0 or len(proc.stdout) > 5_000_000:
            continue
        if b"\x00" in proc.stdout[:4096]:
            continue
        hits += _scan_bytes(f"{path}@{sha[:7]}", proc.stdout)
    return hits


def dist_hits(root: Path) -> list[str]:
    dist = root / "dist"
    if not dist.is_dir():
        return []
    hits: list[str] = []
    for path in sorted(dist.iterdir()):
        if path.suffix == ".tar.gz":
            with tarfile.open(path) as tar:
                for member in tar.getmembers():
                    if member.isfile() and member.size <= 5_000_000:
                        handle = tar.extractfile(member)
                        if handle is not None:
                            hits += _scan_bytes(f"{path.name}:{member.name}", handle.read())
        elif path.suffix == ".whl":
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    if zf.getinfo(name).file_size <= 5_000_000:
                        hits += _scan_bytes(f"{path.name}:{name}", zf.read(name))
    return hits


def scan(root: Path, *, all_refs: bool, history: bool, dist: bool) -> list[str]:
    hits = worktree_hits(root)
    if history:
        hits += history_hits(root, all_refs)
    if dist:
        hits += dist_hits(root)
    return hits


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def self_test() -> int:
    problems: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="prepublish-selftest-"))
    try:
        repo = tmp / "repo"
        repo.mkdir()
        _run(["git", "init", "-q"], repo)
        _run(["git", "config", "user.email", "test@example.invalid"], repo)
        _run(["git", "config", "user.name", "test"], repo)
        (repo / "clean.md").write_text("nothing sensitive here\n", encoding="utf-8")
        _run(["git", "add", "."], repo)
        _run(["git", "commit", "-qm", "clean"], repo)

        if scan(repo, all_refs=False, history=True, dist=False):
            problems.append("干净仓库被判成有问题（误报）")

        (repo / "leak.md").write_text("deployed on us-01\n", encoding="utf-8")
        _run(["git", "add", "."], repo)
        _run(["git", "commit", "-qm", "leak"], repo)
        if not scan(repo, all_refs=False, history=True, dist=False):
            problems.append("工作区里有内部词却没抓到（漏报）")

        (repo / "leak.md").unlink()
        _run(["git", "add", "-u"], repo)
        _run(["git", "commit", "-qm", "remove"], repo)
        if worktree_hits(repo):
            problems.append("文件已删，工作区却仍有命中")
        if not history_hits(repo, False):
            problems.append("历史里的内部词没抓到（只扫当前树是不够的）")

        # 占位凭据不能误报
        (repo / "cfg.md").write_text("MCP_AUTH_TOKEN=example-token-123456\n", encoding="utf-8")
        if worktree_hits(repo):
            problems.append("占位凭据被误报成泄露")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if problems:
        print("prepublish 门禁自检失败：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("prepublish 门禁自检通过（误报 / 工作区漏报 / 历史漏报 / 占位误报 四项都验过）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="扫所有 ref 的历史（默认只扫当前分支）")
    parser.add_argument("--no-history", action="store_true", help="跳过历史扫描")
    parser.add_argument("--no-dist", action="store_true", help="跳过 dist/ 产物扫描")
    parser.add_argument("--self-test", action="store_true", help="运行反向自检")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    try:
        hits = scan(ROOT, all_refs=args.all, history=not args.no_history, dist=not args.no_dist)
    except Exception as exc:  # noqa: BLE001 - 门禁自己出错也要非 0
        print(f"门禁执行失败：{exc}", file=sys.stderr)
        return 2

    if hits:
        print(f"发现 {len(hits)} 处内部标识/凭据，公开仓库不能包含：")
        for line in hits[:60]:
            print(f"  {line}")
        if len(hits) > 60:
            print(f"  ……另有 {len(hits) - 60} 处")
        print("\n修法：换成通用示例（如 example.com、/srv/app）；内部笔记移出仓库并加入 .gitignore。")
        return 1

    print("公开内容检查通过：工作区、历史、发布物里都没有内部标识与凭据")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
