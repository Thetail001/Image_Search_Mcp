#!/usr/bin/env python3
"""门禁：部署脚本的单元名/单元路径与线上一致，且失败必须响亮。

== 为什么需要这条门禁 ==

早先脚本里硬编码成 ``image-search``，而线上跑的是 ``image-search-mcp``。后果不是
"报个错"，而是：重启一个不存在的服务名（什么也没发生），再额外造一个同名的假单元。
这种错误**在输出里看不出来**（打印的仍然是"更新完成"），只有把真正执行的命令记下来
对比才能发现。所以这里 stub 掉 systemctl/uv/curl/apt，把 argv 逐条记下来做**精确**
比对，而不是在输出里找子串 —— 子串比对会把 ``image-search-mcp-wrong`` 也判成通过。

== 旧版本最难看的一点：这条门禁自己会改写仓库里的真脚本 ==

旧实现的反向自检直接在仓库里的 ``image_search_mcp_deploy.sh`` 上改，再在 finally 里
写回。实测确认它确实动过真文件（内容能还原，但 mtime 变了）：内容恰好一致，所以
"改坏了没有"这类检查全看不出来。现在**所有变异都在临时副本上做**，
并且在结尾核对仓库文件的 mtime 与摘要没变过 —— 这条自检本身就是那个 bug 的门禁。

== 每台机器都能跑 ==

不写 ``/etc/systemd/system``：用 ``IMAGE_SEARCH_SYSTEMD_DIR`` 把单元目录指到沙箱
（脚本里的默认值不变）。用真 systemd 测这条门禁就等于验一次东西就得往真系统里写单元。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "image_search_mcp_deploy.sh"

#: 受测脚本路径。反向自检必须真跑**变异版**，所以 check() 会把它指向临时副本；
#: 仓库里的真文件一步也不许动（旧版就是在这儿踩的雷：在真文件上改完再写回）。
RUN_TARGET = SCRIPT

TIMEOUT = 120

TOKEN = "tok-S3CRET-123"

STUBS = {
    "sudo": r"""#!/bin/bash
printf 'sudo\t%s\n' "$*" >> "$STUB_RECORDS"
"$@"
""",
    "systemctl": r"""#!/bin/bash
printf 'systemctl\t%s\n' "$*" >> "$STUB_RECORDS"
state="$STUB_STATE"
case "$1" in
  cat)
      rc=$(cat "$state/systemctl-cat.rc" 2>/dev/null || echo 0)
      [ "$rc" = "0" ] && cat "$state/systemctl-cat.txt" 2>/dev/null
      exit "$rc" ;;
  is-active)
      if [ "${2:-}" = "--quiet" ]; then
          exit "$(cat "$state/is-active-quiet.rc" 2>/dev/null || echo 0)"
      fi
      cat "$state/is-active.txt" 2>/dev/null || true
      exit "$(cat "$state/is-active.rc" 2>/dev/null || echo 0)" ;;
  *) exit 0 ;;
esac
""",
    "uv": r"""#!/bin/bash
printf 'uv\t%s\n' "$*" >> "$STUB_RECORDS"
exit "$(cat "$STUB_STATE/uv.rc" 2>/dev/null || echo 0)"
""",
    "curl": r"""#!/bin/bash
printf 'curl\t%s\n' "$*" >> "$STUB_RECORDS"
url=""
for a in "$@"; do case "$a" in http*) url="$a" ;; esac; done
if [[ "$*" == *'%{http_code}'* ]]; then
    case "$url" in
      */healthz)     cat "$STUB_STATE/healthz.code" 2>/dev/null || echo 200 ;;
      */messages/*)  cat "$STUB_STATE/messages.code" 2>/dev/null || echo 400 ;;
      *)             echo 200 ;;
    esac
fi
exit 0
""",
    "apt": r"""#!/bin/bash
printf 'apt\t%s\n' "$*" >> "$STUB_RECORDS"
exit 0
""",
    # 纯粹为了不让门禁白等 2 秒；这条门禁不验时序。
    "sleep": r"""#!/bin/bash
exit 0
""",
}


class Sandbox:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.work = tmp / "work"
        self.state = tmp / "state"
        self.systemd = tmp / "systemd"
        self.bin = tmp / "bin"
        self.home = tmp / "home"
        self.records = tmp / "records"
        for path in (self.work, self.state, self.systemd, self.bin, self.home):
            path.mkdir(parents=True, exist_ok=True)
        self.records.write_text("")
        for name, body in STUBS.items():
            stub = self.bin / name
            stub.write_text(body)
            stub.chmod(0o755)

    def setup(self, *, env_text: str | None, unit_exists: bool,
              is_active_rc: int = 0, cat_rc: int = 0,
              healthz: str = "200", messages: str = "400") -> None:
        (self.state / "is-active-quiet.rc").write_text(f"{is_active_rc}\n")
        (self.state / "is-active.rc").write_text(f"{is_active_rc}\n")
        (self.state / "is-active.txt").write_text("active\n" if is_active_rc == 0 else "failed\n")
        (self.state / "systemctl-cat.rc").write_text(f"{cat_rc}\n")
        (self.state / "systemctl-cat.txt").write_text(
            "[Service]\nEnvironment=MCP_AUTH_TOKEN=inline\n"
            "ExecStart=/usr/local/bin/uvx image-search-mcp --sse\n"
        )
        (self.state / "uv.rc").write_text("0\n")
        (self.state / "healthz.code").write_text(f"{healthz}\n")
        (self.state / "messages.code").write_text(f"{messages}\n")
        if unit_exists:
            (self.systemd / "image-search-mcp.service").write_text(
                "[Service]\nEnvironment=MCP_AUTH_TOKEN=inline\n"
            )
        if env_text is not None:
            (self.work / ".env").write_text(env_text)

    def run(self, args: list[str], stdin: str = "") -> tuple[subprocess.CompletedProcess, str]:
        env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.home),
            "STUB_STATE": str(self.state),
            "STUB_RECORDS": str(self.records),
            # 单元目录指到沙箱：脚本里的默认值不变，但门禁不碰真 /etc/systemd。
            "IMAGE_SEARCH_SYSTEMD_DIR": str(self.systemd),
            "LANG": "C.UTF-8",
            "TMPDIR": str(self.tmp),
        }
        proc = subprocess.run(
            ["bash", str(RUN_TARGET), *args],
            cwd=self.work, env=env, input=stdin,
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        return proc, self.records.read_text()

    # --- 便捷读取 ---------------------------------------------------------

    def env_files(self) -> list[Path]:
        return sorted(self.work.glob(".env*"))

    def unit_files(self) -> list[Path]:
        return sorted(self.systemd.glob("*"))


ENV_BASE = f"""HOST=0.0.0.0
PORT=8000
MCP_AUTH_TOKEN={TOKEN}
IMAGE_SEARCH_API_KEY=sauce-key
IMAGE_SEARCH_COOKIES=legacy-global
IMAGE_SEARCH_COOKIES_ENGINE=Yandex
IMAGE_SEARCH_COOKIES_YANDEX=keepme
"""


def scenario_update_ok(tmp: Path) -> list[str]:
    """仅升级：不动配置、重启的就是线上那个单元、凭据冒烟真的带上了 token。"""
    box = Sandbox(tmp)
    box.setup(env_text=ENV_BASE, unit_exists=True)
    before = (box.work / ".env").read_bytes()
    proc, records = box.run(["--update-only"])

    problems = []
    if proc.returncode != 0:
        problems.append(f"仅升级本该成功，却退出 {proc.returncode}：{proc.stdout} / {proc.stderr}")

    if "systemctl\trestart image-search-mcp" not in records:
        problems.append(
            "重启命令的 argv 不是 `restart image-search-mcp`（单元名/参数必须精确一致）："
            f"{records!r}"
        )
    if (box.work / ".env").read_bytes() != before:
        problems.append("仅升级模式改动了 .env —— 它承诺的是'不动配置'")
    if len(box.env_files()) != 1:
        problems.append(f"仅升级模式造出了额外的 .env 相关文件：{[p.name for p in box.env_files()]}")
    if any(line.startswith("sudo\tbash") for line in records.splitlines()):
        problems.append("仅升级模式写了 systemd 单元（不该动单元）")
    if "/messages/" not in records or f"Bearer {TOKEN}" not in records:
        problems.append(f"凭据冒烟没有发出带 token 的请求：{records!r}")
    if "/healthz" not in records:
        problems.append(f"没有做 /healthz 健康检查：{records!r}")
    if TOKEN in proc.stdout or TOKEN in proc.stderr:
        problems.append("脚本把 MCP_AUTH_TOKEN 打进输出了")
    return problems


def scenario_update_service_dead(tmp: Path) -> list[str]:
    """服务重启后没起来：必须非零退出，且**不许**说"更新完成"。

    旧版本把 is-active 塞进 $( )，状态被 echo 掩盖 —— 这个场景在旧版本上是绿的。
    """
    box = Sandbox(tmp)
    box.setup(env_text=ENV_BASE, unit_exists=True, is_active_rc=3)
    proc, _ = box.run(["--update-only"])

    problems = []
    if proc.returncode == 0:
        problems.append("服务没起来（is-active 非零）脚本却退出 0 —— 失败被吞了")
    if "更新完成" in proc.stdout:
        problems.append("服务没起来却打印了'更新完成'")
    if "active" not in (proc.stdout + proc.stderr):
        problems.append(f"没有说明服务状态，排障信息不足：{proc.stdout!r} / {proc.stderr!r}")
    return problems


def scenario_update_no_unit(tmp: Path) -> list[str]:
    """线上没有这个单元：必须非零退出，且**不许**去 restart 一个不存在的服务。"""
    box = Sandbox(tmp)
    box.setup(env_text=ENV_BASE, unit_exists=False, cat_rc=1)
    proc, records = box.run(["--update-only"])

    problems = []
    if proc.returncode == 0:
        problems.append("找不到单元却退出 0")
    if "systemctl\trestart" in records:
        problems.append(f"单元不存在却执行了 restart（旧版本的病）：{records!r}")
    if "找不到服务单元" not in proc.stderr + proc.stdout:
        problems.append(f"没有明确报出找不到单元：{proc.stdout!r} / {proc.stderr!r}")
    return problems


def scenario_replace_unit(tmp: Path) -> list[str]:
    """--replace-unit：先备份单元、写新单元、cookies 归属正确、非托管键保留。"""
    box = Sandbox(tmp)
    box.setup(env_text=ENV_BASE, unit_exists=True)
    stdin = "\n\n\n\nBing\nsid=1; other=2\n"
    proc, records = box.run(["--replace-unit"], stdin=stdin)

    problems = []
    if proc.returncode != 0:
        problems.append(f"--replace-unit 本该成功，却退出 {proc.returncode}：{proc.stdout} / {proc.stderr}")

    units = [p.name for p in box.unit_files()]
    if not any(".bak." in name for name in units):
        problems.append(f"替换单元前没有备份旧单元：{units}")
    written = box.systemd / "image-search-mcp.service"
    if not written.exists():
        problems.append("没有写出新单元文件")
    else:
        body = written.read_text()
        if f"EnvironmentFile={box.work}/.env" not in body:
            problems.append(f"新单元的 EnvironmentFile 不对：{body!r}")
        if "Start" not in body:
            problems.append(f"新单元缺少启动方式：{body!r}")

    backups = [p for p in box.work.glob(".env.bak.*")]
    if not backups:
        problems.append("重写 .env 前没有备份")

    new_env = (box.work / ".env").read_text()
    if 'IMAGE_SEARCH_COOKIES_BING="sid=1; other=2"' not in new_env:
        problems.append(
            "cookies 没有写成**带归属**的引擎变量（新契约要求）或含空白的值没加引号："
            f"{new_env!r}"
        )
    if "IMAGE_SEARCH_COOKIES_YANDEX=keepme" not in new_env:
        problems.append("重写 .env 时把已有引擎的 cookies 弄丢了")
    if "IMAGE_SEARCH_COOKIES_ENGINE=Yandex" not in new_env:
        problems.append("重写 .env 时把 cookies 归属键弄丢了")
    if "IMAGE_SEARCH_COOKIES=legacy-global" not in new_env:
        problems.append(
            "重写 .env 时把旧式的全局 IMAGE_SEARCH_COOKIES 删了 —— 那是凭据，"
            "不该被'重新配置'静默抹掉"
        )
    if "已原样保留" not in proc.stdout + proc.stderr:
        problems.append(
            "保留了旧式全局 cookies 却没提醒它需要归属键（没有 ENGINE 归属时服务会拒绝启动）"
        )

    if "systemctl\trestart image-search-mcp" not in records:
        problems.append(f"没有重启线上单元：{records!r}")
    if TOKEN in proc.stdout or TOKEN in proc.stderr:
        problems.append("脚本把 MCP_AUTH_TOKEN 打进输出了")
    return problems


def scenario_reconfigure_keeps_unit(tmp: Path) -> list[str]:
    """配置流程 + 保留单元：单元不写，但 .env 会重写 —— 这一点必须说清楚。

    旧版本先重写 .env、后判定是否保留单元，于是"保留了单元"给了人"什么都没改"的错觉，
    而旧单元照样读那个被重写过的 .env。
    """
    box = Sandbox(tmp)
    box.setup(env_text=ENV_BASE, unit_exists=True)
    before_unit = (box.systemd / "image-search-mcp.service").read_text()
    proc, records = box.run(["--reconfigure"], stdin="\n\n\n\n\n")

    problems = []
    if proc.returncode != 0:
        problems.append(f"--reconfigure 本该成功，却退出 {proc.returncode}：{proc.stdout} / {proc.stderr}")
    if any(line.startswith("sudo\tbash") for line in records.splitlines()):
        problems.append("没给 --replace-unit 却写了单元文件")
    if (box.systemd / "image-search-mcp.service").read_text() != before_unit:
        problems.append("没给 --replace-unit 却改了单元文件")
    if not (box.work / ".env").exists():
        problems.append(".env 没了")
    if "IMAGE_SEARCH_COOKIES_YANDEX=keepme" not in (box.work / ".env").read_text():
        problems.append("重写 .env 时把已有引擎的 cookies 弄丢了")
    if "IMAGE_SEARCH_COOKIES=legacy-global" not in (box.work / ".env").read_text():
        problems.append("重写 .env 时把旧式全局 cookies 删了（凭据不该被静默抹掉）")
    if "不覆盖" not in proc.stdout:
        problems.append("没有说明单元被保留（人无从知道改了什么、没改什么）")
    if "本次配置变更已生效" not in proc.stdout:
        problems.append(
            "保留了单元却没说明 .env 已被改写、且正是该单元运行时会读的文件 —— "
            "这会让人以为'保留了单元'等于'什么都没改'"
        )
    return problems


SCENARIOS = {
    "仅升级（正常）": scenario_update_ok,
    "仅升级（服务没起来）": scenario_update_service_dead,
    "仅升级（单元不存在）": scenario_update_no_unit,
    "替换单元（--replace-unit）": scenario_replace_unit,
    "配置流程（保留单元）": scenario_reconfigure_keeps_unit,
}


def check(script: Path) -> list[str]:
    # 变异版必须被真跑：把受测目标切到传入的路径（仓库真文件只在 main 里读与摘要）。
    global RUN_TARGET
    RUN_TARGET = script
    problems: list[str] = []
    for name, fn in SCENARIOS.items():
        with tempfile.TemporaryDirectory(prefix="deploy-gate-") as raw:
            try:
                found = fn(Path(raw))
            except subprocess.TimeoutExpired:
                found = [f"{TIMEOUT}s 内没跑完 —— 脚本卡住了"]
        problems.extend(f"[{name}] {item}" for item in found)
    RUN_TARGET = SCRIPT
    return problems


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def main() -> int:
    if not SCRIPT.exists():
        print(f"找不到 {SCRIPT}", file=sys.stderr)
        return 2

    source = SCRIPT.read_text(encoding="utf-8")
    problems: list[str] = []

    # --- 真跑一遍五条场景 -------------------------------------------------
    problems.extend(check(SCRIPT))

    # --- 反向自检 ①：把显式的失败检查改回"echo 一下就完事" -------------------
    fix_anchor = "verify_service || exit 1"
    if fix_anchor not in source:
        problems.append(
            f"反向自检没法进行：脚本里找不到 {fix_anchor!r}（源码形状变了？这条门禁可能已失效）"
        )
    else:
        mutated = source.replace(fix_anchor, "echo \"服务状态: $(sudo systemctl is-active $UNIT_NAME)\"")
        with tempfile.TemporaryDirectory(prefix="deploy-gate-mut1-") as raw:
            path = Path(raw) / "mutated.sh"
            path.write_text(mutated)
            if not check(path):
                problems.append(
                    "反向自检失败：把失败检查改回 `echo \"服务状态: $(...)\"` 之后门禁仍然全绿 —— "
                    "说明它测不出'失败被 echo 掩盖'这个缺陷"
                )

    # --- 反向自检 ②：删掉"保留非托管键"的逻辑 ------------------------------
    keep_anchor = "if [ -n \"$PRESERVED\" ]; then"
    if keep_anchor not in source:
        problems.append(f"反向自检没法进行：脚本里找不到 {keep_anchor!r}")
    else:
        lines = source.splitlines(keepends=True)
        kept: list[str] = []
        skipping = False
        for line in lines:
            if keep_anchor in line:
                skipping = True
                continue
            if skipping:
                if line.strip() == "fi":
                    skipping = False
                continue
            kept.append(line)
        mutated = "".join(kept)
        if mutated == source:
            problems.append("反向自检构造失败：删除逻辑没有生效")
        else:
            with tempfile.TemporaryDirectory(prefix="deploy-gate-mut2-") as raw:
                path = Path(raw) / "mutated.sh"
                path.write_text(mutated)
                if not check(path):
                    problems.append(
                        "反向自检失败：删掉'保留非托管键'之后门禁仍然全绿 —— "
                        "说明它没在验 .env 重写会不会丢已有 cookies"
                    )

    # --- 自检：这条门禁不许动仓库里的真脚本 -------------------------------
    #
    # 旧实现就在真脚本上做变异再写回。内容能还原，所以只有 mtime 会露出马脚 ——
    # 而"内容一致"看起来一切正常，这正是它藏了这么久的原因。
    if digest(SCRIPT) != digest_before:
        problems.append("这条门禁改动了仓库里的真脚本 —— 变异必须在临时副本上做")
    elif SCRIPT.stat().st_mtime_ns != mtime_before:
        problems.append(
            "仓库里的真脚本 mtime 变了（内容虽然一致）—— 有代码在真货上写了一遍，"
            "这正是旧版反向自检的行为"
        )

    if problems:
        print("部署脚本门禁没通过：")
        for item in problems:
            print(f"  - {item}")
        return 1

    print("部署脚本：五条场景全过（单元名精确、失败响亮、.env 非托管键保留），两项反向自检有效")
    return 0


if __name__ == "__main__":
    digest_before = digest(SCRIPT)
    mtime_before = SCRIPT.stat().st_mtime_ns
    raise SystemExit(main())
